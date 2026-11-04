"""lynx — a lynx-like text browser harness, stdlib only.

Fetches a page and returns its readable text: title, headings,
paragraphs and list items, with scripts, styles and navigation
stripped. No JavaScript, no rendering — the way lynx -dump sees a
page. The research agent reads through this, never through a
headless browser.

Allowed schemes: http, https, file (local test pages and docs).
Every failure is reported as an error string, never raised, so one
dead link cannot kill a research task. Text is capped so a huge
page cannot flood a task record.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener
from urllib.error import URLError, HTTPError

MAX_CHARS = 200000
MAX_LINKS = 20
TIMEOUT_S = 30
USER_AGENT = "UncensoredNodeResearch/0.0.1 (text-only, like lynx)"

_SKIP = {"script", "style", "nav", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._lines = []
        self._buf = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and len(self.links) < MAX_LINKS:
                full = urljoin(self.base_url, href)
                if urlparse(full).scheme in ("http", "https"):
                    self.links.append(full)
        if tag in ("p", "h1", "h2", "h3", "h4", "li", "tr", "blockquote", "pre"):
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in ("p", "h1", "h2", "h3", "h4", "li", "tr", "blockquote", "pre"):
            self._flush()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._buf.append(text)

    def _flush(self):
        if self._buf:
            line = " ".join(self._buf).strip()
            if line:
                self._lines.append(line)
            self._buf = []

    def text(self):
        self._flush()
        # Collapse blank-heavy runs; keep document order.
        out = []
        for line in self._lines:
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                out.append(line)
        return "\n".join(out)


def extract_text(html, base_url=""):
    """Pure HTML -> {title, text, links}. No network; unit-testable."""
    parser = _TextExtractor(base_url)
    try:
        parser.feed(html)
    except Exception:
        pass
    return {"title": re.sub(r"\s+", " ", parser.title).strip(),
            "text": parser.text(),
            "links": parser.links}


def fetch_text(url, timeout=TIMEOUT_S, max_chars=MAX_CHARS):
    """Fetch a URL as text. Returns a dict, never raises."""
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https", "file"):
        return {"url": url, "title": "", "text": "", "links": [],
                "error": "unsupported scheme: %s" % scheme}
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with build_opener().open(req, timeout=timeout) as r:
            raw = r.read(max_chars + 65536)
            charset = "utf-8"
            try:
                ctype = r.headers.get_content_charset()
                if ctype:
                    charset = ctype
            except Exception:
                pass
            html = raw.decode(charset, errors="replace")
    except HTTPError as e:
        return {"url": url, "title": "", "text": "", "links": [],
                "error": "HTTP %s" % e.code}
    except (URLError, OSError, ValueError) as e:
        return {"url": url, "title": "", "text": "", "links": [],
                "error": "fetch failed: %s" % (getattr(e, "reason", e) or e)}
    except Exception as e:
        return {"url": url, "title": "", "text": "", "links": [],
                "error": "fetch failed: %s" % e}
    out = extract_text(html, base_url=url)
    out["url"] = url
    out["text"] = out["text"][:max_chars]
    out["error"] = ""
    return out
