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
    "max_untracked": 5000,        # first snapshot: more files to hash than this -> switched off
    "first_snapshot_ms": 8000,    # budget for the very first snapshot of a project
    "snapshot_ms": 3000,          # budget for every later snapshot
    "scan_ms": 400,               # budget for the file-count pre-scan of non-git dirs
    "hook_timeout_s": 60,         # cap per bridged hook (their own timeout still applies if lower)
    "hook_budget_s": 240,         # cap for all bridged hooks of one Bash call
    "max_bridged_files": 50,
    "hook_workers": 4,            # files whose hooks run at the same time (1 = one file after another)      # at most this many files get synthesized events per Bash call
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

# A cloned repository must not be able to widen what bash-bridge executes or reads ...
USER_ONLY = {"project_hooks", "sources", "plugin_dirs", "include_match_all", "follow_rule_symlinks", "excludes"}
# ... nor raise its resource limits (it may lower them).
LOWER_ONLY = {"max_untracked", "undo_max_bytes", "store_max_bytes", "keep_entries", "undo_days", "max_file_bytes", "max_files",
              "hook_timeout_s", "hook_budget_s", "first_snapshot_ms", "snapshot_ms", "scan_ms",
              "max_bridged_files", "context_chars", "rule_chars"}

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
            if i > 0 and k in USER_ONLY:
                debug("project config may not set %s" % k)
                continue
            if i > 0 and k in LOWER_ONLY:
                try:
                    if float(v) > float(cfg[k]):
                        debug("project config may only lower %s" % k)
                        continue
                except (TypeError, ValueError):
                    continue
            cfg[k] = v
    # Env vars a project's settings `env` block sets are ignored (except turning bash-bridge off):
    # Claude Code exports them, so they are as untrusted as the repository itself.
    env = {k: v for k in list(os.environ) if k.startswith("BASH_BRIDGE_")
           for v in [trusted_env(k)] if v is not None}
    if os.environ.get("BASH_BRIDGE_DISABLE"):
        env["BASH_BRIDGE_DISABLE"] = os.environ["BASH_BRIDGE_DISABLE"]
    if _truthy(env.get("BASH_BRIDGE_DISABLE", "")):
        cfg["enabled"] = False
    for name, key in _BOOL_ENV.items():
        if name in env:
            cfg[key] = _truthy(env[name])
    for name, key in _INT_ENV.items():
        if name in env:
            try:
                cfg[key] = int(env[name])
            except ValueError:
                pass
    if env.get("BASH_BRIDGE_GUARD"):
        cfg["guard"] = env["BASH_BRIDGE_GUARD"].strip().lower()
    if env.get("BASH_BRIDGE_PROJECT_HOOKS"):
        v = env["BASH_BRIDGE_PROJECT_HOOKS"].strip().lower()
        cfg["project_hooks"] = "trusted" if v == "trusted" else _truthy(v)
    if env.get("BASH_BRIDGE_RULES_ON_READ"):
        cfg["rules_on_read"] = env["BASH_BRIDGE_RULES_ON_READ"].strip().lower()
    if env.get("BASH_BRIDGE_SOURCES"):
        cfg["sources"] = [s.strip() for s in env["BASH_BRIDGE_SOURCES"].split(",") if s.strip()]
    if env.get("BASH_BRIDGE_PLUGIN_DIRS"):
        cfg["plugin_dirs"] = list(cfg.get("plugin_dirs") or []) + [
            p for p in env["BASH_BRIDGE_PLUGIN_DIRS"].split(os.pathsep) if p
        ]
    if cfg.get("guard") not in ("revert", "report", "off"):
        cfg["guard"] = "revert"
    return cfg
