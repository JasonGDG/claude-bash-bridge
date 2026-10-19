"""Configuration: defaults < ~/.claude/bash-bridge.json < <project>/.claude/bash-bridge.json < env vars."""
from __future__ import annotations

import os

from .util import claude_config_dir, debug, read_json, trusted_env

DEFAULTS = {
    "enabled": True,
    # Run your PostToolUse hooks for Write/Edit on files a Bash call changed.
    "hooks": True,
    # Run your PreToolUse hooks for Write/Edit after the fact. "revert": a denied
    # change is rolled back; "report": only tell Claude; "off": skip.
    "guard": "revert",
    # Also bridge hooks whose matcher is "*" / "" (they already ran for Bash).
    "include_match_all": False,
    # Inject path-scoped rules / nested CLAUDE.md for changed files.
    "rules": True,
    # Rules for files a read-only Bash command looked at: "auto" skips the plain
    # single-file views Claude Code already handles itself, "all", or "off".
    "rules_on_read": "auto",
    # Keep pre-images so `bash-bridge undo` works.
    "undo": True,
    # Which settings sources to collect hooks from.
    "sources": ["managed", "user", "project", "local", "plugins"],
    # Extra plugin directories (for plugins loaded with --plugin-dir).
    "plugin_dirs": [],
    # Limits.
    "max_files": 100000,          # tracked + untracked files in the worktree
    "max_file_bytes": 20 * 1024 * 1024,  # bigger files are not snapshotted ("too large")
    "max_untracked": 5000,        # more untracked (not ignored) files: a call snapshots tracked files only
    "first_snapshot_ms": 8000,    # budget for the very first snapshot of a project
    "snapshot_ms": 3000,          # budget for every later snapshot
    "scan_ms": 400,               # budget for the file-count pre-scan of non-git dirs
    "hook_timeout_s": 60,         # cap per bridged hook (their own timeout still applies if lower)
    "hook_budget_s": 180,         # one deadline for all bridged hooks of one Bash call (hooks.json allows 300 s)
    "max_bridged_files": 50,      # at most this many files get synthesized events per Bash call
    "hook_workers": 4,            # files whose hooks run at the same time (1 = one file after another)
    # 0 = a project that is too big or too slow is skipped for 10 minutes, then measured again.
    # More = skip it for that many hours (only from ~/.claude/bash-bridge.json).
    "disable_hours": 0,
    "context_chars": 9000,        # stay under Claude Code's 10,000-char additionalContext cap
    "rule_chars": 4000,           # per rule / CLAUDE.md file
    "keep_entries": 300,          # undo log entries kept per project
    "undo_days": 30,              # ...and for at most this many days (pre-images go with them)
    "undo_max_bytes": 200 * 1024 * 1024,  # pre-images kept per Bash call
    "store_max_bytes": 1024 * 1024 * 1024,  # all pre-images of a project; oldest entries go first
    # Hooks from .claude/settings.json / settings.local.json run only in a project Claude Code
    # has marked trusted (hasTrustDialogAccepted in ~/.claude.json). true = always, false = never.
    "project_hooks": "trusted",
    # Follow symlinks in .claude/rules that point outside the rules folder.
    "follow_rule_symlinks": False,
    "excludes": [
        "node_modules/", ".venv/", "venv/", "__pycache__/", ".tox/", ".mypy_cache/",
        ".pytest_cache/", ".ruff_cache/", ".next/", ".nuxt/", ".turbo/", ".gradle/",
        ".DS_Store", "*.pyc", ".cache/",
    ],
}

# A cloned repository must not be able to widen what bash-bridge executes or reads, switch its
# protection off piecemeal, or make it give up (a lower max_untracked/max_files/max_file_bytes or
# snapshot budget would make a project "too big" and turn everything off) ...
USER_ONLY = {"project_hooks", "sources", "plugin_dirs", "include_match_all", "follow_rule_symlinks", "excludes",
             "hooks", "guard", "rules", "undo", "disable_hours", "max_untracked", "max_file_bytes", "max_files",
             "first_snapshot_ms", "snapshot_ms", "scan_ms", "max_bridged_files"}
