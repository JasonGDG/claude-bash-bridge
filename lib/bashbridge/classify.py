"""Cheap shell-command heuristics.

``is_read_only(cmd)`` is deliberately conservative: it returns True only when
every simple command is a known reader and nothing is redirected into a file.
Anything it does not understand counts as "may write" and gets a snapshot.
"""
from __future__ import annotations

import os
import re
import shlex

SEPARATORS = {";", "|", "||", "&&", "&", "(", ")", "\n", ";;", "|&"}

READERS = {
    "cat", "head", "tail", "ls", "tree", "grep", "egrep", "fgrep", "rg", "ag", "wc",
    "cut", "tr", "nl", "file", "stat", "du", "df", "pwd", "echo", "printf", "which", "type", "whereis",
    "date", "whoami", "id", "uname", "hostname", "basename", "dirname", "realpath", "readlink", "diff",
    "cmp", "comm", "column", "fold", "od", "xxd", "hexdump", "strings", "md5", "md5sum", "shasum",
    "sha1sum", "sha256sum", "cksum", "test", "[", "true", "false", "sleep", "ps", "printenv", "bat",
    "jq", "cd", "pushd", "popd", "export", "set", "unset", "local", "uniq", "sort", "find", "fd", "sed",
    "git", "command", "lsof", "uptime", "seq", "rev", "expand", "unexpand",
    "zcat", "gzcat", "tac", "look", "fmt",
}

GIT_READ = {
    "status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "ls-tree", "cat-file", "describe",
    "grep", "shortlog", "whatchanged", "merge-base", "rev-list", "show-ref", "for-each-ref",
    "check-ignore", "version", "count-objects", "name-rev", "cherry", "var",
}

# git options and env that make a "reading" subcommand run other programs (diff drivers, textconv,
# gpg, a pager) or read another repository's config
GIT_GLOBAL_WRITE = {"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree", "--namespace", "-p", "--paginate", "-P"}
GIT_ARG_WRITE = {"--ext-diff", "--textconv", "--show-signature", "--filters", "-O", "--open-files-in-pager",
                 "--output", "-o"}
# repository config that lets git diff/log/show/blame/status/... run a program (checked per command)
GIT_HELPER_CONFIG = (r"^(diff\.external|diff\..+\.(command|textconv)|filter\..+\.(clean|smudge|process)"
                     r"|core\.fsmonitor|log\.showsignature|status\.submodulesummary)$")
SAFE_ENV_PREFIX = re.compile(r"^(LC_[A-Z]+|LANG|LANGUAGE|TZ|COLUMNS|LINES|NO_COLOR|TERM|CLICOLOR)=")

FIND_WRITE = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}

VIEWERS = {"cat", "head", "tail", "nl", "bat", "sed", "grep", "rg"}


def tokenize(cmd: str):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def split_commands(tokens):
    cmds, cur = [], []
    for t in tokens:
        if t in SEPARATORS or (t and all(c in ";&|()" for c in t)):
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def _strip_prefix(words):
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    while i < len(words) and words[i] in ("command", "builtin", "time", "nice", "nohup"):
        i += 1
    return words[i:]


def _has_file_redirect(tokens) -> bool:
    for i, t in enumerate(tokens):
        if t in ("&>", "&>>"):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt != "/dev/null":
                return True
            continue
        if t in (">", ">>", ">|", "<>") or (t.startswith(">") and t not in (">&",)):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            prev = tokens[i - 1] if i > 0 else ""
            if nxt in ("&",) or nxt.startswith("&"):
                continue  # >&2
            if nxt == "/dev/null" or nxt.startswith("/dev/std") or nxt == "/dev/tty":
                continue
            if t == ">" and nxt == "&":
                continue
            if prev == "&" and nxt in ("/dev/null",):
                continue
            return True
    return False


def _reader_ok(words, git_dirs=None, cur=None) -> bool:
    # VAR=value in front of a reader may change what it runs (GIT_EXTERNAL_DIFF, LESSOPEN, PAGER ...)
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        if not SAFE_ENV_PREFIX.match(words[i]):
            return False
        i += 1
    words = _strip_prefix(words)
    if not words:
        return True
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog not in READERS:
        return False
    if prog == "git":
        j = 0
        where = cur
        while j < len(args) and args[j].startswith("-"):
            a = args[j]
            if a in GIT_GLOBAL_WRITE or a.split("=")[0] in GIT_GLOBAL_WRITE:
                return False
            if a == "-C":
                if j + 1 >= len(args):
                    return False
                d = os.path.expanduser(args[j + 1])
                where = d if os.path.isabs(d) or where is None else os.path.join(where, d)
                j += 2
                continue
            j += 1
        if j >= len(args):
            return True
        sub = args[j]
        rest = args[j + 1:]
        # only an explicit allow-list counts as read-only; branch/stash/tag/remote/config/worktree can write
        if sub not in GIT_READ:
            return False
        if any(a in GIT_ARG_WRITE or a.split("=")[0] in GIT_ARG_WRITE for a in rest):
            return False
        if sub == "grep" and any(re.match(r"^-[A-Za-z]*O", a) for a in rest):
            return False
        # signature placeholders (%G?, %GS, %GK ...) in --format/--pretty make git run gpg
        if any("%G" in a for a in rest):
            return False
        if git_dirs is not None:
            git_dirs.add(where)
        return True
    if prog == "sed":
        for a in args:
            if a.startswith("--"):
                if a.startswith("--in-place") or a == "--separate":
                    return False
                continue
            if a.startswith("-") and len(a) > 1 and "i" in a[1:].split("e")[0].split("f")[0]:
                return False  # -i, -Ei, -ni, -ri, -i.bak ...
            if a == "-I" or (a.startswith("-") and "I" in a[1:]):
                return False
        script = " ".join(a for a in args if not a.startswith("-"))
        return not re.search(r"(^|[;}\s/])w\s", script) and "/w " not in script
    if prog == "tree":
        return not any(a == "-o" or a.startswith("-o") for a in args)
    if prog == "xxd":
        return len([a for a in args if not a.startswith("-")]) <= 1
    if prog in ("rg", "ag"):
        return not any(a == "--pre" or a.startswith("--pre=") for a in args)
    if prog == "find":
        return not any(a in FIND_WRITE for a in args)
    if prog == "fd":
        return not any(a in ("-x", "-X", "--exec", "--exec-batch") for a in args)
    if prog == "sort":
        return not any(a == "-o" or a.startswith("-o") or a.startswith("--output") or a.startswith("--compress-program")
                       for a in args)
    if prog == "uniq":
        return len([a for a in args if not a.startswith("-")]) <= 1
    if prog in ("jq",):
        return not any(a in ("-i", "--in-place") for a in args)
    if prog == "command":
        return True
    return True


