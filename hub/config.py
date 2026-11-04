"""Paths and defaults for the hub. Data lives outside git, in data/ by default."""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir() -> str:
    return os.environ.get("HUB_DATA_DIR") or os.path.join(REPO_ROOT, "data")


def models_dir() -> str:
    return os.path.join(REPO_ROOT, "models")


def bin_dir() -> str:
    return os.path.join(REPO_ROOT, "bin")


def load_catalog():
    path = os.path.join(REPO_ROOT, "models.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def default_model(role):
    """The model a startup loads for a role: the catalog entry flagged
    "default", else the first entry of that role, else None. This is
    the single source of truth the System tab displays and the web
    server-start paths use, so the shown name is the loaded name."""
    cat = load_catalog()["models"]
    for m in cat:
        if m.get("role") == role and m.get("default"):
            return m
    for m in cat:
        if m.get("role") == role:
            return m
    return None


def _override_path():
    return os.path.join(data_dir(), "local-chat-model.json")


def _valid_gguf_name(name):
    """A plain GGUF basename: no separators, no parent escape."""
    return (isinstance(name, str) and bool(name)
            and "/" not in name and "\\" not in name and ".." not in name
            and name.lower().endswith(".gguf"))


def get_chat_model_override():
    """The operator's chosen local chat GGUF basename, or None."""
    try:
        with open(_override_path(), encoding="utf-8") as f:
            name = json.load(f).get("file")
    except Exception:
        return None
    return name if _valid_gguf_name(name) else None


def set_chat_model_override(name):
    """Persist the operator's local chat choice. Raises ValueError
    on a bad name; does not check the file exists (the caller does)."""
    if not _valid_gguf_name(name):
        raise ValueError("not a plain .gguf filename")
    with open(_override_path(), "w", encoding="utf-8") as f:
        json.dump({"file": name}, f, indent=2)


def clear_chat_model_override():
    try:
        os.remove(_override_path())
    except OSError:
        pass


def effective_chat_model():
    """The chat model actually in use: the operator's override when
    its file is on disk, else the catalog default. The System tab,
    the restart paths, and autorun all read this one helper, so the
    shown name is the loaded name even after the operator picks a
    different GGUF. Returns a catalog-style dict + overridden flag,
    or None when nothing is configured."""
    override = get_chat_model_override()
    if override:
        path = os.path.join(models_dir(), override)
        if os.path.exists(path):
            cat = load_catalog()["models"]
            match = next((m for m in cat
                          if m.get("role") == "chat"
                          and m.get("file") == override), None)
            out = dict(match) if match else {"id": None, "name": override,
                                             "file": override, "role": "chat"}
            out["overridden"] = True
            return out
    base = default_model("chat")
    if base is None:
        return None
    out = dict(base)
    out["overridden"] = False
    return out


def select_local_model(name):
    """Validate + persist a local chat choice. Selecting the catalog
    default clears the override (default stays default). Returns the
    effective model dict. Raises ValueError on a bad name, FileNotFoundError
    when the file is not in models/."""
    if not _valid_gguf_name(name):
        raise ValueError("not a plain .gguf filename")
    if not os.path.exists(os.path.join(models_dir(), name)):
        raise FileNotFoundError("no such GGUF in models/")
    base = default_model("chat")
    if base and base.get("file") == name:
        clear_chat_model_override()
    else:
        set_chat_model_override(name)
    return effective_chat_model()


def chat_port() -> int:
    return int(os.environ.get("HUB_CHAT_PORT", "8080"))


def embed_port() -> int:
    return int(os.environ.get("HUB_EMBED_PORT", "11435"))
