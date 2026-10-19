"""Shadow-git snapshots of a project's working tree.

Each project gets a private git directory under the state dir
(``<state>/projects/<key>/shadow.git``) whose work tree is the project. The
project's own ``.git`` is never written to. A snapshot is a git tree object:

* ``git ls-files -m -d -o --exclude-standard`` lists what changed since the
  last snapshot (honours .gitignore, uses the stat cache, so it costs about as
  much as ``git status``),
* ``git update-index`` hashes only those paths,
* ``git write-tree`` returns the tree id.

For git projects the shadow repo borrows the project's object store through
``objects/info/alternates`` and, when that is safe, starts from a copy of the
project's index, so the first snapshot does not have to hash every file.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from .util import debug, now_ms, read_json, sha_key, state_dir, write_json_atomic

ZERO_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SHADOW_ATTRIBUTES = "* -text -eol -crlf -filter -ident -working-tree-encoding\n"
_CONTENT_ATTR = re.compile(rb"\b(filter|text|eol|crlf|ident|working-tree-encoding)\b")


class SnapshotError(Exception):
    pass


class Disabled(Exception):
    """The project is too big / too slow; bash-bridge stays out of the way."""


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(args, cwd=None, env=None, input_bytes=None, timeout=10.0, check=True) -> bytes:
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=cwd, env=env or _clean_env(), input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except FileNotFoundError:
        raise SnapshotError("git not found")
    except subprocess.TimeoutExpired:
        raise SnapshotError("git %s timed out" % args[0])
    if check and proc.returncode != 0:
        raise SnapshotError("git %s failed: %s" % (" ".join(args[:3]), proc.stderr.decode("utf-8", "replace")[:300]))
    return proc.stdout


def find_git_toplevel(path: str):
    try:
        out = run_git(["-C", path, "rev-parse", "--show-toplevel"], timeout=3.0)
        top = out.decode("utf-8", "surrogateescape").strip()
        return top or None
    except SnapshotError:
        return None


def git_blob_sha(path: str):
    """Git blob id of a file on disk (raw bytes, like --no-filters); None if missing."""
    try:
        if os.path.islink(path):
            data = os.fsencode(os.readlink(path))
        elif os.path.isfile(path):
            with open(path, "rb") as fh:
                data = fh.read()
        else:
            return None
    except OSError:
        return None
    h = hashlib.sha1(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def disk_state(path: str):
    """(git mode, blob sha) of a file on disk, or None if it does not exist."""
    sha = git_blob_sha(path)
    if sha is None:
        return None
    if os.path.islink(path):
        return ("120000", sha)
    return ("100755" if os.access(path, os.X_OK) else "100644", sha)


def project_key(worktree: str) -> str:
    return sha_key(os.path.realpath(worktree))


def store_dir_for(worktree: str) -> str:
    return os.path.join(state_dir(), "projects", project_key(worktree))


class Store:
    def __init__(self, worktree: str):
        self.worktree = os.path.realpath(worktree)
        self.dir = store_dir_for(self.worktree)
        self.gitdir = os.path.join(self.dir, "shadow.git")
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.entries_path = os.path.join(self.dir, "entries.jsonl")
        self.pending_dir = os.path.join(self.dir, "pending")

    # ------------------------------------------------------------------ basics
    def env(self) -> dict:
        env = _clean_env()
        env["GIT_DIR"] = self.gitdir
        env["GIT_WORK_TREE"] = self.worktree
        return env

    def git(self, args, input_bytes=None, timeout=10.0, check=True) -> bytes:
        return run_git(args, cwd=self.worktree, env=self.env(), input_bytes=input_bytes, timeout=timeout, check=check)

    def meta(self) -> dict:
        return read_json(self.meta_path, {}) or {}

    def save_meta(self, meta: dict) -> None:
        write_json_atomic(self.meta_path, meta)

    @contextlib.contextmanager
    def lock(self, timeout: float = 10.0):
        os.makedirs(self.dir, exist_ok=True)
        fh = open(os.path.join(self.dir, "lock"), "a+")
        deadline = time.time() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise SnapshotError("lock timeout")
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()

    # ------------------------------------------------------------------ setup
    def disabled_reason(self):
        meta = self.meta()
        until = meta.get("disabled_until", 0)
        if until and until > time.time():
            return meta.get("disabled_reason") or "disabled"
        return None

    def disable(self, reason: str, hours: float = 24.0) -> None:
        meta = self.meta()
        meta["disabled_until"] = time.time() + hours * 3600
        meta["disabled_reason"] = reason
        meta["worktree"] = self.worktree
        self.save_meta(meta)
        debug("disabled %s: %s" % (self.worktree, reason))

    def _real_gitdir(self):
        try:
            out = run_git(["-C", self.worktree, "rev-parse", "--absolute-git-dir", "--git-common-dir"], timeout=3.0)
        except SnapshotError:
            return None, None
        lines = out.decode("utf-8", "surrogateescape").splitlines()
        if len(lines) < 2:
            return None, None
        gitdir = lines[0]
        common = lines[1]
        if not os.path.isabs(common):
            common = os.path.normpath(os.path.join(self.worktree, common))
        return gitdir, common

    def _seed_is_safe(self, real_gitdir: str) -> bool:
        if not os.path.isfile(os.path.join(real_gitdir, "index")):
            return False
        try:
            if any(n.startswith("sharedindex.") for n in os.listdir(real_gitdir)):
                return False
        except OSError:
            return False
        try:
            cfg = run_git(["-C", self.worktree, "config", "--get-regexp", r"^core\.(autocrlf|splitindex)$"],
                          timeout=3.0, check=False)
            for line in cfg.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].lower() not in ("false", "0", "no", "off"):
                    return False
        except SnapshotError:
            return False
        attr_files = [os.path.join(real_gitdir, "info", "attributes")]
        try:
            out = run_git(["-C", self.worktree, "ls-files", "-z", "--", ".gitattributes", "*/.gitattributes"],
                          timeout=5.0)
            for rel in out.split(b"\0")[:200]:
                if rel:
                    attr_files.append(os.path.join(self.worktree, os.fsdecode(rel)))
        except SnapshotError:
            return False
        for path in attr_files:
            try:
                with open(path, "rb") as fh:
                    for line in fh.read(200000).splitlines():
                        line = line.strip()
                        if line and not line.startswith(b"#") and _CONTENT_ATTR.search(line):
                            return False
            except OSError:
                continue
        return True

    def _index_entry_count(self, index_path: str) -> int:
        try:
            with open(index_path, "rb") as fh:
                head = fh.read(12)
            if head[:4] == b"DIRC":
                return int.from_bytes(head[8:12], "big")
        except OSError:
            pass
        return 0

    def _count_files(self, cfg: dict, budget_ms: int):
        """Bounded walk for non-git trees. Returns count, or None if over the cap/budget."""
        excl_dirs = {e.rstrip("/") for e in cfg.get("excludes", []) if e.endswith("/")}
        excl_dirs.add(".git")
        limit = int(cfg.get("max_files", 100000))
        deadline = time.time() + budget_ms / 1000.0
        count = 0
        stack = [self.worktree]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for ent in it:
                        if ent.is_dir(follow_symlinks=False):
                            if ent.name not in excl_dirs:
                                stack.append(ent.path)
                        else:
                            count += 1
                            if count > limit:
                                return None
            except OSError:
                continue
            if time.time() > deadline:
                return None
        return count

    def write_excludes(self, cfg: dict, extra=()) -> None:
        info = os.path.join(self.gitdir, "info")
        os.makedirs(info, exist_ok=True)
        lines = ["# written by bash-bridge"] + list(cfg.get("excludes", []))
        meta = self.meta()
        real_info = meta.get("real_info_exclude")
        if real_info:
            try:
                with open(real_info, "r", encoding="utf-8", errors="replace") as fh:
                    lines += [l.rstrip("\n") for l in fh]
            except OSError:
                pass
        for rel in list(meta.get("big_files", [])) + list(extra):
            lines.append("/" + re.sub(r"([\\*?\[\]!#])", r"\\\1", rel))
        with open(os.path.join(info, "exclude"), "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(info, "attributes"), "w") as fh:
            fh.write(SHADOW_ATTRIBUTES)

    def ensure(self, cfg: dict) -> None:
        """Create the shadow repo if needed. Raises Disabled for projects over the caps."""
        reason = self.disabled_reason()
        if reason:
            raise Disabled(reason)
        if os.path.isfile(os.path.join(self.gitdir, "HEAD")) and self.meta().get("ready"):
            return
        os.makedirs(self.dir, exist_ok=True)
        real_gitdir, common = self._real_gitdir()
        is_git = bool(real_gitdir) and os.path.realpath(
            find_git_toplevel(self.worktree) or "") == self.worktree
        max_files = int(cfg.get("max_files", 100000))
        if is_git:
            n = self._index_entry_count(os.path.join(real_gitdir, "index"))
            if n > max_files:
                self.disable("%d tracked files exceed max_files=%d" % (n, max_files), hours=24 * 7)
                raise Disabled(self.meta()["disabled_reason"])
        else:
            n = self._count_files(cfg, int(cfg.get("scan_ms", 400)))
            if n is None:
                self.disable("more than max_files=%d files (or the scan took over %d ms)"
                             % (max_files, int(cfg.get("scan_ms", 400))), hours=24)
                raise Disabled(self.meta()["disabled_reason"])
        if os.path.isdir(self.gitdir):
            shutil.rmtree(self.gitdir, ignore_errors=True)
        run_git(["init", "-q", "--bare", self.gitdir], timeout=10.0)
        for k, v in (("core.bare", "false"), ("core.autocrlf", "false"), ("core.safecrlf", "false"),
                     ("core.fsmonitor", "false"), ("core.untrackedCache", "false"), ("core.quotepath", "false"),
                     ("core.splitIndex", "false"), ("gc.auto", "0"), ("advice.addEmbeddedRepo", "false"),
                     ("core.precomposeunicode", "true"), ("index.skipHash", "false")):
            run_git(["--git-dir", self.gitdir, "config", k, v], timeout=5.0)
        meta = {"worktree": self.worktree, "created": time.time(), "is_git": is_git, "big_files": []}
        seeded = False
        if is_git:
            objects = os.path.join(common, "objects")
            if os.path.isdir(objects):
                alt = os.path.join(self.gitdir, "objects", "info")
                os.makedirs(alt, exist_ok=True)
                with open(os.path.join(alt, "alternates"), "w") as fh:
                    fh.write(objects + "\n")
            real_excl = os.path.join(common, "info", "exclude")
            if os.path.isfile(real_excl):
                meta["real_info_exclude"] = real_excl
            if self._seed_is_safe(real_gitdir):
                try:
                    shutil.copyfile(os.path.join(real_gitdir, "index"), os.path.join(self.gitdir, "index"))
                    self.git(["write-tree"], timeout=10.0)  # fails on unmerged/odd indexes
                    seeded = True
                except (OSError, SnapshotError):
                    try:
                        os.remove(os.path.join(self.gitdir, "index"))
                    except OSError:
                        pass
        meta["seeded"] = seeded
        self.save_meta(meta)
        self.write_excludes(cfg)
        meta = self.meta()
        meta["ready"] = True
        self.save_meta(meta)

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, cfg: dict, first: bool = False) -> str:
        budget = int(cfg.get("first_snapshot_ms" if first else "snapshot_ms", 3000)) / 1000.0
        start = time.time()
        max_bytes = int(cfg.get("max_file_bytes", 20 * 1024 * 1024))
        max_files = int(cfg.get("max_files", 100000))
        try:
            out = self.git(["ls-files", "-z", "-m", "-d", "-o", "--exclude-standard"], timeout=budget)
        except SnapshotError as e:
            if "timed out" in str(e):
                self.disable("listing changes took longer than %d ms" % int(budget * 1000), hours=6)
                raise Disabled(self.meta()["disabled_reason"])
            raise
        paths = []
        seen = set()
        big = []
        for raw in out.split(b"\0"):
            if not raw or raw.endswith(b"/") or raw in seen:
                continue  # nested repositories show up as "dir/"
            seen.add(raw)
            full = os.path.join(self.worktree, os.fsdecode(raw))
            try:
                st = os.lstat(full)
                if st.st_size > max_bytes and not os.path.islink(full):
                    big.append(os.fsdecode(raw))
                    continue
            except OSError:
                pass  # deleted: update-index --remove handles it
            paths.append(raw)
        if len(paths) > max_files:
            self.disable("%d changed/untracked files exceed max_files=%d" % (len(paths), max_files), hours=24)
            raise Disabled(self.meta()["disabled_reason"])
        if big:
            meta = self.meta()
            known = set(meta.get("big_files", []))
            new = [b for b in big if b not in known]
            if new:
                meta["big_files"] = sorted(known.union(new))[-5000:]
                self.save_meta(meta)
                self.write_excludes(cfg)
            # drop already-tracked big files from the shadow index so they stay out
            self.git(["update-index", "-z", "--force-remove", "--stdin"],
                     input_bytes=b"\0".join(os.fsencode(b) for b in big) + b"\0", timeout=budget, check=False)
        remaining = max(1.0, budget - (time.time() - start))
        if paths:
            try:
                self.git(["update-index", "-z", "--add", "--remove", "--replace", "--stdin"],
                         input_bytes=b"\0".join(paths) + b"\0", timeout=remaining)
            except SnapshotError as e:
                if "timed out" in str(e):
                    self.disable("hashing changed files took longer than %d ms" % int(budget * 1000), hours=6)
                    raise Disabled(self.meta()["disabled_reason"])
                debug("update-index failed (%s); falling back to add -A" % e)
                self.git(["add", "-A", "--ignore-errors", "--", "."], timeout=remaining, check=False)
        tree = self.git(["write-tree"], timeout=max(1.0, budget)).decode().strip()
        elapsed = time.time() - start
        debug("snapshot %s in %d ms (%d paths)" % (tree[:10], elapsed * 1000, len(paths)))
        return tree

    # ------------------------------------------------------------------ queries
    def diff(self, t1: str, t2: str):
        """[(status, relpath)] with status in A (created), D (deleted), M (modified), T (type change)."""
        if t1 == t2:
            return []
        out = self.git(["diff-tree", "-r", "-z", "--no-renames", "--name-status", t1, t2], timeout=20.0)
        parts = out.split(b"\0")
        res = []
        i = 0
        while i + 1 < len(parts):
            st = parts[i].decode()
            if not st:
                i += 1
                continue
            res.append((st[0], os.fsdecode(parts[i + 1])))
            i += 2
        return res

    def entry_info(self, tree: str, rel: str):
        """(mode, blob sha) of rel in tree, or None."""
        out = self.git(["--literal-pathspecs", "ls-tree", "-z", tree, "--", rel], timeout=10.0, check=False)
        for rec in out.split(b"\0"):
            if not rec:
                continue
            head, _, path = rec.partition(b"\t")
            mode, typ, sha = head.split(b" ")
            if os.fsdecode(path) == rel and typ == b"blob":
                return mode.decode(), sha.decode()
        return None

    def read_blob(self, sha: str) -> bytes:
        return self.git(["cat-file", "blob", sha], timeout=30.0)

    def blob_size(self, sha: str) -> int:
        try:
            return int(self.git(["cat-file", "-s", sha], timeout=10.0).strip())
        except (SnapshotError, ValueError):
            return -1

    # ------------------------------------------------------------------ pending (pre -> post)
    def pending_path(self, tool_use_id: str) -> str:
        from .util import safe_id
        return os.path.join(self.pending_dir, safe_id(tool_use_id) + ".json")

    def save_pending(self, tool_use_id: str, data: dict) -> None:
        write_json_atomic(self.pending_path(tool_use_id), data)

    def pop_pending(self, tool_use_id: str):
        path = self.pending_path(tool_use_id)
        data = read_json(path, None)
        try:
            os.remove(path)
        except OSError:
            pass
        return data

    def prune_pending(self, max_age_s: float = 86400) -> None:
        try:
            for name in os.listdir(self.pending_dir):
                p = os.path.join(self.pending_dir, name)
                if time.time() - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
        except OSError:
            pass

    # ------------------------------------------------------------------ undo log
    def load_entries(self):
        res = []
        try:
            with open(self.entries_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            res.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            pass
        return res

    def save_entries(self, entries) -> None:
        tmp = self.entries_path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.entries_path)

    def add_entry(self, entry: dict, keep: int) -> dict:
        entries = self.load_entries()
        entry["id"] = (entries[-1]["id"] + 1) if entries else 1
        entry.setdefault("ts", time.time())
        entries.append(entry)
        if len(entries) > keep:
            entries = entries[-keep:]
        self.save_entries(entries)
        return entry

    def update_entry(self, entry: dict) -> None:
        entries = self.load_entries()
        for i, e in enumerate(entries):
            if e.get("id") == entry.get("id"):
                entries[i] = entry
        self.save_entries(entries)


def write_blob_to(store: Store, tree: str, rel: str) -> bool:
    """Restore rel from tree into the work tree. Returns False if rel is not in tree."""
    info = store.entry_info(tree, rel)
    if not info:
        return False
    mode, sha = info
    data = store.read_blob(sha)
    full = os.path.join(store.worktree, rel)
    parent = os.path.dirname(full)
    if os.path.lexists(parent) and not os.path.isdir(parent):
        os.remove(parent)
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(full) or (os.path.lexists(full) and not os.path.isfile(full)):
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    if mode == "120000":
        if os.path.lexists(full):
            os.remove(full)
        os.symlink(os.fsdecode(data), full)
        return True
    tmp = full + ".bash-bridge-restore"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o755 if mode == "100755" else 0o644)
    os.replace(tmp, full)
    return True


def remove_path(store: Store, rel: str) -> None:
    full = os.path.join(store.worktree, rel)
    if os.path.islink(full) or os.path.isfile(full):
        os.remove(full)
    # remove now-empty parent directories (never the work tree itself)
    d = os.path.dirname(full)
    while d and d != store.worktree and d.startswith(store.worktree + os.sep):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


__all__ = ["Store", "SnapshotError", "Disabled", "find_git_toplevel", "git_blob_sha", "write_blob_to",
           "remove_path", "store_dir_for", "ZERO_TREE", "now_ms"]
