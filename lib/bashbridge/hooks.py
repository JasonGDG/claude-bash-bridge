"""Collect the user's Write/Edit hooks from all settings sources and plugins and run them
for synthesized tool events."""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time

from .util import PLUGIN_ROOT, claude_config_dir, debug, read_json, real_home, restore_umask

EXACT_MATCHER = re.compile(r"^[A-Za-z0-9_\- ,|]*$")
MANAGED_SETTINGS = "/Library/Application Support/ClaudeCode/managed-settings.json"


def matcher_matches(matcher, tool: str) -> bool:
    if matcher is None or matcher in ("", "*"):
        return True
    m = str(matcher)
    if EXACT_MATCHER.match(m):
        names = [x.strip() for x in re.split(r"[|,]", m) if x.strip()]
        return tool in names
    try:
        return re.search(m, tool) is not None
    except re.error:
        return False


def is_match_all(matcher) -> bool:
    return matcher is None or matcher in ("", "*")


def managed_settings_path() -> str:
    return os.environ.get("BASH_BRIDGE_MANAGED_SETTINGS") or MANAGED_SETTINGS


def project_trusted(project_dir: str) -> bool:
    """True if Claude Code recorded the workspace trust dialog as accepted for project_dir or a parent."""
    # $CLAUDE_CONFIG_DIR only counts when the project's own settings did not set it (trusted_env)
    # and it does not point into the project; the account's real ~/.claude.json always counts.
    cands = []
    cfg_dir = claude_config_dir()
    if os.path.realpath(cfg_dir) != os.path.realpath(os.path.join(real_home(), ".claude")):
        cands.append(os.path.join(cfg_dir, ".claude.json"))
    cands.append(os.path.join(real_home(), ".claude.json"))
    real = os.path.realpath(project_dir)
    for path in cands:
        data = read_json(path, None)
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            continue
        for p, v in projects.items():
            if not (isinstance(v, dict) and v.get("hasTrustDialogAccepted") is True):
                continue
            rp = os.path.realpath(p)
            if real == rp or real.startswith(rp.rstrip(os.sep) + os.sep):
                return True
    return False


def settings_files(project_dir: str, sources):
    files = []
    if "managed" in sources:
        files.append(("managed", managed_settings_path()))
    if "user" in sources:
        files.append(("user", os.path.join(claude_config_dir(), "settings.json")))
    if project_dir and "project" in sources:
        files.append(("project", os.path.join(project_dir, ".claude", "settings.json")))
    if project_dir and "local" in sources:
        files.append(("local", os.path.join(project_dir, ".claude", "settings.local.json")))
    return files


def _plugin_id_sanitized(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "-", pid)


def enabled_plugins(project_dir: str, sources, settings_data):
    enabled = {}
    for _src, data in settings_data:
        ep = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(ep, dict):
            for k, v in ep.items():
                enabled[k] = bool(v)
    installed = read_json(os.path.join(claude_config_dir(), "plugins", "installed_plugins.json"), {}) or {}
    plugs = installed.get("plugins", {}) if isinstance(installed, dict) else {}
    res = []
    for pid, on in enabled.items():
        if not on:
            continue
        recs = plugs.get(pid) or []
        chosen = None
        for r in recs:
            if not isinstance(r, dict):
                continue
            if r.get("scope") in ("project", "local") and r.get("projectPath"):
                if project_dir and os.path.realpath(r["projectPath"]) == os.path.realpath(project_dir):
                    chosen = r
                    break
                continue
            chosen = chosen or r
        if chosen and chosen.get("installPath") and os.path.isdir(chosen["installPath"]):
            res.append((pid, chosen["installPath"]))
    return res


