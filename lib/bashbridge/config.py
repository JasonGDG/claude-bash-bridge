"""Configuration: defaults < ~/.claude/bash-bridge.json < <project>/.claude/bash-bridge.json < env vars."""
from __future__ import annotations

import os

from .util import claude_config_dir, read_json

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
    "max_file_bytes": 20 * 1024 * 1024,  # bigger untracked files are not snapshotted
    "first_snapshot_ms": 8000,    # budget for the very first snapshot of a project
    "snapshot_ms": 3000,          # budget for every later snapshot
    "scan_ms": 400,               # budget for the file-count pre-scan of non-git dirs
    "hook_timeout_s": 60,         # cap per bridged hook (their own timeout still applies if lower)
    "hook_budget_s": 240,         # cap for all bridged hooks of one Bash call
    "max_bridged_files": 50,      # at most this many files get synthesized events per Bash call
    "context_chars": 9000,        # stay under Claude Code's 10,000-char additionalContext cap
    "rule_chars": 4000,           # per rule / CLAUDE.md file
    "keep_entries": 300,          # undo log entries kept per project
    "excludes": [
        "node_modules/", ".venv/", "venv/", "__pycache__/", ".tox/", ".mypy_cache/",
        ".pytest_cache/", ".ruff_cache/", ".next/", ".nuxt/", ".turbo/", ".gradle/",
        ".DS_Store", "*.pyc", ".cache/",
    ],
}

_BOOL_ENV = {
    "BASH_BRIDGE_HOOKS": "hooks",
    "BASH_BRIDGE_RULES": "rules",
    "BASH_BRIDGE_UNDO": "undo",
    "BASH_BRIDGE_INCLUDE_MATCH_ALL": "include_match_all",
}
_INT_ENV = {
    "BASH_BRIDGE_MAX_FILES": "max_files",
    "BASH_BRIDGE_MAX_FILE_BYTES": "max_file_bytes",
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
    for path in files:
        data = read_json(path, None)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS:
                    cfg[k] = v
    env = os.environ
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
