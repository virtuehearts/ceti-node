"""Hermes-style full-screen TUI for the hub. Stdlib curses only.

Layout (same rules as CETI's TUI, which fixed the Windows overflow):
  header  reverse-video bar: HUB + fingerprint + ctx + VRAM/RAM + servers
  pane    Main Menu / submenu, or transcript / live story output
  input   prompt (menu choice, or typing, or /command)
  footer  reverse-video status bar: door, streaming state, key hints

Hermes chrome (the look this TUI was missing): phosphor palette —
green primary, cyan headers, dim secondary, amber warnings, red
errors; boxed panels for the menu and status doors; a stories table;
one exact next action per blocker. On a terminal without colour
support, bold / reverse / dim carry the same structure.

  - Wrap, never truncate: long lines wrap to width-1 at word boundaries.
  - Never write the last column, never write the bottom-right cell.
  - Tabs expanded, control chars stripped, before wrapping.

Doors (Hermes-style numbered menu, single-key choices):
  1  Chat — typing here asks the model, streaming token by token
  2  Stories — list, new (wizard: premise, genre, perspective,
     rating, plot, outline), write live, status, compile
  3  Memory — remember / recall, sample texts status
  4  Status — identity, servers, models, story progress
  0  Quit

Slash commands (in Chat, with autocomplete popup on Tab):
  /menu, /quit, /help, /clear, /remember <text>, /recall <query>,
  /story, /status
Up/Down cycles input history (outside the popup). Ctrl-C abandons
a running answer and drops back to input. Enter while streaming
cancels the old answer and starts the new one.

On Windows, curses is not in the stdlib: install windows-curses
(`pip install windows-curses`) and the same code runs. If curses
cannot be imported, `run_tui` raises a clear error and the caller
falls back to the line chat.
"""
import queue
import textwrap
import threading
import time

from . import memory
from .config import chat_port, embed_port, data_dir
from .identity import ensure_identity
from .servers import _health, chat_complete, embed_text, stream_chat
from .story import StoryWriter, word_count

COMMANDS = ("/menu", "/quit", "/help", "/clear", "/remember", "/recall",
            "/story", "/status", "/new")


# ---- Hermes chrome: phosphor palette, boxed panels, status bars -------
# Pure functions except Theme.init(), so tests exercise them without curses.

def line_role(line: str) -> str:
    """Colour role for a transcript line, by prefix."""
    if line.startswith("you> "):
        return "cyan"
    if line.startswith("hub> ") or line.startswith("hub story> "):
        return "green"
    if line.startswith("[chat error") or "failed" in line[:40]:
        return "red"
    if line.startswith("[") or line.startswith("  !"):
        return "amber"
    if line.startswith("---"):
        return "dim"
    if line.startswith("STORIES") or line.startswith("UNCENSORED"):
        return "title"
    return "green"


class Theme:
    """Curses attributes per role. Safe without colour support: bold,
    reverse and dim still apply; colours are added only if available."""

    def __init__(self):
        self.enabled = False
        self._pairs = {}
        self._rev = 0
        self._bold = 0
        self._dim = 0

    def init(self):
        try:
            import curses
            self._rev = curses.A_REVERSE
            self._bold = curses.A_BOLD
            self._dim = curses.A_DIM
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
                pairs = {"green": curses.COLOR_GREEN,
                         "cyan": curses.COLOR_CYAN,
                         "amber": curses.COLOR_YELLOW,
                         "red": curses.COLOR_RED}
                for i, (role, col) in enumerate(pairs.items(), start=1):
                    curses.init_pair(i, col, -1)
                    self._pairs[role] = curses.color_pair(i)
                self.enabled = True
        except Exception:
            pass

    def attr(self, role: str) -> int:
        if role == "rev":
            return self._rev
        if role == "title":
            return self._pairs.get("cyan", 0) | self._bold
        if role == "dim":
            return self._pairs.get("green", 0) | self._dim
        return self._pairs.get(role, 0)


