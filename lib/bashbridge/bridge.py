"""Hook entry points: PreToolUse(Bash) snapshot, PostToolUse(Bash) diff + bridge."""
from __future__ import annotations

import os
import time

from . import __version__
from . import config as config_mod
from .classify import invokes_bash_bridge, is_plain_single_view, is_read_only, read_targets
from .hooks import collect, if_matches, interpret_post, interpret_pre, run_parallel, select_for_tool, settings_files
from .rules import RuleSet, build_context, claude_md_excludes, loaded_in_transcript
from .snapshot import Disabled, SnapshotError, Store, find_git_toplevel, remove_path, write_blob_to
from .util import debug, is_within, read_json, safe_id, state_dir, truncate, write_json_atomic

TEXT_CAP = 256 * 1024
READ_CAP = 2 * 1024 * 1024
STATUS_WORD = {"A": "created", "M": "modified", "T": "modified", "D": "deleted"}


# --------------------------------------------------------------------------- context
def resolve(event):
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    project_dir = os.path.realpath(project_dir)
    top = find_git_toplevel(project_dir)
    worktree = os.path.realpath(top) if top else project_dir
    return project_dir, worktree


def session_path(sid: str) -> str:
    return os.path.join(state_dir(), "sessions", safe_id(sid) + ".json")


def load_session(sid: str) -> dict:
    d = read_json(session_path(sid), None)
    if not isinstance(d, dict):
        d = {}
    d.setdefault("injected", [])
    d.setdefault("notes", [])
    return d


def save_session(sid: str, data: dict) -> None:
    data["updated"] = time.time()
    write_json_atomic(session_path(sid), data)


# --------------------------------------------------------------------------- synthesis
def _decode(b):
    if b is None or b"\0" in b[:8192]:
        return None
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_file(path):
    try:
        if os.path.getsize(path) > READ_CAP:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def edit_strings(old: str, new: str):
    a = old.splitlines(True)
    b = new.splitlines(True)
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    s = 0
    while s < len(a) - p and s < len(b) - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    lo, hi_a, hi_b = p, len(a) - s, len(b) - s
    if lo == hi_a:  # pure insertion: anchor on a neighbouring line so old_string is not empty
        if lo > 0:
            lo -= 1
        elif hi_a < len(a):
            hi_a += 1
            hi_b += 1
    return "".join(a[lo:hi_a]), "".join(b[lo:hi_b])


def synthesize(store, pre_tree, status, rel, abs_path):
    """-> (tool_name, tool_input, tool_response, change_word)"""
    new_bytes = _read_file(abs_path)
    new_text = _decode(new_bytes)
    old_text = None
    if status != "A":
        info = store.entry_info(pre_tree, rel)
        if info and store.blob_size(info[1]) <= READ_CAP:
            old_text = _decode(store.read_blob(info[1]))
    word = STATUS_WORD.get(status, "modified")
    if status != "A" and old_text is not None and new_text is not None:
        o, n = edit_strings(old_text, new_text)
        if len(o) <= 100000 and len(n) <= 100000:
            ti = {"file_path": abs_path, "old_string": o, "new_string": n, "replace_all": False}
            tr = {"filePath": abs_path, "oldString": o, "newString": n, "originalFile": None,
                  "structuredPatch": [], "userModified": False, "replaceAll": False}
            return "Edit", ti, tr, word
    content = new_text if (new_text is not None and len(new_text) <= TEXT_CAP) else ""
    ti = {"file_path": abs_path, "content": content}
    tr = {"type": "create" if status == "A" else "update", "filePath": abs_path, "content": content,
          "structuredPatch": []}
    return "Write", ti, tr, word


def payload_for(event, hook_event, tool, ti, tr, n, word, cmd):
    p = {}
    for k in ("session_id", "transcript_path", "cwd", "permission_mode", "prompt_id", "effort", "agent_id",
              "agent_type", "scratchpad_dir"):
        if k in event:
            p[k] = event[k]
    p["hook_event_name"] = hook_event
    p["tool_name"] = tool
    p["tool_input"] = ti
    if tr is not None:
        p["tool_response"] = tr
    p["tool_use_id"] = "%s-bash-bridge-%d" % (event.get("tool_use_id") or "toolu", n)
    p["bash_bridge"] = {"synthetic": True, "version": __version__, "source_tool": "Bash", "change": word,
                        "command": truncate(cmd or "", 2000)}
    return p