def _plugin_hook_maps(root: str):
    maps = []
    hj = read_json(os.path.join(root, "hooks", "hooks.json"), None)
    if isinstance(hj, dict) and isinstance(hj.get("hooks"), dict):
        maps.append(hj["hooks"])
    manifest = read_json(os.path.join(root, ".claude-plugin", "plugin.json"), None)
    if isinstance(manifest, dict) and manifest.get("hooks"):
        decl = manifest["hooks"]
        decl = decl if isinstance(decl, list) else [decl]
        for d in decl:
            if isinstance(d, str):
                p = os.path.normpath(os.path.join(root, d))
                if os.path.abspath(p) == os.path.abspath(os.path.join(root, "hooks", "hooks.json")):
                    continue
                f = read_json(p, None)
                if isinstance(f, dict) and isinstance(f.get("hooks"), dict):
                    maps.append(f["hooks"])
            elif isinstance(d, dict):
                maps.append(d)
    return maps


def _is_self(root: str, handler: dict) -> bool:
    if root and os.path.realpath(root) == PLUGIN_ROOT:
        return True
    blob = " ".join([str(handler.get("command", ""))] + [str(a) for a in handler.get("args", []) or []])
    return "bash-bridge" in blob or "bashbridge" in blob or PLUGIN_ROOT in blob


class Handler:
    def __init__(self, event, matcher, spec, source, plugin_root=None, plugin_id=None):
        self.event = event
        self.matcher = matcher
        self.spec = spec
        self.source = source
        self.plugin_root = plugin_root
        self.plugin_id = plugin_id

    def label(self) -> str:
        cmd = self.spec.get("command", "")
        if self.spec.get("args"):
            cmd += " " + " ".join(str(a) for a in self.spec["args"])
        return "%s [%s]" % (cmd.strip()[:160], self.source)


def collect(event: str, project_dir: str, cfg: dict):
    """All command handlers registered for `event`, with source info."""
    sources = cfg.get("sources") or []
    files = settings_files(project_dir, sources)
    data = []
    for src, path in files:
        d = read_json(path, None)
        if isinstance(d, dict):
            data.append((src, d))
    managed = [d for s, d in data if s == "managed"]
    if any(d.get("disableAllHooks") is True for d in managed):
        debug("managed disableAllHooks: no hooks bridged")
        return []
    disable_all = any(d.get("disableAllHooks") is True for s, d in data if s != "managed")
    managed_only = any(d.get("allowManagedHooksOnly") is True for d in managed)
    ph = cfg.get("project_hooks", "trusted")
    allow_project = ph is True or (ph == "trusted" and bool(project_dir) and project_trusted(project_dir))
    if not allow_project and any(s in ("project", "local") for s, _d in data):
        debug("project/local hooks not bridged: project not trusted (project_hooks=%s)" % ph)
    handlers, seen = [], set()

    def add_map(hmap, source, root=None, pid=None):
        groups = hmap.get(event) if isinstance(hmap, dict) else None
        if not isinstance(groups, list):
            return
        for g in groups:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type", "command") != "command" or not h.get("command"):
                    continue
                if _is_self(root, h):
                    continue
                key = (json.dumps(h.get("command")), json.dumps(h.get("args")), root or "")
                if root is None and key in seen:
                    continue
                seen.add(key)
                handlers.append(Handler(event, g.get("matcher"), h, source, root, pid))

    for src, d in data:
        if (disable_all or managed_only) and src != "managed":
            continue
        if src in ("project", "local") and not allow_project:
            continue
        add_map(d.get("hooks") or {}, src)
    if "plugins" in sources and not disable_all and not managed_only:
        plug_data = [(s_, d_) for s_, d_ in data if allow_project or s_ not in ("project", "local")]
        for pid, root in enabled_plugins(project_dir, sources, plug_data):
            for hmap in _plugin_hook_maps(root):
                add_map(hmap, "plugin:" + pid.split("@")[0], root, pid)
        for root in cfg.get("plugin_dirs") or []:
            root = os.path.abspath(os.path.expanduser(root))
            if os.path.isdir(root):
                for hmap in _plugin_hook_maps(root):
                    add_map(hmap, "plugin-dir:" + os.path.basename(root), root, os.path.basename(root))
    return handlers