def panel(title: str, rows, width: int):
    """Box rows into a panel. rows are (text, role) tuples, or the literal
    "sep" for a divider. Returns (text, role) lines, all one width."""
    pw = max(24, width - 2)
    inner = pw - 2
    field = inner - 2  # 1-space padding each side
    out = []
    top = "┌─ " + title + " "
    out.append((top + "─" * max(0, inner - len(top) + 1) + "┐", "title"))
    for row in rows:
        if row == "sep":
            out.append(("├" + "─" * inner + "┤", "dim"))
            continue
        text, role = row
        for piece in wrap_lines(text, field + 1):
            out.append(("│ " + piece.ljust(field) + " │", role))
    out.append(("└" + "─" * inner + "┘", "dim"))
    return out


def draw(stdscr, y, x, text, attr=0):
    """One addstr that never raises and never touches the last column."""
    try:
        stdscr.addstr(y, x, text, attr)
    except Exception:
        pass


def wrap_lines(text: str, width: int):
    """Wrap text to width-1, word boundaries, no truncation, no last column."""
    width = max(10, width - 1)
    out = []
    for raw in text.replace("\t", "    ").split("\n"):
        clean = "".join(ch for ch in raw if ch == "\n" or ord(ch) >= 32)
        if not clean.strip():
            out.append("")
        else:
            out.extend(textwrap.wrap(clean, width=width, break_long_words=False,
                                     break_on_hyphens=False) or [""])
    return out


class FakeScreen:
    """Minimal screen for tests: records put() calls, no real terminal."""
    def __init__(self, height=24, width=80):
        self.height = height
        self.width = width
        self.lines = [""] * height

    def getmaxyx(self):
        return self.height, self.width

    def put(self, y, x, text, attr=None):
        if 0 <= y < self.height and 0 <= x < self.width:
            text = text[:max(0, self.width - x - 1)]
            row = self.lines[y]
            row = row[:x] + text + row[x + len(text):]
            self.lines[y] = row[:self.width - 1]


def render_pane(screen, lines, top=1, height=None):
    """Render wrapped lines into the pane area, clipping to screen height."""
    h, w = screen.getmaxyx()
    if height is None:
        height = h - 4
    visible = lines[-(height):] if len(lines) > height else lines
    for i, line in enumerate(visible):
        for j, wrapped in enumerate(wrap_lines(line, w)):
            y = top + i + j
            if y >= top + height:
                break
            screen.put(y, 0, wrapped)


