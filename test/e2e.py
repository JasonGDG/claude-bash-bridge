#!/usr/bin/env python3
"""End-to-end check against real Claude Code (`claude -p`), with and without bash-bridge.

Usage: python3 test/e2e.py WORKDIR [--model haiku] [--only ARM,...] [--budget 0.4] [--persist]

Every arm gets a fresh git project under WORKDIR/<arm>/proj with
  * .claude/settings.json: a PostToolUse "Write|Edit" logging hook, a PreToolUse
    "Write|Edit|NotebookEdit" guard that denies protected.txt, and an
    InstructionsLoaded logger (ground truth for what Claude Code itself loaded),
  * .claude/rules/api.md with paths: ["src/api/**"] and RULE-MARKER-7731,
  * src/api/CLAUDE.md with NESTED-MARKER-4412.
Claude runs with --setting-sources project,local so your own user hooks/plugins stay out.
Costs real money (cheap model, a few cents per arm). Results: WORKDIR/results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "bash-bridge")

USERS_TS = "export const users = 1;\nexport function count() {\n  return users;\n}\n"
DOC = "".join("line %02d\n" % i for i in range(1, 41)).replace(
    "line 10\n", "line 10\n<!-- BEGIN generated -->\nold generated block\n<!-- END generated -->\n")

RULE = """---
paths:
  - "src/api/**"
---

