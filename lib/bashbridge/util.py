"""Small helpers shared by all modules. Standard library only."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

PLUGIN_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def state_dir() -> str:
    return os.environ.get("BASH_BRIDGE_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude-bash-bridge")


def debug(msg: str) -> None:
    """Append a line to the debug log if BASH_BRIDGE_DEBUG names a file. Never raises."""
    path = os.environ.get("BASH_BRIDGE_DEBUG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s [%d] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def debug_exc(where: str) -> None:
    if os.environ.get("BASH_BRIDGE_DEBUG"):
        import traceback
        debug("%s failed:\n%s" % (where, traceback.format_exc()))


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def sha_key(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:n]


def safe_id(text: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(text))[:120] or "none"


def now_ms() -> int:
    return int(time.time() * 1000)


def is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)] + "\n[... truncated by bash-bridge ...]"


def eprint(*args) -> None:
    print(*args, file=sys.stderr)