# ... nor raise its resource limits. It may lower these, down to PROJECT_FLOOR. "enabled": false is
# allowed (a repository may opt out of bash-bridge as a whole), but the user and Claude are told.
LOWER_ONLY = {"undo_max_bytes", "store_max_bytes", "keep_entries", "undo_days", "hook_timeout_s", "hook_budget_s",
              "context_chars", "rule_chars", "hook_workers"}

MB = 1024 * 1024
# (type, min, max) for every number; values outside are replaced by the default (debug note)
RANGES = {
    "max_files": (int, 1, 10 ** 7),
    "max_file_bytes": (int, 1, 1 << 40),
    "max_untracked": (int, 1, 10 ** 7),
    "first_snapshot_ms": (int, 100, 600000),
    "snapshot_ms": (int, 100, 600000),
    "scan_ms": (int, 10, 60000),
    "hook_timeout_s": (float, 1, 600),
    "hook_budget_s": (float, 1, 200),   # leaves >= 100 s of hooks.json's 300 s for snapshots and undo
    "max_bridged_files": (int, 1, 10000),
    "hook_workers": (int, 1, 16),
    "disable_hours": (float, 0, 24 * 30),
    "context_chars": (int, 500, 9500),
    "rule_chars": (int, 100, 9000),
    "keep_entries": (int, 1, 100000),
    "undo_days": (float, 0.01, 3650),
    "undo_max_bytes": (int, 0, 1 << 40),
    "store_max_bytes": (int, 0, 1 << 42),
}
PROJECT_FLOOR = {"hook_timeout_s": 10, "hook_budget_s": 30, "keep_entries": 10, "undo_days": 1,
                 "undo_max_bytes": 10 * MB, "store_max_bytes": 100 * MB, "context_chars": 2000, "rule_chars": 500,
                 "hook_workers": 1}
BOOLS = {"enabled", "hooks", "rules", "undo", "include_match_all", "follow_rule_symlinks"}
ENUMS = {"guard": ("revert", "report", "off"), "rules_on_read": ("auto", "all", "off")}
KNOWN_SOURCES = ("managed", "user", "project", "local", "plugins")


def _number(key, v):
    """v as the key's type if it is a number (or numeric string) in range, else None."""
    typ, lo, hi = RANGES[key]
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x < lo or x > hi:
        return None
    if typ is int:
        if x != int(x):
            return None
        return int(x)
    return x


def _valid(key, v):
    """(ok, normalized value)."""
    if key in RANGES:
        x = _number(key, v)
        return x is not None, x
    if key in BOOLS:
        return isinstance(v, bool), v
    if key in ENUMS:
        x = str(v).strip().lower() if isinstance(v, str) else None
        return x in ENUMS[key], x
    if key == "project_hooks":
        if isinstance(v, bool):
            return True, v
        return v == "trusted", v
    if key == "sources":
        ok = isinstance(v, list) and all(isinstance(x, str) and x in KNOWN_SOURCES for x in v)
        return ok, list(v) if ok else None
    if key in ("plugin_dirs", "excludes"):
        ok = isinstance(v, list) and all(isinstance(x, str) and x for x in v)
        return ok, list(v) if ok else None
    return False, None


