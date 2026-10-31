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


def chat_port() -> int:
    return int(os.environ.get("HUB_CHAT_PORT", "8080"))


def embed_port() -> int:
    return int(os.environ.get("HUB_EMBED_PORT", "11435"))
