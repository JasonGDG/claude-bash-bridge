"""Command line: `bash-bridge hook <mode>` (used by hooks.json), `list`, `undo`, `status`."""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

from . import __version__


def _hook(mode: str) -> int:
    """Run a hook. Whatever happens: exit 0, and stdout holds either nothing or one JSON object."""
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()  # anything printed by accident must not reach Claude Code
    out = None
    try:
        if os.environ.get("BASH_BRIDGE_ACTIVE"):
            return 0  # we are running inside a hook bash-bridge started: no recursion
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return 0
        from .bridge import dispatch
        out = dispatch(mode, event)
    except BaseException:  # noqa: BLE001 - a hook must never break the session
        from .util import debug_exc
        debug_exc("hook " + mode)
        out = None
    finally:
        sys.stdout = real_stdout
    if out:
        try:
            real_stdout.write(json.dumps(out, ensure_ascii=False))
            real_stdout.flush()
        except Exception:
            pass
    return 0


def _store_for(project):
    from .snapshot import Store, find_git_toplevel, store_dir_for
    start = os.path.realpath(project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    top = find_git_toplevel(start)
    cands = []
    if top:
        cands.append(os.path.realpath(top))
    d = start
    while True:
        cands.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for c in cands:
        if os.path.isfile(os.path.join(store_dir_for(c), "entries.jsonl")):
            return Store(c)
    return Store(cands[0])


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def _one_line(cmd, n=90):
    c = " ".join(str(cmd or "").split())
    return c if len(c) <= n else c[: n - 3] + "..."


WORD = {"A": "created", "M": "modified", "T": "type changed", "D": "deleted"}


def cmd_list(args) -> int:
    store = _store_for(args.project)
    entries = store.load_entries()
    if args.session:
        entries = [e for e in entries if e.get("session") == args.session]
    entries = entries[-args.n:] if args.n else entries
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=1))
        return 0
    if not entries:
        print("bash-bridge: no recorded Bash changes for %s" % store.worktree)
        return 0
    print("bash-bridge: Bash calls that changed files in %s (newest last)" % store.worktree)
    for e in entries:
        flag = " [undone]" if e.get("undone") else ""
        print("#%-4s %s  %s%s" % (e.get("id"), _fmt_time(e.get("ts")), _one_line(e.get("command")), flag))
        for st, rel in e.get("changes", [])[:20]:
            print("        %-9s %s" % (WORD.get(st, st), rel))
        if len(e.get("changes", [])) > 20:
            print("        ... %d more" % (len(e["changes"]) - 20))
    return 0


def plan_undo(store, entries, force=False):
    """Compute what undoing `entries` (newest first) does. Returns list of step dicts."""
    from .snapshot import disk_state
    virtual = {}
    steps = []
    for e in entries:
        for st, rel in e.get("changes", []):
            pre = store.entry_info(e["pre"], rel)
            post = store.entry_info(e["post"], rel)
            full = os.path.join(store.worktree, rel)
            current = virtual[rel] if rel in virtual else disk_state(full)
            expected = tuple(post) if post else None
            conflict = current != expected
            if pre and post:
                action = "restore"
            elif pre:
                action = "recreate"
            else:
                action = "remove"
            if pre and current == tuple(pre):
                action = "unchanged"
                conflict = False
            steps.append({"entry": e["id"], "rel": rel, "action": action, "conflict": conflict,
                          "pre_tree": e["pre"], "status": st})
            if not conflict or force:
                virtual[rel] = tuple(pre) if pre else None
    return steps