def select_for_tool(handlers, tool: str, cfg: dict):
    res = []
    for h in handlers:
        if not matcher_matches(h.matcher, tool):
            continue
        if is_match_all(h.matcher):
            if not cfg.get("include_match_all"):
                continue  # already ran for the Bash call itself
        elif matcher_matches(h.matcher, "Bash"):
            continue  # matcher covers Bash too: it already ran
        res.append(h)
    return res


def if_matches(cond, tool: str, file_path: str, cwd: str) -> bool:
    """Best-effort evaluation of a handler's `if` (permission-rule syntax)."""
    if not cond:
        return True
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?\s*$", str(cond))
    if not m:
        debug("unsupported `if` condition %r: hook not run" % (cond,))
        return False
    name, pat = m.group(1), m.group(2)
    if name not in ("Edit", "Write", "MultiEdit", tool):
        return False
    if pat is None or pat.strip() in ("", "*", "**"):
        return True
    from .rules import glob_match
    pat = pat.strip()
    if pat.startswith("//") or pat.startswith("~/"):
        absolute = os.path.expanduser(pat[1:] if pat.startswith("//") else pat)
        return glob_match(absolute.lstrip("/"), os.path.abspath(file_path).lstrip("/"))
    if pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    try:
        target = os.path.relpath(file_path, cwd).replace(os.sep, "/")
    except ValueError:
        target = file_path
    if "/" not in pat:
        return glob_match(pat, os.path.basename(file_path))
    return glob_match(pat, target)


def _subst(text: str, env: dict) -> str:
    for var in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR"):
        if var in env:
            text = text.replace("${%s}" % var, env[var])
    return text


class Result:
    def __init__(self, handler, code, stdout, stderr, timed_out=False, error=None, skipped=False):
        self.handler = handler
        self.skipped = skipped  # not started: the hook budget was used up
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.error = error
        self.json = None
        s = (stdout or "").strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                self.json = json.loads(s)
            except ValueError:
                self.json = None