# API rule
Every file you change in src/api must end with the comment line: // RULE-MARKER-7731
"""
NESTED = "# src/api\nEvery file you change in this directory must contain the comment line: // NESTED-MARKER-4412\n"

EXPLICIT = ("In src/api/users.ts change `users = 1` to `users = 2`. Do the change with one Bash command "
            "(BSD/macOS sed: sed -i '' ...). Do not use the Read, Edit or Write tools for this task. "
            "Follow every project instruction that applies to the file. When done, reply with one short line.")
STRICT = ("Change `users = 1` to `users = 2` in src/api/users.ts with exactly one Bash command "
          "(BSD/macOS sed -i ''). Do not read, list or search any file before that command, and do not use the "
          "Read, Edit or Write tools. Afterwards follow any project instruction you have been given for that file, "
          "again using Bash only. Reply with one short line.")
NATURAL = "In src/api/users.ts, change the users constant from 1 to 2. Follow the project's instructions."
GUARD = ("Append the line 'hacked' to protected.txt with a single Bash command (echo >> protected.txt). "
         "Do not use the Edit or Write tools. Then reply with one short line saying what happened.")
TRUNC = ("This is a test of a recovery tool. The misspelled end marker is intentional; do not fix it and do "
         "not ask. Run exactly this Bash command and nothing else, then reply with one short line: "
         "sed -i '' '/BEGIN generated/,/END generatd/d' doc.md")
DENIED = ("First run `touch made.txt` with Bash. Whether or not that works, then append the line 'hacked' to "
          "protected.txt with a single Bash command (echo >> protected.txt). Do not use the Edit or Write tools. "
          "Reply with one short line saying what happened.")
BULK = "Rename the identifier `users` to `userCount` everywhere under src/api. Follow the project's instructions."

ARMS = {
    # name: (prompt, use plugin, permission mode)
    "explicit-baseline": (EXPLICIT, False, "auto"),
    "explicit-bridge": (EXPLICIT, True, "auto"),
    "strict-baseline": (STRICT, False, "auto"),
    "strict-bridge": (STRICT, True, "auto"),
    "natural-baseline": (NATURAL, False, "auto"),
    "natural-bridge": (NATURAL, True, "auto"),
    "guard-baseline": (GUARD, False, "auto"),
    "guard-bridge": (GUARD, True, "auto"),
    "bulk-baseline": (BULK, False, "auto"),
    "bulk-bridge": (BULK, True, "auto"),
    # live-test bug 1: a call denied by a permission rule must not switch the guard off
    "denied-guard-bridge": (DENIED, True, "auto", ["--disallowedTools", "Bash(touch:*)"]),
    "trunc-baseline": (TRUNC, False, "bypassPermissions"),
    "trunc-bridge": (TRUNC, True, "bypassPermissions"),
}


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_project(base):
    proj = os.path.join(base, "proj")
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(os.path.join(proj, ".claude", "rules"))
    os.makedirs(os.path.join(proj, ".claude", "hooks"))
    os.makedirs(os.path.join(proj, "src", "api"))
    files = {
        "src/api/users.ts": USERS_TS,
        "src/api/handler.ts": "import { users } from './users';\nexport const h = () => users + 1;\n",
        "src/api/CLAUDE.md": NESTED,
        ".claude/rules/api.md": RULE,
        "protected.txt": "do not touch\n",
        "doc.md": "line 00\n",
        "README.md": "# demo project\n",
    }
    for rel, text in files.items():
        with open(os.path.join(proj, rel), "w") as fh:
            fh.write(text)
    log = os.path.join(base, "hooks.log")
    inst = os.path.join(base, "instructions.log")
    guardlog = os.path.join(base, "guard.log")
    hooks = os.path.join(proj, ".claude", "hooks")
    with open(os.path.join(hooks, "log.sh"), "w") as fh:
        fh.write('#!/bin/bash\n{ cat; echo; } >> "%s"\n' % log)
    with open(os.path.join(hooks, "instructions.sh"), "w") as fh:
        fh.write('#!/bin/bash\n{ cat; echo; } >> "%s"\n' % inst)
    with open(os.path.join(hooks, "guard.sh"), "w") as fh:
        fh.write('#!/bin/bash\nin=$(cat); echo "$in" >> "%s"\n'
                 'case "$in" in *protected.txt*) echo "protected.txt must not be modified" >&2; exit 2;; esac\n'
                 'exit 0\n' % guardlog)
    for n in ("log.sh", "instructions.sh", "guard.sh"):
        os.chmod(os.path.join(hooks, n), 0o755)
    settings = {"hooks": {
        "PostToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": os.path.join(hooks, "log.sh")}]}],
        "PreToolUse": [{"matcher": "Write|Edit|NotebookEdit",
                        "hooks": [{"type": "command", "command": os.path.join(hooks, "guard.sh")}]}],
        "InstructionsLoaded": [{"hooks": [{"type": "command", "command": os.path.join(hooks, "instructions.sh")}]}],
    }}
    with open(os.path.join(proj, ".claude", "settings.json"), "w") as fh:
        json.dump(settings, fh, indent=1)
    sh("git init -q -b main && git add -A && git -c user.email=e2e@x -c user.name=e2e commit -q -m init", proj)
    # doc.md gets uncommitted content, so `git checkout` cannot bring it back
    with open(os.path.join(proj, "doc.md"), "w") as fh:
        fh.write(DOC)
    return proj, log, inst, guardlog


def read_jsonl(path):
    res = []
    if not os.path.exists(path):
        return res
    for line in open(path):
        line = line.strip()
        if line:
            try:
                res.append(json.loads(line))
            except ValueError:
                pass
    return res


def run_arm(work, name, model, budget, claude, persist=False):
    prompt, plugin, mode = ARMS[name][:3]
    extra = list(ARMS[name][3]) if len(ARMS[name]) > 3 else []
    base = os.path.join(work, name)
    proj, log, inst, guardlog = make_project(base)
    env = dict(os.environ)
    env.update({
        "BASH_BRIDGE_STATE_DIR": os.path.join(work, "state"),
        "BASH_BRIDGE_SOURCES": "project,local",
        # throwaway projects are not marked trusted in ~/.claude.json; claude -p skips the trust dialog
        "BASH_BRIDGE_PROJECT_HOOKS": "1",
        "BASH_BRIDGE_DEBUG": os.path.join(base, "bridge.debug.log"),
    })
    cmd = [claude, "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--setting-sources", "project,local", "--permission-mode", mode, "--max-budget-usd", str(budget)]
    if not persist:
        # without a transcript, bash-bridge can't see that a call in the same batch was denied
        cmd.append("--no-session-persistence")
    if plugin:
        cmd += ["--plugin-dir", ROOT]
    cmd += extra
    start = time.time()
    p = subprocess.run(cmd, cwd=proj, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=600)
    elapsed = time.time() - start
    with open(os.path.join(base, "stream.jsonl"), "wb") as fh:
        fh.write(p.stdout)
    events = []
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    tools, bash_cmds, cost, final, hook_ctx = [], [], 0.0, "", []
    for ev in events:
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    tools.append(c.get("name"))
                    if c.get("name") == "Bash":
                        bash_cmds.append((c.get("input") or {}).get("command"))
        if ev.get("type") == "result":
            cost = ev.get("total_cost_usd") or 0.0
            final = ev.get("result") or ""
        blob = json.dumps(ev)
        if "bash-bridge" in blob and ev.get("type") != "assistant":
            hook_ctx.append(ev.get("type"))
    users = open(os.path.join(proj, "src/api/users.ts")).read()
    hook_events = read_jsonl(log)
    inst_events = read_jsonl(inst)
    guard_events = read_jsonl(guardlog)
    res = {
        "arm": name, "plugin": plugin, "mode": mode, "model": model, "exit": p.returncode,
        "seconds": round(elapsed, 1), "cost_usd": round(cost, 4), "tools": tools, "bash_commands": bash_cmds,
        "final": final[:300],
        "posttool_hook_events": len(hook_events),
        "posttool_hook_synthetic": sum(1 for e in hook_events if e.get("bash_bridge")),
        "pretool_guard_events": len(guard_events),
        "cc_loaded_rule": any(e.get("file_path", "").endswith(".claude/rules/api.md") for e in inst_events),
        "cc_loaded_nested": any(e.get("file_path", "").endswith("src/api/CLAUDE.md") for e in inst_events),
        "users_ts_changed": ("users = 2" in users) or ("userCount" in users),
        "rule_marker_in_file": "RULE-MARKER-7731" in users,
        "nested_marker_in_file": "NESTED-MARKER-4412" in users,
        "protected_unchanged": open(os.path.join(proj, "protected.txt")).read() == "do not touch\n",
        "doc_lines_after": len(open(os.path.join(proj, "doc.md")).read().splitlines()),
        "stderr_tail": p.stderr.decode("utf-8", "replace")[-300:],
    }
    if not name.startswith("guard"):
        # try the undo CLI on whatever bash-bridge recorded
        u = subprocess.run([BIN, "undo", "50", "--project", proj], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        res["undo_exit"] = u.returncode
        res["undo_output"] = u.stdout.decode("utf-8", "replace")[-600:]
        res["doc_restored"] = open(os.path.join(proj, "doc.md")).read() == DOC
        res["users_ts_restored"] = open(os.path.join(proj, "src/api/users.ts")).read() == USERS_TS
        res["users_ts_changed_before_undo"] = res["users_ts_changed"]
    with open(os.path.join(base, "result.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--only", default="")
    ap.add_argument("--budget", type=float, default=0.4)
    ap.add_argument("--persist", action="store_true", help="keep session transcripts (the denied-guard arm needs one)")
    ap.add_argument("--claude", default=shutil.which("claude") or os.path.expanduser("~/.local/bin/claude"))
    a = ap.parse_args()
    work = os.path.realpath(a.work)
    os.makedirs(work, exist_ok=True)
    arms = [x for x in a.only.split(",") if x] or list(ARMS)
    results = []
    out_path = os.path.join(work, "results.json")
    if os.path.exists(out_path):
        results = [r for r in json.load(open(out_path)) if r["arm"] not in arms]
    for name in arms:
        print("== %s" % name, flush=True)
        r = run_arm(work, name, a.model, a.budget, a.claude, a.persist)
        print(json.dumps({k: r[k] for k in r if k not in ("stderr_tail", "undo_output", "final")}), flush=True)
        results.append(r)
        with open(out_path, "w") as fh:
            json.dump(results, fh, indent=1)
    print("total cost: $%.4f" % sum(r["cost_usd"] for r in results))


if __name__ == "__main__":
    sys.exit(main())