_BOOL_ENV = {
    "BASH_BRIDGE_HOOKS": "hooks",
    "BASH_BRIDGE_RULES": "rules",
    "BASH_BRIDGE_UNDO": "undo",
    "BASH_BRIDGE_INCLUDE_MATCH_ALL": "include_match_all",
    "BASH_BRIDGE_FOLLOW_RULE_SYMLINKS": "follow_rule_symlinks",
}
_INT_ENV = {
    "BASH_BRIDGE_MAX_FILES": "max_files",
    "BASH_BRIDGE_MAX_FILE_BYTES": "max_file_bytes",
    "BASH_BRIDGE_MAX_UNTRACKED": "max_untracked",
    "BASH_BRIDGE_FIRST_SNAPSHOT_MS": "first_snapshot_ms",
    "BASH_BRIDGE_SNAPSHOT_MS": "snapshot_ms",
    "BASH_BRIDGE_HOOK_TIMEOUT_S": "hook_timeout_s",
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load(project_dir: str | None) -> dict:
    cfg = dict(DEFAULTS)
    files = [os.path.join(claude_config_dir(), "bash-bridge.json")]
    if project_dir:
        files.append(os.path.join(project_dir, ".claude", "bash-bridge.json"))
    for i, path in enumerate(files):
        data = read_json(path, None)
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if k not in DEFAULTS:
                continue
            ok, v = _valid(k, v)
            if not ok:
                debug("%s: invalid value for %s, using %r" % (path, k, cfg[k]))
                continue
            if i > 0 and k == "enabled":
                if v is False:
                    cfg["enabled"] = False
                    cfg["disabled_by"] = path
                continue
            if i > 0 and k in USER_ONLY:
                debug("project config may not set %s" % k)
                continue
            if i > 0 and k in LOWER_ONLY:
                if v > cfg[k]:
                    debug("project config may only lower %s" % k)
                    continue
                if v < PROJECT_FLOOR.get(k, 0):
                    debug("project config may not lower %s below %s" % (k, PROJECT_FLOOR[k]))
                    v = PROJECT_FLOOR[k] if PROJECT_FLOOR[k] <= cfg[k] else cfg[k]
            cfg[k] = v
    # Env vars a project's settings `env` block sets are ignored (except turning bash-bridge off):
    # Claude Code exports them, so they are as untrusted as the repository itself.
    env = {k: v for k in list(os.environ) if k.startswith("BASH_BRIDGE_")
           for v in [trusted_env(k)] if v is not None}
    if _truthy(env.get("BASH_BRIDGE_DISABLE", "")):
        cfg["enabled"] = False
    elif _truthy(os.environ.get("BASH_BRIDGE_DISABLE", "")):
        cfg["enabled"] = False
        cfg["disabled_by"] = "BASH_BRIDGE_DISABLE in the project's .claude/settings env"
    for name, key in _BOOL_ENV.items():
        if name in env:
            cfg[key] = _truthy(env[name])
    for name, key in _INT_ENV.items():
        if name in env:
            x = _number(key, env[name])
            if x is None:
                debug("invalid %s=%r ignored" % (name, env[name]))
            else:
                cfg[key] = x
    if env.get("BASH_BRIDGE_GUARD"):
        ok, v = _valid("guard", env["BASH_BRIDGE_GUARD"])
        if ok:
            cfg["guard"] = v
    if env.get("BASH_BRIDGE_PROJECT_HOOKS"):
        v = env["BASH_BRIDGE_PROJECT_HOOKS"].strip().lower()
        cfg["project_hooks"] = "trusted" if v == "trusted" else _truthy(v)
    if env.get("BASH_BRIDGE_RULES_ON_READ"):
        ok, v = _valid("rules_on_read", env["BASH_BRIDGE_RULES_ON_READ"])
        if ok:
            cfg["rules_on_read"] = v
    if env.get("BASH_BRIDGE_SOURCES"):
        cfg["sources"] = [s.strip() for s in env["BASH_BRIDGE_SOURCES"].split(",") if s.strip()]
    if env.get("BASH_BRIDGE_PLUGIN_DIRS"):
        cfg["plugin_dirs"] = list(cfg.get("plugin_dirs") or []) + [
            p for p in env["BASH_BRIDGE_PLUGIN_DIRS"].split(os.pathsep) if p
        ]
    return cfg
