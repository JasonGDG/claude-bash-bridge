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


# umask of the process before secure_state() tightened it; restored for the user's hooks
ORIG_UMASK = None


class UnsafeState(Exception):
    pass


def check_private_dir(path: str) -> None:
    """path must be a real directory (not a symlink) owned by us; group/other bits are removed."""
    st = os.lstat(path)
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise UnsafeState("%s is a symlink or not a directory" % path)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise UnsafeState("%s is owned by another user" % path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def secure_state() -> str:
    """Make every file bash-bridge creates private (umask 077) and validate the state root."""
    global ORIG_UMASK
    if ORIG_UMASK is None:
        ORIG_UMASK = os.umask(0o077)
    root = state_dir()
    if not os.path.lexists(root):
        os.makedirs(root, mode=0o700, exist_ok=True)
    check_private_dir(root)
    for sub in ("projects", "sessions"):
        p = os.path.join(root, sub)
        if os.path.lexists(p):
            check_private_dir(p)
    return root


def restore_umask() -> None:
    """preexec_fn for the user's hooks: they run with the umask Claude Code gave us."""
    if ORIG_UMASK is not None:
        os.umask(ORIG_UMASK)


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
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
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