class TUI:
    def __init__(self, ident, auto_serve=False, ctx=32768, ram=False):
        self.ident = ident
        self.ctx = ctx
        self.ram = ram
        self.auto_serve = auto_serve
        self.mode = "menu"  # menu | chat | stories | stories_new | memory | status
        self.transcript = []
        self.input_text = ""
        self.history = []
        self.history_idx = -1
        self.popup = []
        self.popup_idx = 0
        self.stream_q = queue.Queue()
        self.stream_thread = None
        self.stream_cancel = False
        self.streaming = False
        self.story_writer = StoryWriter(ident=ident)
        self.story_wizard = None
        self.story_selected = None

    # ---- status -------------------------------------------------------
    def header(self) -> str:
        chat = "up" if _health(chat_port()) else "down"
        emb = "up" if _health(embed_port()) else "down"
        return (f"HUB // {self.ident.fingerprint} | chat:{chat} embed:{emb} "
                f"| ctx:{self.ctx} {'RAM' if self.ram else 'VRAM'}")

    def menu_lines(self):
        return [
            "UNCENSORED HUB // MAIN MENU",
            "",
            "  1  Chat — ask the model (streaming, live)",
            "  2  Stories — write long-form stories live, block by block",
            "  3  Memory — remember / recall / sample texts",
            "  4  Status — identity, servers, models",
            "  0  Quit",
            "",
            "Type a number, or /help for slash commands.",
        ]

    def menu_panel(self, width: int):
        """Menu door as a boxed panel; the title line becomes the panel
        title, the rest become rows."""
        rows = []
        for ln in self.menu_lines()[1:]:
            if ln.startswith("Type a number"):
                rows.append((ln, "dim"))
            else:
                rows.append((ln, "green"))
        return panel("UNCENSORED HUB // MAIN MENU", rows, width)

    def status_panels(self, width: int):
        """Status door as mission-control panels: session, servers, memory,
        a stories table, and one exact next action per blocker."""
        chat = _health(chat_port())
        emb = _health(embed_port())
        try:
            mems = memory.count(memory.connect(data_dir()), self.ident.hash)
        except Exception:
            mems = "?"
        panels = []
        panels += panel("SESSION", [
            (f"fingerprint  {self.ident.fingerprint}", "green"),
            (f"node         {self.ident.node_id}", "green"),
        ], width)
        panels += panel("SERVERS", [
            (f"chat   {'✓ up' if chat else '✗ down'}",
             "green" if chat else "red"),
            (f"embed  {'✓ up' if emb else '✗ down'}",
             "green" if emb else "red"),
        ], width)
        panels += panel("MEMORY", [
            (f"vectors  {mems}", "green"),
        ], width)
        try:
            stories = self.story_writer.list_stories()
        except Exception:
            stories = []
        srows = []
        if not stories:
            srows.append(("no stories yet — open the Stories door and type 'new'",
                          "dim"))
        else:
            srows.append(("ID  BLOCKS  WORDS  TOKENS", "title"))
            srows.append("sep")
            for s in stories:
                try:
                    st = self.story_writer.status(s)
                    srows.append((f"{s}  {st['blocks']}  {st['words']}  "
                                  f"~{st['est_tokens']}", "green"))
                except Exception:
                    srows.append((f"{s}  (status unreadable)", "amber"))
        panels += panel("STORIES", srows, width)
        nxt = []
        if not chat:
            nxt.append(("NEXT: start the chat server — hub serve", "amber"))
        if not emb:
            nxt.append(("NEXT: start the embed server — hub serve", "amber"))
        if not nxt:
            nxt.append(("All servers up. Type /menu to return.", "dim"))
        panels += panel("NEXT ACTION", nxt, width)
        return panels

    # ---- chat ---------------------------------------------------------
    def ask_streaming(self, user_text: str):
        """Start a background streaming answer; chunks land in stream_q."""
        self.stream_cancel = False
        self.streaming = True
        self.transcript.append(f"you> {user_text}")

        def run():
            try:
                from .servers import chat_complete as _cc
                # Streaming path: collect chunks, push to queue.
                parts = []
                for chunk in stream_chat(
                        [{"role": "user", "content": user_text}],
                        max_tokens=2048, temperature=0.7,
                        cancel=lambda: self.stream_cancel):
                    parts.append(chunk)
                    self.stream_q.put(("chunk", chunk))
                self.stream_q.put(("done", "".join(parts)))
            except Exception as e:
                self.stream_q.put(("error", str(e)))
        self.stream_thread = threading.Thread(target=run, daemon=True)
        self.stream_thread.start()

    def pump_stream(self):
        """Drain stream_q into the transcript. Call once per frame."""
        got = False
        while True:
            try:
                kind, payload = self.stream_q.get_nowait()
            except queue.Empty:
                break
            got = True
            if kind == "chunk":
                if self.transcript and self.transcript[-1].startswith("hub> "):
                    self.transcript[-1] += payload
                else:
                    self.transcript.append(f"hub> {payload}")
            elif kind == "done":
                self.streaming = False
                # Remember the exchange in vector memory (best effort).
                try:
                    conn = memory.connect(data_dir())
                    emb = embed_text(self.transcript[-2][5:] if len(self.transcript) >= 2 else "")
                    if emb is not None:
                        memory.add(conn, self.ident.hash, self.transcript[-2], emb, source="chat")
                        emb2 = embed_text(payload)
                        if emb2 is not None:
                            memory.add(conn, self.ident.hash, payload, emb2, source="chat")
                except Exception:
                    pass
            elif kind == "error":
                self.streaming = False
                self.transcript.append(f"[chat error: {payload}]")
        return got

    # ---- commands -----------------------------------------------------
    def handle_line(self, line: str):
        """Handle one submitted input line in the current mode. Returns
        a status string for the footer, or None."""
        line = line.strip()
        if not line:
            return None
        self.history.append(line)
        self.history_idx = -1
        if self.mode == "menu":
            return self._menu_choice(line)
        if line == "/menu":
            self.mode = "menu"
            return "main menu"
        if line == "/quit":
            return "quit"
        if line == "/help":
            self.transcript.extend([
                "Commands: /menu /quit /help /clear /remember <text> /recall <query> /story /status",
                "In Chat, typing asks the model. Ctrl-C cancels a running answer.",
            ])
            return None
        if line == "/clear":
            self.transcript.clear()
            return "cleared"
        if line.startswith("/remember "):
            return self._remember(line[len("/remember "):].strip())
        if line.startswith("/recall "):
            return self._recall(line[len("/recall "):].strip())
        if line == "/story":
            self.mode = "stories"
            return "stories door"
        if line == "/status":
            self.mode = "status"
            return "status door"
        if self.mode == "chat":
            if self.streaming:
                self.stream_cancel = True
                self.transcript.append("[previous answer cancelled]")
            self.ask_streaming(line)
            return "streaming..."
        if self.mode == "stories":
            return self._stories_choice(line)
        return None

    def _menu_choice(self, line: str):
        mapping = {"1": "chat", "2": "stories", "3": "memory", "4": "status", "0": "quit"}
        if line in mapping:
            if mapping[line] == "quit":
                return "quit"
            self.mode = mapping[line]
            return f"{self.mode} door"
        self.transcript.append("Type 1-4, or 0 to quit.")
        return None

    def _remember(self, text: str):
        emb = embed_text(text)
        if emb is None:
            return "embedding server not running"
        conn = memory.connect(data_dir())
        rid = memory.add(conn, self.ident.hash, text, emb, source="remember")
        return f"remembered as id {rid}"

    def _recall(self, query: str):
        emb = embed_text(query)
        if emb is None:
            return "embedding server not running"
        conn = memory.connect(data_dir())
        hits = memory.search(conn, self.ident.hash, emb, top_k=5)
        if not hits:
            self.transcript.append("no memories for this identity yet")
            return None
        for score, text, source, rid, mdl in hits:
            self.transcript.append(f"[{score:.3f}] id={rid} ({source}{', ' + mdl if mdl else ''}) {text}")
        return None

    def _stories_choice(self, line: str):
        if self.story_wizard is not None:
            return self._wizard_step(line)
        if line == "1" or line.lower() == "list":
            stories = self.story_writer.list_stories()
            if not stories:
                self.transcript.append("no stories yet — type 'new' to start one")
            else:
                for s in stories:
                    st = self.story_writer.status(s)
                    meta = st.get("meta", {})
                    self.transcript.append(
                        f"{s} — {st['blocks']} blocks, {st['words']} words, "
                        f"~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k), "
                        f"{meta.get('genre','')} / {meta.get('perspective','')} / {meta.get('rating','')}")
            return None
        if line.lower() == "new":
            self.story_wizard = {"step": 0, "fields": {}}
            self.transcript.append("New story — premise? (type it, or 'cancel')")
            return "story wizard: premise"
        if line.lower().startswith("write "):
            parts = line.split()
            sid = parts[1] if len(parts) > 1 else self.story_selected
            if not sid:
                return "usage: write <story-id> [pages] [count]"
            pages = int(parts[2]) if len(parts) > 2 else 3
            count = int(parts[3]) if len(parts) > 3 else 1
            return self._write_live(sid, pages, count)
        if line.lower().startswith("status "):
            sid = line.split(None, 1)[1]
            st = self.story_writer.status(sid)
            self.transcript.append(
                f"{sid}: {st['blocks']} blocks, {st['words']} words, "
                f"~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k)")
            return None
        if line.lower().startswith("compile "):
            sid = line.split(None, 1)[1]
            r = self.story_writer.compile(sid)
            self.transcript.append(f"compiled: {r['chapters']} chapters, {r['words']} words -> {r['md']}")
            return None
        self.transcript.append("Stories: list | new | write <id> [pages] [count] | status <id> | compile <id> | /menu")
        return None

    def _wizard_step(self, line: str):
        if line.lower() == "cancel":
            self.story_wizard = None
            return "wizard cancelled"
        steps = [("premise", "Premise?"),
                 ("genre", "Genre? (e.g. fantasy, sci-fi, horror — empty for unspecified)"),
                 ("perspective", "Perspective? 1st / 2nd / 3rd / omniscient (empty = 3rd)"),
                 ("rating", "Rating? G / PG / PG-13 / R / X (empty = PG-13)"),
                 ("plot", "Plot? (one paragraph, empty = bible's arc)"),
                 ("outline", "Outline? (chapter outline, empty = bible generates 12 beats)")]
        idx = self.story_wizard["step"]
        key, _prompt = steps[idx]
        self.story_wizard["fields"][key] = line
        idx += 1
        if idx >= len(steps):
            f = self.story_wizard["fields"]
            self.story_wizard = None
            try:
                sid = self.story_writer.new_story(
                    f["premise"], genre=f.get("genre", ""), storyline=f["premise"],
                    perspective=f.get("perspective", ""), plot=f.get("plot", ""),
                    outline=f.get("outline", ""), rating=f.get("rating", ""))
            except Exception as e:
                return f"story creation failed: {e}"
            self.story_selected = sid
            self.transcript.append(f"story created: {sid}")
            self.transcript.append(f"write live with: write {sid} 3 1")
            return "story created"
        self.story_wizard["step"] = idx
        self.transcript.append(steps[idx][1])
        return f"story wizard: {steps[idx][0]}"

    def _write_live(self, sid: str, pages: int, count: int):
        """Write blocks, streaming each block's text into the transcript live."""
        for _ in range(count):
            self.transcript.append(f"--- writing block for {sid} ({pages} pages) ---")
            chunks = []

            def on_chunk(c):
                chunks.append(c)
                # Append to the last transcript line in chunks, for live display.
                if self.transcript and self.transcript[-1].startswith("hub story> "):
                    self.transcript[-1] += c
                else:
                    self.transcript.append(f"hub story> {c}")
            try:
                r = self.story_writer.next_block(sid, pages=pages, on_chunk=on_chunk)
            except Exception as e:
                self.transcript.append(f"[story write failed: {e}]")
                return f"story write failed: {e}"
            if r.get("blocked"):
                self.transcript.append("[gate blocked — no generation ran]")
                for v in r.get("violations", []):
                    self.transcript.append(f"  ! {v}")
                return "gate blocked: rewrite the beat, or force it from the CLI with --force"
            if r.get("accepted") is False:
                self.transcript.append(f"[gate flagged — held as draft, not compiled: {r.get('flagged_draft')}]")
                for v in r.get("violations", []):
                    self.transcript.append(f"  ! {v}")
                return "gate flagged: draft saved, not compiled"
            self.transcript.append(f"--- block {r['block']} done: {r['words']} words ---")
        st = self.story_writer.status(sid)
        return f"total: {st['blocks']} blocks, {st['words']} words, ~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k)"

    # ---- autocomplete -------------------------------------------------
    def autocomplete(self, text: str):
        if not text.startswith("/"):
            return []
        return [c for c in COMMANDS if c.startswith(text)]