def cmd_undo(args) -> int:
    from .snapshot import remove_path, write_blob_to
    store = _store_for(args.project)
    entries = [e for e in store.load_entries() if not e.get("undone")]
    if args.session:
        entries = [e for e in entries if e.get("session") == args.session]
    if args.id is not None:
        entries = [e for e in entries if e.get("id") == args.id]
    else:
        entries = entries[-max(1, args.n):]
    entries = list(reversed(entries))
    if not entries:
        print("bash-bridge: nothing to undo in %s" % store.worktree)
        return 1
    steps = plan_undo(store, entries, args.force)
    if args.dry_run:
        print("bash-bridge DRY RUN (nothing changed yet): undo the last %d Bash call(s) in %s"
              % (len(entries), store.worktree))
    else:
        print("bash-bridge UNDO: undoing the last %d Bash call(s) in %s" % (len(entries), store.worktree))
    past = {"restore": "restored", "recreate": "recreated", "remove": "removed", "unchanged": "kept"}
    future = {"restore": "would restore", "recreate": "would recreate", "remove": "would remove",
              "unchanged": "would keep"}
    note = {"restore": "(the Bash call modified it)", "recreate": "(the Bash call deleted it)",
            "remove": "(the Bash call created it)", "unchanged": "(already back at the old content)"}
    by_entry = {}
    for s in steps:
        by_entry.setdefault(s["entry"], []).append(s)
    failed = 0
    done = 0
    incomplete = set()
    with store.lock(timeout=10.0):
        for e in entries:
            print("Bash call id %s at %s: %s" % (e["id"], _fmt_time(e.get("ts")), _one_line(e.get("command"))))
            for s in by_entry.get(e["id"], []):
                if s["conflict"] and not args.force:
                    print("    SKIPPED         %s (changed again after that Bash call; --force overwrites it)"
                          % s["rel"])
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                label = future[s["action"]] if args.dry_run else past[s["action"]]
                if args.dry_run or s["action"] == "unchanged":
                    print("    %-15s %s %s" % (label, s["rel"], note[s["action"]]))
                    continue
                try:
                    if s["action"] == "remove":
                        remove_path(store, s["rel"])
                    else:
                        write_blob_to(store, s["pre_tree"], s["rel"])
                    done += 1
                    print("    %-15s %s %s" % (label, s["rel"], note[s["action"]]))
                except OSError as ex:
                    print("    FAILED          %s: %s" % (s["rel"], ex))
                    failed += 1
                    incomplete.add(e["id"])
        if not args.dry_run:
            all_entries = store.load_entries()
            ids = {e["id"] for e in entries} - incomplete
            for e in all_entries:
                if e.get("id") in ids:
                    e["undone"] = True
                    e["undone_ts"] = time.time()
            store.save_entries(all_entries)
    if args.dry_run:
        print("Nothing was changed. To apply: /bash-undo %d   (or: bash-bridge undo %d)" % (len(entries), len(entries)))
    else:
        print("Done: %d file(s) changed back." % done)
    if failed:
        print("%d file(s) skipped or failed." % failed)
    return 0 if not failed else 2


def cmd_status(args) -> int:
    from .config import load
    store = _store_for(args.project)
    meta = store.meta()
    cfg = load(os.environ.get("CLAUDE_PROJECT_DIR") or store.worktree)
    print("bash-bridge %s" % __version__)
    print("work tree:   %s" % store.worktree)
    print("state dir:   %s" % store.dir)
    print("enabled:     %s (hooks=%s guard=%s rules=%s undo=%s)" % (cfg["enabled"], cfg["hooks"], cfg["guard"],
                                                                    cfg["rules"], cfg["undo"]))
    if meta.get("disabled_until", 0) > time.time():
        print("inactive:    %s (until %s)" % (meta.get("disabled_reason"), _fmt_time(meta["disabled_until"])))
    print("entries:     %d" % len(store.load_entries()))
    return 0


def cmd_slash(argv) -> int:
    """Entry for the /bash-undo skill: `[list] [N] [--dry-run] [--force]`."""
    toks = [t for t in " ".join(argv).split() if t]
    if toks and toks[0] in ("list", "ls"):
        return cmd_list(argparse.Namespace(project=None, n=20, json=False, session=None))
    ns = argparse.Namespace(project=None, n=1, dry_run=False, force=False, id=None, session=None)
    for t in toks:
        if t in ("--dry-run", "-n", "dry-run", "dry"):
            ns.dry_run = True
        elif t == "--force":
            ns.force = True
        elif t.startswith("#") and t[1:].isdigit():
            ns.id = int(t[1:])
        elif t.isdigit():
            ns.n = int(t)
    return cmd_undo(ns)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        return _hook(argv[1] if len(argv) > 1 else "")
    if argv and argv[0] == "slash":
        try:
            return cmd_slash(argv[1:])
        except Exception as e:  # noqa: BLE001
            print("bash-bridge: %s" % e)
            return 1
    p = argparse.ArgumentParser(prog="bash-bridge", description="Undo and inspect file changes made by Bash calls.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    pl = sub.add_parser("list", help="show recorded Bash calls that changed files")
    pl.add_argument("-n", type=int, default=20, help="show the last N (default 20, 0 = all)")
    pl.add_argument("--project", help="project directory (default: current directory)")
    pl.add_argument("--session", help="only this Claude Code session id")
    pl.add_argument("--json", action="store_true")
    pu = sub.add_parser("undo", help="restore files changed by the last N Bash calls")
    pu.add_argument("n", nargs="?", type=int, default=1)
    pu.add_argument("--dry-run", action="store_true", help="only show what would change")
    pu.add_argument("--force", action="store_true", help="overwrite files that changed again since")
    pu.add_argument("--id", type=int, help="undo exactly this entry (see list)")
    pu.add_argument("--project")
    pu.add_argument("--session")
    ps = sub.add_parser("status", help="show state and configuration for this project")
    ps.add_argument("--project")
    args = p.parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "undo":
        return cmd_undo(args)
    if args.cmd == "status":
        return cmd_status(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
