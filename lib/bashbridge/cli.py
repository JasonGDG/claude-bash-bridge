"""Command line: `bash-bridge hook <mode>` (used by hooks.json), `list`, `undo`, `status`."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
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


WORD = {"A": "created", "M": "modified", "T": "type changed", "D": "deleted",
        "B": "changed (too large to snapshot)", "U": "created (unreadable, not snapshotted)"}


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
        if e.get("overlap"):
            flag += " [overlapped another Bash call: changes may belong to it]"
        if entry_expired(store, e):
            flag += " [expired: cannot be undone fully]"
        print("#%-4s %s  %s%s" % (e.get("id"), _fmt_time(e.get("ts")), show(_one_line(e.get("command"))), flag))
        for st, rel in e.get("changes", [])[:20]:
            print("        %-9s %s" % (WORD.get(st, st), show(rel)))
        if len(e.get("changes", [])) > 20:
            print("        ... %d more" % (len(e["changes"]) - 20))
    return 0


def show(rel) -> str:
    """Path for terminal output: control characters (newline, tab, ESC ...) escaped."""
    return "".join(c if (ord(c) >= 32 and ord(c) != 127) else ("\\n" if c == "\n" else "\\x%02x" % ord(c))
                   for c in str(rel))


def entry_expired(store, e) -> bool:
    files = e.get("files")
    if files is None:
        return True
    for f in files:
        if f.get("st") in ("B", "U"):
            continue
        if not f.get("kept", True):
            return True
        if f.get("pre") and not os.path.isfile(store.blob_path(f["pre"][1])):
            return True
    return False


def _files_of(e):
    """Per-path pre/post states of an entry. Entries written before pre-images were kept have none."""
    return e.get("files")


def plan_undo(store, entries, force=False):
    """Compute what undoing `entries` (newest first) does. Returns list of step dicts.
    Must be called with the store lock held."""
    from .snapshot import disk_state
    virtual = {}
    steps = []
    for e in entries:
        files = _files_of(e)
        if files is None:
            steps.append({"entry": e["id"], "rel": "(all files)", "action": "expired", "conflict": False,
                          "expected": None, "pre": None})
            continue
        for f in files:
            rel = f["rel"]
            pre = tuple(f["pre"]) if f.get("pre") else None
            post = tuple(f["post"]) if f.get("post") else None
            full = os.path.join(store.worktree, rel)
            current = virtual[rel] if rel in virtual else disk_state(full)
            conflict = current != post
            if f.get("st") in ("B", "U"):
                steps.append({"entry": e["id"], "rel": rel, "action": "toolarge" if f["st"] == "B" else "unreadable",
                              "conflict": False, "expected": None, "pre": None})
                continue
            if pre and post:
                action = "restore"
            elif pre:
                action = "recreate"
            else:
                action = "remove"
            if (pre and current == pre) or (pre is None and current is None):
                action, conflict = "unchanged", False
            elif pre and (not f.get("kept", True) or store.load_blob(pre[1]) is None):
                action = "expired"
            steps.append({"entry": e["id"], "rel": rel, "action": action, "conflict": conflict,
                          "expected": current if rel in virtual else post, "pre": pre})
            if action != "expired" and (not conflict or force):
                virtual[rel] = pre
    return steps


def cmd_undo(args) -> int:
    from .snapshot import disk_state, remove_path, write_file
    store = _store_for(args.project)
    past = {"restore": "restored", "recreate": "recreated", "remove": "removed", "unchanged": "kept"}
    future = {"restore": "would restore", "recreate": "would recreate", "remove": "would remove",
              "unchanged": "would keep"}
    note = {"restore": "(the Bash call modified it)", "recreate": "(the Bash call deleted it)",
            "remove": "(the Bash call created it)", "unchanged": "(already back at the old content)"}
    failed = done = 0
    incomplete = set()
    with store.lock(timeout=10.0):
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
        by_entry = {}
        for st in steps:
            by_entry.setdefault(st["entry"], []).append(st)
        # remove what the call created first (deepest first), then restore: a file that was
        # replaced by a directory can only come back once the directory is empty again
        for k, lst in by_entry.items():
            lst.sort(key=lambda x: (0 if x["action"] == "remove" else 1,
                                    -x["rel"].count("/") if x["action"] == "remove" else 0))
        for e in entries:
            print("Bash call id %s at %s: %s" % (e["id"], _fmt_time(e.get("ts")), _one_line(e.get("command"))))
            if e.get("overlap") and not args.force:
                print("    SKIPPED         (this call overlapped another Bash call, so its entry may include that "
                      "call's changes; --force undoes it anyway)")
                failed += 1
                incomplete.add(e["id"])
                continue
            for st in by_entry.get(e["id"], []):
                rel = show(st["rel"])
                if st["action"] == "toolarge":
                    print("    TOO LARGE       %s (over max_file_bytes when it changed; not snapshotted, cannot undo)"
                          % rel)
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                if st["action"] == "unreadable":
                    print("    UNREADABLE      %s (created without read permission; not snapshotted, left alone)" % rel)
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                if st["action"] == "expired":
                    print("    EXPIRED         %s (its previous content is no longer stored; cannot undo)" % rel)
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                if st["conflict"] and not args.force:
                    print("    SKIPPED         %s (changed again after that Bash call; --force overwrites it)" % rel)
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                label = future[st["action"]] if args.dry_run else past[st["action"]]
                if args.dry_run or st["action"] == "unchanged":
                    print("    %-15s %s %s" % (label, rel, note[st["action"]]))
                    continue
                full = os.path.join(store.worktree, st["rel"])
                # re-check right before touching the file: something may have written it since planning
                if not args.force and disk_state(full) != st["expected"]:
                    print("    SKIPPED         %s (changed while undoing; --force overwrites it)" % rel)
                    failed += 1
                    incomplete.add(e["id"])
                    continue
                try:
                    if st["action"] == "remove":
                        remove_path(store, st["rel"])
                    else:
                        data = store.load_blob(st["pre"][1])
                        if data is None:
                            raise OSError("previous content is no longer stored")
                        write_file(store, st["rel"], st["pre"][0], data)
                    done += 1
                    print("    %-15s %s %s" % (label, rel, note[st["action"]]))
                except OSError as ex:
                    print("    FAILED          %s: %s" % (rel, ex))
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
        if done and any(re.search(r"\bgit\s+(-C\s+\S+\s+)?(rm|mv|add|reset|restore\s+--staged)\b", e.get("command") or "")
                        for e in entries):
            print("Note: only the files were restored. Changes to the git index (staged adds/removals) are not "
                  "undone; check `git status`.")
    if failed:
        print("%d file(s) skipped, expired or failed." % failed)
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
    from .util import UnsafeState, secure_state
    try:
        secure_state()
    except (UnsafeState, OSError) as e:
        print("bash-bridge: refusing to use the state directory: %s" % e)
        return 1
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
    from .util import UnsafeState as _Unsafe
    try:
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "undo":
            return cmd_undo(args)
        if args.cmd == "status":
            return cmd_status(args)
    except _Unsafe as e:
        print("bash-bridge: refusing to use the state directory: %s" % e)
        return 1
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