def is_read_only(cmd: str, cwd=None) -> bool:
    """True only if cmd certainly writes no file. git subcommands from GIT_READ count only when the
    repository they run in has no config that makes them start other programs (GIT_HELPER_CONFIG),
    and no GIT_* override is in the environment; that check runs git once per repository."""
    if not cmd or not cmd.strip():
        return True
    if "$(" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd or "<<" in cmd:
        return False
    try:
        tokens = tokenize(cmd)
    except ValueError:
        return False
    if _has_file_redirect(tokens):
        return False
    base = cwd or os.getcwd()
    cur = base
    git_dirs = set()
    for words in split_commands(tokens):
        w = _strip_prefix(words)
        if w and w[0] in ("cd", "pushd"):
            if len(w) > 1:
                nd = os.path.expanduser(w[1])
                cur = nd if os.path.isabs(nd) else os.path.normpath(os.path.join(cur, nd))
            else:
                cur = os.path.expanduser("~")
        if not _reader_ok(words, git_dirs, cur):
            return False
    return all(git_runs_no_helpers(d) for d in git_dirs)


_GIT_ENV_HELPERS = ("GIT_EXTERNAL_DIFF", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL",
                    "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE", "GIT_PAGER")
_HELPER_CACHE = {}


def git_runs_no_helpers(where) -> bool:
    """True if git in `where` has no config (any level) that runs a program for diff/log/show/status."""
    if any(os.environ.get(k) for k in _GIT_ENV_HELPERS):
        return False
    where = where or os.getcwd()
    if where in _HELPER_CACHE:
        return _HELPER_CACHE[where]
    import subprocess
    env = dict(os.environ, LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(["git", "-C", where, "config", "--get-regexp", GIT_HELPER_CONFIG],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=3)
    except (OSError, subprocess.SubprocessError):
        _HELPER_CACHE[where] = False
        return False
    ok = False
    if p.returncode == 1 and not p.stdout:
        ok = True  # no such key anywhere
    elif p.returncode == 0:
        ok = True
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            key, _sp, val = line.partition(" ")
            val = val.strip().lower()
            # the built-in fsmonitor daemon (a boolean) writes only inside .git; a hook path runs a program
            if key == "core.fsmonitor" and val in ("true", "false", "0", "1", "yes", "no", "on", "off", ""):
                continue
            if key in ("log.showsignature", "status.submodulesummary") and val in ("false", "0", "no", "off"):
                continue
            ok = False
            break
    _HELPER_CACHE[where] = ok
    return ok


def invokes_bash_bridge(cmd: str) -> bool:
    return bool(re.search(r"(^|[\s/;&|'\"])bash-bridge(\s|$|['\"])", cmd or ""))


def is_plain_single_view(cmd: str) -> bool:
    """A single `cat file` / `head -5 file` style command. Claude Code >= 2.1.293
    loads rules for these itself, so rules_on_read=auto skips them."""
    try:
        tokens = tokenize(cmd)
    except ValueError:
        return False
    if any(t in SEPARATORS or t.startswith(">") or t.startswith("<") for t in tokens):
        return False
    words = _strip_prefix(tokens)
    if not words or os.path.basename(words[0]) not in VIEWERS:
        return False
    if any(a.startswith("-") and a not in ("-n",) and not re.match(r"^-\d+$", a) for a in words[1:]) \
            and os.path.basename(words[0]) in ("cat",):
        return False
    files = [a for a in words[1:] if not a.startswith("-")]
    return len(files) <= 2


def read_targets(cmd: str, cwd: str, limit: int = 20):
    """Existing files a read-only command names, resolved against cwd (tracks `cd`)."""
    try:
        tokens = tokenize(cmd)
    except ValueError:
        return []
    out = []
    cur = cwd
    for words in split_commands(tokens):
        words = _strip_prefix(words)
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog in ("cd", "pushd") and len(words) > 1:
            nd = os.path.expanduser(words[1])
            cur = nd if os.path.isabs(nd) else os.path.normpath(os.path.join(cur, nd))
            continue
        for a in words[1:]:
            if a.startswith("-") or a in SEPARATORS:
                continue
            p = os.path.expanduser(a)
            p = p if os.path.isabs(p) else os.path.join(cur, p)
            try:
                if os.path.isfile(p):
                    rp = os.path.abspath(p)
                    if rp not in out:
                        out.append(rp)
            except (OSError, ValueError):
                continue
            if len(out) >= limit:
                return out
    return out