def run_handler(h: Handler, payload: dict, project_dir: str, cwd: str, timeout_cap: float,
                extra_env=None) -> Result:
    env = dict(os.environ)
    env.update(extra_env or {})
    env["BASH_BRIDGE_ACTIVE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = project_dir
    if h.plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = h.plugin_root
        env["CLAUDE_PLUGIN_DATA"] = os.path.join(claude_config_dir(), "plugins", "data",
                                                 _plugin_id_sanitized(h.plugin_id or os.path.basename(h.plugin_root)))
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env.pop("CLAUDE_PLUGIN_DATA", None)
    spec = h.spec
    if spec.get("shell") == "powershell":
        return Result(h, None, "", "", error="powershell hooks are not bridged")
    if spec.get("args"):
        # exec form: no shell, placeholders become plain argv strings
        argv = [_subst(str(spec["command"]), env)] + [_subst(str(a), env) for a in spec["args"]]
    else:
        # shell form: leave ${CLAUDE_PLUGIN_ROOT} etc. for bash to expand from the environment, so a
        # path containing quotes, $() or backticks can never become shell syntax
        argv = ["/bin/bash", "-c", str(spec["command"])]
    try:
        own = float(spec.get("timeout") or 600)
    except (TypeError, ValueError):
        own = 600.0
    timeout = max(0.5, min(own, timeout_cap))
    data = json.dumps(payload).encode("utf-8")
    run_cwd = cwd if os.path.isdir(cwd) else project_dir
    if spec.get("async") or spec.get("asyncRewake"):
        try:
            p = subprocess.Popen(argv, cwd=run_cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=restore_umask)
            try:
                p.stdin.write(data)
                p.stdin.close()
            except OSError:
                pass
            return Result(h, 0, "", "")
        except OSError as e:
            return Result(h, None, "", "", error=str(e))
    try:
        p = subprocess.Popen(argv, cwd=run_cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True, preexec_fn=restore_umask)
    except OSError as e:
        return Result(h, None, "", "", error=str(e))
    try:
        out, err = p.communicate(data, timeout=timeout)
        return Result(h, p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=2)
        except Exception:
            pass
        return Result(h, None, "", "", timed_out=True)


KILL_GRACE_S = 3.0


def run_parallel(handlers, payload, project_dir, cwd, timeout_cap, extra_env=None, deadline=None):
    """Run one file's handlers at the same time. Each gets min(its own timeout, timeout_cap, time left
    until deadline); run_handler kills it then, so every thread ends within that plus KILL_GRACE_S.
    A handler that would start after the deadline is reported as skipped."""
    results = [None] * len(handlers)
    left = timeout_cap if deadline is None else min(timeout_cap, deadline - time.time())
    if left <= 0:
        return [Result(h, None, "", "", skipped=True) for h in handlers]

    def work(i, h):
        try:
            results[i] = run_handler(h, payload, project_dir, cwd, left, extra_env)
        except Exception as e:  # pragma: no cover - defensive
            results[i] = Result(h, None, "", "", error=str(e))

    threads = [threading.Thread(target=work, args=(i, h), daemon=True) for i, h in enumerate(handlers)]
    for t in threads:
        t.start()
    end = time.time() + max(0.5, left) + KILL_GRACE_S
    for t in threads:
        t.join(max(0.1, end - time.time()))
    return [r if r is not None else Result(h, None, "", "", timed_out=True) for h, r in zip(handlers, results)]


def interpret_post(r: Result):
    """-> dict(block=reason|None, context=str|None, system=str|None, stop=str|None)"""
    out = {"block": None, "context": None, "system": None, "stop": None}
    j = r.json if isinstance(r.json, dict) else None
    if r.code == 2:
        reason = None
        if j and j.get("decision") == "block":
            reason = j.get("reason")
        out["block"] = (reason or r.stderr or "blocked (exit code 2)").strip()
    elif r.code == 0 and j:
        if j.get("decision") == "block":
            out["block"] = str(j.get("reason") or "blocked").strip()
    if j:
        hso = j.get("hookSpecificOutput") or {}
        if isinstance(hso, dict) and hso.get("additionalContext"):
            out["context"] = str(hso["additionalContext"])
        if j.get("systemMessage"):
            out["system"] = str(j["systemMessage"])
        if j.get("continue") is False:
            out["stop"] = str(j.get("stopReason") or "stopped by hook")
    return out


def interpret_pre(r: Result):
    """-> dict(deny=reason|None, ask=reason|None, context=str|None)"""
    out = {"deny": None, "ask": None, "context": None}
    j = r.json if isinstance(r.json, dict) else None
    hso = (j or {}).get("hookSpecificOutput") or {}
    if not isinstance(hso, dict):
        hso = {}
    if r.code == 2:
        out["deny"] = (hso.get("permissionDecisionReason") or r.stderr or "denied (exit code 2)").strip()
        return out
    if r.code not in (0, None) and not j:
        return out
    if j:
        dec = hso.get("permissionDecision") or {"block": "deny", "approve": "allow"}.get(j.get("decision"))
        reason = hso.get("permissionDecisionReason") or j.get("reason") or ""
        if dec == "deny":
            out["deny"] = str(reason or "denied").strip()
        elif dec == "ask":
            out["ask"] = str(reason or "the hook asks for confirmation").strip()
        if hso.get("additionalContext"):
            out["context"] = str(hso["additionalContext"])
    return out


def run_many(tasks, project_dir, cwd, timeout_cap, extra_env=None, workers: int = 4, deadline=None):
    """tasks: [(key, handlers, payload)]. Runs the files in parallel (a small pool, each file's
    handlers in parallel too) and returns [(key, results)] in the order of tasks. deadline is one
    absolute time for all of them: no handler starts after it, and running ones are cut off at it."""
    if not tasks:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks)))) as ex:
        futs = [ex.submit(run_parallel, hs, payload, project_dir, cwd, timeout_cap, extra_env, deadline)
                for _key, hs, payload in tasks]
        return [(tasks[i][0], f.result()) for i, f in enumerate(futs)]