def run_tui(ident, auto_serve=False, ctx=32768, ram=False):
    """Run the full-screen TUI. Raises RuntimeError if curses is unavailable."""
    try:
        import curses
    except ImportError as e:
        raise RuntimeError(
            "curses is not available on this Python. On Windows, run: pip install windows-curses, then try again. "
            "Falling back to the line chat: python -m hub chat") from e
    tui = TUI(ident, auto_serve=auto_serve, ctx=ctx, ram=ram)

    def main(stdscr):
        curses.curs_set(1)
        stdscr.nodelay(False)
        stdscr.keypad(True)
        theme = Theme()
        theme.init()
        while True:
            # Pump streaming chunks (non-blocking drain, then redraw).
            tui.pump_stream()
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            # Header bar (reverse video, full width)
            hdr = " " + tui.header()
            draw(stdscr, 0, 0, hdr[:w - 1].ljust(w - 1), theme.attr("rev"))
            # Pane: (text, role) lines, wrapped, clipped to the pane area
            if tui.mode == "menu":
                pane = tui.menu_panel(w)
            elif tui.mode == "stories":
                pane = [("STORIES — list | new | write <id> [pages] [count] | status <id> | compile <id> | /menu", "title")]
                pane += [(ln, line_role(ln)) for ln in tui.transcript[-100:]]
            elif tui.mode == "status":
                pane = tui.status_panels(w)
                pane += [(ln, line_role(ln)) for ln in tui.transcript[-20:]]
            else:
                pane = [(ln, line_role(ln)) for ln in tui.transcript]
            pane_h = max(1, h - 4)
            wrapped = []
            for text, role in pane:
                for piece in wrap_lines(text, w):
                    wrapped.append((piece, role))
            visible = wrapped[-pane_h:] if len(wrapped) > pane_h else wrapped
            for i, (ln, role) in enumerate(visible):
                draw(stdscr, 1 + i, 0, ln[:w - 1], theme.attr(role))
            # Separator above input
            draw(stdscr, h - 3, 0, "─" * (w - 1), theme.attr("dim"))
            # Input
            prompt = f"{tui.mode}> "
            draw(stdscr, h - 2, 0, prompt, theme.attr("cyan"))
            draw(stdscr, h - 2, len(prompt),
                 tui.input_text[:max(0, w - 1 - len(prompt))],
                 theme.attr("green"))
            # Footer status bar (reverse video, full width)
            foot = (f" hub │ door:{tui.mode} │ "
                    f"{'streaming…' if tui.streaming else 'idle'} │ "
                    f"Enter: send  Tab: complete  Up/Down: history  "
                    f"Ctrl-C: cancel  /menu: main menu")
            draw(stdscr, h - 1, 0, foot[:w - 1].ljust(w - 1),
                 theme.attr("rev"))
            try:
                stdscr.move(h - 2, min(len(prompt) + len(tui.input_text), w - 2))
            except curses.error:
                pass
            stdscr.refresh()
            # Autocomplete popup (drawn after refresh, simple one-line list)
            if tui.input_text.startswith("/"):
                matches = tui.autocomplete(tui.input_text)
                if matches:
                    draw(stdscr, h - 4, 0, "  ".join(matches)[:w - 1],
                         theme.attr("cyan"))
                    stdscr.refresh()
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                break
            if key in (3,):  # Ctrl-C
                if tui.streaming:
                    tui.stream_cancel = True
                    tui.transcript.append("[answer cancelled]")
                continue
            if key in (10, 13):  # Enter
                line = tui.input_text
                tui.input_text = ""
                result = tui.handle_line(line)
                if result == "quit":
                    break
                continue
            if key == 9:  # Tab
                matches = tui.autocomplete(tui.input_text)
                if matches:
                    tui.input_text = matches[tui.popup_idx % len(matches)]
                    tui.popup_idx += 1
                continue
            if key == curses.KEY_UP:
                if tui.history:
                    tui.history_idx = (tui.history_idx + 1) % len(tui.history)
                    tui.input_text = tui.history[-(tui.history_idx + 1)]
                continue
            if key == curses.KEY_DOWN:
                if tui.history:
                    tui.history_idx = max(-1, tui.history_idx - 1)
                    tui.input_text = tui.history[-(tui.history_idx + 1)] if tui.history_idx >= 0 else ""
                continue
            if key in (127, 8, curses.KEY_BACKSPACE):
                tui.input_text = tui.input_text[:-1]
                continue
            if 32 <= key <= 126:
                tui.input_text += chr(key)
                continue
    curses.wrapper(main)