# --------------------------------------------------------------------------- handlers
def handle_pre(event, cfg):
    ti = event.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if event.get("tool_name") != "Bash" or ti.get("run_in_background"):
        return None
    if invokes_bash_bridge(cmd) or is_read_only(cmd):
        return None
    if not (cfg["hooks"] or cfg["rules"] or cfg["undo"] or cfg["guard"] != "off"):
        return None
    project_dir, worktree = resolve(event)
    store = Store(worktree)
    tuid = event.get("tool_use_id") or "unknown"
    t0 = time.time()
    try:
        with store.lock(timeout=5.0):
            first = not store.meta().get("ready")
            store.ensure(cfg)
            tree = store.snapshot(cfg, first=first)
    except Disabled as e:
        store.save_pending(tuid, {"disabled": str(e)})
        return None
    store.save_pending(tuid, {"tree": tree, "ts": time.time(), "project_dir": project_dir})
    debug("pre %s: tree %s in %d ms" % (tuid, tree[:10], (time.time() - t0) * 1000))
    return None


def _rel(path, project_dir, worktree):
    base = project_dir if is_within(path, project_dir) else worktree
    return os.path.relpath(path, base)


def handle_post(event, cfg, failure=False):
    ti = event.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if event.get("tool_name") != "Bash" or ti.get("run_in_background") or invokes_bash_bridge(cmd):
        return None
    hook_event = "PostToolUseFailure" if failure else "PostToolUse"
    project_dir, worktree = resolve(event)
    store = Store(worktree)
    tuid = event.get("tool_use_id") or "unknown"
    sid = event.get("session_id") or "nosession"
    pending = store.pop_pending(tuid) if os.path.isdir(store.dir) else None

    if pending is None:
        if is_read_only(cmd):
            return _read_rules(event, cfg, cmd, project_dir, sid, hook_event)
        return None
    if pending.get("disabled"):
        sess = load_session(sid)
        note = "disabled:" + store.worktree
        if note in sess["notes"]:
            return None
        sess["notes"].append(note)
        save_session(sid, sess)
        return {"systemMessage": "bash-bridge is inactive for %s: %s. Hooks, rules and undo are not "
                                 "bridged for Bash changes here." % (store.worktree, pending["disabled"])}
    pre_tree = pending["tree"]
    t0 = time.time()
    try:
        with store.lock(timeout=10.0):
            post_tree = store.snapshot(cfg)
            changes = store.diff(pre_tree, post_tree)
    except Disabled:
        return None
    if not changes:
        debug("post %s: no changes (%d ms)" % (tuid, (time.time() - t0) * 1000))
        return None
    debug("post %s: %d changes %s" % (tuid, len(changes), changes[:5]))

    entry = {"session": sid, "tool_use_id": tuid, "cwd": event.get("cwd"), "command": truncate(cmd, 4000),
             "pre": pre_tree, "post": post_tree, "changes": changes, "project_dir": project_dir}
    if cfg["undo"]:
        with store.lock(timeout=10.0):
            entry = store.add_entry(entry, int(cfg["keep_entries"]))

    def abs_of(rel):
        return os.path.join(store.worktree, rel)

    live = [(st, rel) for st, rel in changes if st != "D"]
    limit = int(cfg["max_bridged_files"])
    over = max(0, len(live) - limit)
    live = live[:limit]

    blocks, contexts, systems, stops = [], [], [], []
    denied, reverted = [], set()
    ran_any = False
    synth_cache = {}

    def synth(st, rel):
        if rel not in synth_cache:
            synth_cache[rel] = synthesize(store, pre_tree, st, rel, abs_of(rel))
        return synth_cache[rel]

    budget_end = time.time() + float(cfg["hook_budget_s"])
    n = 0
    # ---- PreToolUse Write|Edit guards, applied after the fact
    if cfg["guard"] != "off":
        pre_handlers = collect("PreToolUse", project_dir, cfg)
        if pre_handlers:
            for st, rel in live:
                tool, tinput, _tr, word = synth(st, rel)
                hs = [h for h in select_for_tool(pre_handlers, tool, cfg)
                      if if_matches(h.spec.get("if"), tool, abs_of(rel), event.get("cwd") or project_dir)]
                if not hs or time.time() > budget_end:
                    continue
                n += 1
                ran_any = True
                payload = payload_for(event, "PreToolUse", tool, tinput, None, n, word, cmd)
                cap = min(float(cfg["hook_timeout_s"]), max(1.0, budget_end - time.time()))
                for r in run_parallel(hs, payload, project_dir, event.get("cwd") or project_dir, cap):
                    ip = interpret_pre(r)
                    if ip["deny"]:
                        denied.append((st, rel, tool, r.handler.label(), ip["deny"]))
                    elif ip["ask"]:
                        contexts.append("PreToolUse hook %s would ask before this %s of %s: %s"
                                        % (r.handler.label(), tool, rel, ip["ask"]))
                    if ip["context"]:
                        contexts.append(ip["context"])
            if denied and cfg["guard"] == "revert":
                with store.lock(timeout=10.0):
                    for st, rel, _tool, _label, _reason in denied:
                        try:
                            if st == "A":
                                remove_path(store, rel)
                            else:
                                write_blob_to(store, pre_tree, rel)
                            reverted.add(rel)
                        except OSError as e:
                            debug("revert %s failed: %s" % (rel, e))
            for st, rel, tool, label, reason in denied:
                action = ("bash-bridge restored the file to its state before the Bash command"
                          if rel in reverted else "the change is still on disk")
                blocks.append("PreToolUse hook %s denies %s of %s: %s (%s)." % (label, tool, rel, reason, action))

    # ---- PostToolUse Write|Edit hooks
    if cfg["hooks"]:
        post_handlers = collect("PostToolUse", project_dir, cfg)
        if post_handlers:
            for st, rel in live:
                if rel in reverted:
                    continue
                tool, tinput, tresp, word = synth(st, rel)
                hs = [h for h in select_for_tool(post_handlers, tool, cfg)
                      if if_matches(h.spec.get("if"), tool, abs_of(rel), event.get("cwd") or project_dir)]
                if not hs:
                    continue
                if time.time() > budget_end:
                    systems.append("bash-bridge: hook budget used up; hooks skipped for %s" % rel)
                    continue
                n += 1
                ran_any = True
                payload = payload_for(event, "PostToolUse", tool, tinput, tresp, n, word, cmd)
                cap = min(float(cfg["hook_timeout_s"]), max(1.0, budget_end - time.time()))
                for r in run_parallel(hs, payload, project_dir, event.get("cwd") or project_dir, cap):
                    ip = interpret_post(r)
                    lab = r.handler.label()
                    if r.timed_out:
                        debug("hook timed out: %s" % lab)
                        systems.append("bash-bridge: hook %s timed out on %s" % (lab, rel))
                    if ip["block"]:
                        blocks.append("PostToolUse hook %s (run by bash-bridge for the %s of %s): %s"
                                      % (lab, "creation" if st == "A" else "edit", rel, ip["block"]))
                    if ip["context"]:
                        contexts.append(ip["context"])
                    if ip["system"]:
                        systems.append(ip["system"])
                    if ip["stop"]:
                        stops.append(ip["stop"])
                    debug("hook %s on %s -> code=%s" % (lab, rel, r.code))

    # ---- refresh the snapshot so formatter output / reverts belong to this entry
    if (ran_any or reverted) and cfg["undo"]:
        try:
            with store.lock(timeout=10.0):
                t3 = store.snapshot(cfg)
                entry["post"] = t3
                entry["changes"] = store.diff(pre_tree, t3) or changes
                store.update_entry(entry)
        except (Disabled, SnapshotError) as e:
            debug("re-snapshot failed: %s" % e)

    # ---- rules / nested CLAUDE.md
    rules_text = ""
    if cfg["rules"]:
        targets = [abs_of(rel) for st, rel in live if rel not in reverted]
        rel_names = [_rel(p, project_dir, store.worktree) for p in targets]
        rules_text = _rules_context(event, cfg, project_dir, sid, targets,
                                    ("changed", _list_names(rel_names)))

    parts = []
    if blocks:
        parts.append("bash-bridge ran your Write/Edit hooks for files this Bash command changed:\n- "
                     + "\n- ".join(blocks))
    if contexts:
        parts.append("\n".join(contexts))
    if rules_text:
        parts.append(rules_text)
    if over:
        parts.append("bash-bridge: %d more changed files were not bridged (max_bridged_files=%d)." % (over, limit))
    if not parts and not systems and not stops:
        debug("post %s done in %d ms, nothing to report" % (tuid, (time.time() - t0) * 1000))
        return None
    out = {}
    ctx = "\n\n".join(parts[1:] if (blocks and not failure) else parts)
    if blocks and not failure:
        out["decision"] = "block"
        out["reason"] = truncate(parts[0], 9000)
    if ctx:
        out["hookSpecificOutput"] = {"hookEventName": hook_event, "additionalContext": truncate(ctx, 9500)}
    if systems:
        out["systemMessage"] = truncate("\n".join(systems), 2000)
    if stops:
        out["continue"] = False
        out["stopReason"] = stops[0]
    debug("post %s done in %d ms" % (tuid, (time.time() - t0) * 1000))
    return out


def _list_names(names, k=5):
    if len(names) <= k:
        return ", ".join(names)
    return ", ".join(names[:k]) + " and %d more" % (len(names) - k)


def _rules_context(event, cfg, project_dir, sid, targets, header):
    if not targets:
        return ""
    excl = claude_md_excludes([p for _s, p in settings_files(project_dir, ["user", "project", "local", "managed"])])
    rs = RuleSet(project_dir, cfg, excl)
    matches = []
    for p in targets:
        rel = os.path.relpath(p, project_dir) if is_within(p, project_dir) else p
        for instr, reason, body in rs.matches_for(p):
            matches.append((instr, reason, body, rel))
    if not matches:
        return ""
    sess = load_session(sid)
    already = set(sess["injected"]) | loaded_in_transcript(event.get("transcript_path"))
    text, injected = build_context(matches, already, cfg, header)
    if injected:
        sess["injected"] = sorted(set(sess["injected"]) | set(injected))
        save_session(sid, sess)
    return text


def _read_rules(event, cfg, cmd, project_dir, sid, hook_event):
    mode = cfg.get("rules_on_read", "auto")
    if not cfg["rules"] or mode == "off":
        return None
    if mode == "auto" and is_plain_single_view(cmd):
        return None
    targets = [p for p in read_targets(cmd, event.get("cwd") or project_dir) if is_within(p, project_dir)]
    if not targets:
        return None
    names = [os.path.relpath(p, project_dir) for p in targets]
    text = _rules_context(event, cfg, project_dir, sid, targets, ("read", _list_names(names)))
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": hook_event, "additionalContext": text}}


def handle_session_start(event, cfg):
    sid = event.get("session_id")
    src = event.get("source")
    if sid and src in ("compact", "clear"):
        sess = load_session(sid)
        sess["injected"] = []
        save_session(sid, sess)
    if src == "startup":
        _maintenance()
    return None


def _maintenance():
    root = os.path.join(state_dir(), "sessions")
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if time.time() - os.path.getmtime(p) > 14 * 86400:
                os.remove(p)
    except OSError:
        pass
    proj = os.path.join(state_dir(), "projects")
    try:
        for name in os.listdir(proj):
            pend = os.path.join(proj, name, "pending")
            if os.path.isdir(pend):
                for f in os.listdir(pend):
                    fp = os.path.join(pend, f)
                    if time.time() - os.path.getmtime(fp) > 86400:
                        os.remove(fp)
    except OSError:
        pass


def dispatch(mode, event):
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or None
    cfg = config_mod.load(project_dir)
    if not cfg["enabled"]:
        return None
    if mode == "pre":
        return handle_pre(event, cfg)
    if mode == "post":
        return handle_post(event, cfg, failure=False)
    if mode == "post-failure":
        return handle_post(event, cfg, failure=True)
    if mode == "session-start":
        return handle_session_start(event, cfg)
    return None
