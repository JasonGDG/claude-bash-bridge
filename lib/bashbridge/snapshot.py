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
import zlib

from .util import UnsafeState, check_private_dir, check_private_file, debug, now_ms, read_json, sha_key, state_dir, write_json_atomic

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


KNOWN_INDEX_EXTENSIONS = {b"TREE", b"REUC", b"EOIE", b"IEOT"}


def index_is_plain(data: bytes) -> bool:
    """True for a v2/v3 SHA-1 index whose checksum is valid, with no extended entry flags
    (skip-worktree, intent-to-add) and only extensions that mean the same in the shadow repo
    (cache tree, resolve-undo, offset tables). Anything else is not seeded."""
    try:
        if len(data) < 32 or data[:4] != b"DIRC":
            return False
        ver = int.from_bytes(data[4:8], "big")
        if ver not in (2, 3):
            return False
        body, trailer = data[:-20], data[-20:]
        if trailer != b"\0" * 20 and hashlib.sha1(body).digest() != trailer:
            return False
        n = int.from_bytes(data[8:12], "big")
        pos = 12
        end_entries = len(data) - 20
        for _ in range(n):
            flags = int.from_bytes(data[pos + 60:pos + 62], "big")
            if flags & 0x4000:
                return False
            namelen = flags & 0x0FFF
            name_end = pos + 62 + namelen if namelen < 0xFFF else data.index(b"\0", pos + 62)
            pos += ((name_end - pos) // 8 + 1) * 8
            if pos > end_entries:
                return False
        while pos < end_entries:
            sig = data[pos:pos + 4]
            size = int.from_bytes(data[pos + 4:pos + 8], "big")
            if sig not in KNOWN_INDEX_EXTENSIONS:
                debug("index extension %r: not seeding" % sig)
                return False
            pos += 8 + size
        return pos == end_entries
    except (ValueError, IndexError):
        return False


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
        self.blobs_dir = os.path.join(self.dir, "blobs")
        for p in (self.dir, self.gitdir, self.pending_dir, self.blobs_dir):
            if os.path.lexists(p):
                check_private_dir(p)  # no symlinks, ours, 0700
        for name in ("meta.json", "entries.jsonl", "lock", "last-gc"):
            check_private_file(os.path.join(self.dir, name))  # regular, ours, 0600

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
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.dir, "lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fh = os.fdopen(fd, "a+")
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

    def reset(self) -> None:
        meta = self.meta()
        meta["ready"] = False
        self.save_meta(meta)
        shutil.rmtree(self.gitdir, ignore_errors=True)

    def disable(self, reason: str, hours: float = 24.0) -> None:
        meta = self.meta()
        meta["disabled_until"] = time.time() + hours * 3600
        meta["disabled_reason"] = reason
        meta["worktree"] = self.worktree
        meta["ready"] = False
        self.save_meta(meta)
        # free the space: a half-built shadow repo is useless while disabled
        shutil.rmtree(self.gitdir, ignore_errors=True)
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
        """Only copy the project's index when the shadow repo will read it the same way."""
        index = os.path.join(real_gitdir, "index")
        if not os.path.isfile(index) or os.path.exists(index + ".lock"):
            return False
        try:
            with open(index, "rb") as fh:
                head = fh.read(12)
            if head[:4] != b"DIRC" or int.from_bytes(head[4:8], "big") not in (2, 3, 4):
                return False
            if any(n.startswith("sharedindex.") for n in os.listdir(real_gitdir)):
                return False
        except OSError:
            return False
        try:
            cfg = run_git(["-C", self.worktree, "config", "--get-regexp",
                           r"^(core\.(autocrlf|splitindex|sparsecheckout|sparsecheckoutcone|fsmonitor|untrackedcache)"
                           r"|index\.sparse|feature\.manyfiles|extensions\.worktreeconfig|extensions\.objectformat)$"],
                          timeout=3.0, check=False)
            for line in cfg.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].lower() not in ("false", "0", "no", "off", "sha1"):
                    return False
            if os.path.exists(os.path.join(real_gitdir, "info", "sparse-checkout")):
                return False
            # assume-unchanged (lowercase tag) or skip-worktree (S) entries hide changes from ls-files -m
            tags = run_git(["-C", self.worktree, "ls-files", "-v", "-z"], timeout=10.0)
            for rec in tags.split(b"\0"):
                if rec and (rec[:1].islower() or rec[:1] == b"S"):
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
        for rel in list(extra):
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
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
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
            n = self._count_files(dict(cfg, max_files=min(int(cfg.get("max_files", 100000)),
                                                          int(cfg.get("max_untracked", 5000)))),
                                  int(cfg.get("scan_ms", 400)))
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
                     ("core.precomposeunicode", "true"), ("index.skipHash", "false"),
                     ("core.looseCompression", "1")):
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
                    src = os.path.join(real_gitdir, "index")
                    with open(src, "rb") as fh:
                        data = fh.read()
                    if not index_is_plain(data):
                        raise OSError("project index has features the shadow repo can't share")
                    with open(os.path.join(self.gitdir, "index"), "wb") as fh:
                        fh.write(data)
                    with open(src, "rb") as fh:
                        again = fh.read()
                    if again != data or os.path.exists(src + ".lock"):
                        raise OSError("project index changed while copying")
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
        self.big, self.unreadable = {}, []
        untracked = 0
        for raw in out.split(b"\0"):
            if not raw or raw.endswith(b"/") or raw in seen:
                continue  # nested repositories show up as "dir/"
            seen.add(raw)
            rel = os.fsdecode(raw)
            full = os.path.join(self.worktree, rel)
            try:
                st = os.lstat(full)
                if not os.path.islink(full):
                    if st.st_size > max_bytes:
                        # re-evaluated on every snapshot; kept out of the shadow index meanwhile
                        self.big[rel] = [st.st_size, st.st_mtime_ns]
                        continue
                    if os.path.isfile(full) and not os.access(full, os.R_OK):
                        self.unreadable.append(rel)
                        continue
                untracked += 1
            except OSError:
                pass  # deleted: update-index --remove handles it
            paths.append(raw)
        max_untracked = int(cfg.get("max_untracked", 5000))
        if first and untracked > max_untracked and self.meta().get("is_git"):
            # unseeded git projects list tracked files too; count only what git itself calls untracked
            try:
                real = run_git(["-C", self.worktree, "ls-files", "-o", "-z", "--exclude-standard"], timeout=budget)
                untracked = len([r for r in real.split(b"\0") if r and not r.endswith(b"/")])
            except SnapshotError:
                pass
        if first and untracked > max_untracked:
            self.disable("%d files to snapshot (not ignored by .gitignore) exceed max_untracked=%d"
                         % (untracked, max_untracked), hours=24 * 7)
            raise Disabled(self.meta()["disabled_reason"])
        if len(paths) > max_files:
            self.disable("%d changed/untracked files exceed max_files=%d" % (len(paths), max_files), hours=24 * 7)
            raise Disabled(self.meta()["disabled_reason"])
        if self.big:
            self.git(["update-index", "-z", "--force-remove", "--stdin"],
                     input_bytes=b"\0".join(os.fsencode(b) for b in self.big) + b"\0", timeout=budget, check=False)
        remaining = max(1.0, budget - (time.time() - start))
        if paths:
            try:
                self.git(["update-index", "-z", "--add", "--remove", "--replace", "--stdin"],
                         input_bytes=b"\0".join(paths) + b"\0", timeout=remaining)
            except SnapshotError as e:
                if "timed out" in str(e):
                    self.disable("hashing changed files took longer than %d ms" % int(budget * 1000), hours=6)
                    raise Disabled(self.meta()["disabled_reason"])
                debug("update-index failed (%s); retrying path by path" % e)
                for raw in paths[:2000]:
                    self.git(["update-index", "-z", "--add", "--remove", "--replace", "--stdin"],
                             input_bytes=raw + b"\0", timeout=remaining, check=False)
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
        check_private_file(path)
        data = read_json(path, None)
        try:
            os.remove(path)
        except OSError:
            pass
        return data

    def fresh_pending(self, exclude: str, session: str = "", agent: str = "", max_age_s: float = 120):
        """Other calls in this project that took a pre-snapshot and are not finished.
        Pending entries of calls that never ran (denied) are removed by the PermissionDenied and
        PostToolBatch hooks; the short window only matters on Claude Code versions without them."""
        from .util import safe_id
        res = []
        mine = safe_id(exclude) + ".json"
        try:
            names = os.listdir(self.pending_dir)
        except OSError:
            return res
        for name in names:
            p = os.path.join(self.pending_dir, name)
            try:
                if name == mine or not name.endswith(".json") or time.time() - os.path.getmtime(p) > max_age_s:
                    continue
            except OSError:
                continue
            data = read_json(p, None)
            if not isinstance(data, dict) or data.get("disabled"):
                continue
            res.append(p)
        return res

    def drop_pending_of(self, session: str, agent: str) -> int:
        n = 0
        try:
            names = os.listdir(self.pending_dir)
        except OSError:
            return 0
        for name in names:
            p = os.path.join(self.pending_dir, name)
            data = read_json(p, None)
            if isinstance(data, dict) and data.get("session") == session and (data.get("agent") or "") == agent:
                try:
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
        return n

    def mark_overlap(self, path: str) -> None:
        data = read_json(path, None)
        if isinstance(data, dict) and not data.get("overlap"):
            data["overlap"] = True
            write_json_atomic(path, data)

    def prune_pending(self, max_age_s: float = 86400) -> None:
        try:
            for name in os.listdir(self.pending_dir):
                p = os.path.join(self.pending_dir, name)
                if time.time() - os.path.getmtime(p) > max_age_s:
                    os.remove(p)
        except OSError:
            pass

    # ------------------------------------------------------------------ pre-image store
    # Undo does not rely on git objects (the project's `git gc` could prune objects the shadow
    # repo only borrows, and snapshot trees are unreferenced). Every pre-image an undo entry needs
    # is copied into blobs/<sha[:2]>/<sha> (zlib, 0600) and deleted together with its entry.
    def blob_path(self, sha: str) -> str:
        return os.path.join(self.blobs_dir, sha[:2], sha[2:])

    def tree_entries(self, tree: str, rels):
        """{rel: (mode, sha)} for the blobs (and symlinks) among rels in tree."""
        res = {}
        rels = list(rels)
        if not rels:
            return res
        for i in range(0, len(rels), 500):
            chunk = rels[i:i + 500]
            out = self.git(["--literal-pathspecs", "ls-tree", "-r", "-z", tree, "--"] + chunk,
                           timeout=30.0, check=False)
            for rec in out.split(b"\0"):
                if not rec:
                    continue
                head, _, path = rec.partition(b"\t")
                parts = head.split(b" ")
                if len(parts) == 3 and parts[1] == b"blob":
                    res[os.fsdecode(path)] = (parts[0].decode(), parts[2].decode())
        return res

    def keep_blobs(self, shas, max_bytes: int) -> set:
        """Copy the given blobs into the pre-image store, streaming (bounded memory) and within
        max_bytes in total; sizes are checked from `cat-file --batch-check` before anything is read.
        Returns the shas that are stored."""
        import threading
        stored, todo = set(), []
        for sha in shas:
            if os.path.isfile(self.blob_path(sha)):
                stored.add(sha)
            else:
                todo.append(sha)
        if not todo:
            return stored
        sizes = {}
        out = self.git(["cat-file", "--batch-check"], input_bytes=("\n".join(todo) + "\n").encode(),
                       timeout=60.0, check=False)
        for line in out.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == "blob":
                sizes[parts[0]] = int(parts[2])
        chosen, total = [], 0
        for sha in todo:
            size = sizes.get(sha)
            if size is None or total + size > max_bytes:
                debug("pre-image %s not kept (%s bytes, cap %d)" % (sha, size, max_bytes))
                continue
            total += size
            chosen.append(sha)
        if not chosen:
            return stored
        proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=self.worktree, env=self.env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def feed():
            try:
                proc.stdin.write(("\n".join(chosen) + "\n").encode())
                proc.stdin.close()
            except OSError:
                pass

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        try:
            for sha in chosen:
                header = proc.stdout.readline().split()
                if len(header) != 3:
                    continue
                size = int(header[2])
                path = self.blob_path(sha)
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                tmp = path + ".%d.tmp" % os.getpid()
                comp = zlib.compressobj(1)
                left = size
                with open(tmp, "wb") as fh:
                    while left > 0:
                        chunk = proc.stdout.read(min(1 << 20, left))
                        if not chunk:
                            break
                        left -= len(chunk)
                        fh.write(comp.compress(chunk))
                    fh.write(comp.flush())
                proc.stdout.read(1)  # trailing newline
                if left:
                    os.remove(tmp)
                    break
                os.replace(tmp, path)
                stored.add(sha)
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
        return stored

    def store_bytes(self) -> int:
        total = 0
        for d, _s, files in os.walk(self.blobs_dir):
            for f in files:
                try:
                    total += os.lstat(os.path.join(d, f)).st_size
                except OSError:
                    pass
        return total

    def load_blob(self, sha: str):
        """Pre-image bytes, or None if expired / missing / corrupt."""
        try:
            fd = os.open(self.blob_path(sha), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise UnsafeState("pre-image %s is owned by another user" % sha)
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "rb") as fh:
                fd = None
                data = zlib.decompress(fh.read())
        except (OSError, zlib.error):
            return None
        finally:
            if fd is not None:
                os.close(fd)
        h = hashlib.sha1(b"blob %d\0" % len(data))
        h.update(data)
        return data if h.hexdigest() == sha else None

    def record_files(self, entry: dict, cfg: dict) -> None:
        """Fill entry["files"]: per changed path the pre and post (mode, sha); keep the pre-images."""
        rels = [rel for _st, rel in entry["changes"]]
        pre = self.tree_entries(entry["pre"], rels)
        post = self.tree_entries(entry["post"], rels)
        cap = int(cfg.get("undo_max_bytes", 200 * 1024 * 1024))
        stored = self.keep_blobs(sorted({v[1] for v in pre.values()}), cap)
        files = []
        for st, rel in entry["changes"]:
            p = pre.get(rel)
            files.append({"st": st, "rel": rel, "pre": list(p) if p else None,
                          "post": list(post[rel]) if rel in post else None,
                          "kept": (p is None) or (p[1] in stored)})
        entry["files"] = files

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

    def add_entry(self, entry: dict, keep: int, max_age_days: float = 30.0,
                  store_max: int = 1024 * 1024 * 1024) -> dict:
        entries = self.load_entries()
        entry["id"] = (entries[-1]["id"] + 1) if entries else 1
        entry.setdefault("ts", time.time())
        entries.append(entry)
        cutoff = time.time() - max_age_days * 86400
        kept = [e for e in entries if e.get("ts", 0) >= cutoff][-keep:]
        if len(kept) != len(entries):
            self.save_entries(kept)
            self.gc_blobs(kept)
        # project-wide cap: drop the oldest entries (and their pre-images) until it fits
        while len(kept) > 1 and self.store_bytes() > store_max:
            debug("pre-image store over %d bytes: expiring entry %s" % (store_max, kept[0].get("id")))
            kept = kept[1:]
            self.save_entries(kept)
            self.gc_blobs(kept)
        self.save_entries(kept)
        return entry

    def gc_blobs(self, entries) -> None:
        """Delete pre-images no remaining entry refers to."""
        live = set()
        for e in entries:
            for f in e.get("files") or []:
                if f.get("pre"):
                    live.add(f["pre"][1])
        try:
            for d in os.listdir(self.blobs_dir):
                sub = os.path.join(self.blobs_dir, d)
                for name in os.listdir(sub):
                    if d + name not in live:
                        os.remove(os.path.join(sub, name))
        except OSError:
            pass

    def update_entry(self, entry: dict) -> None:
        entries = self.load_entries()
        for i, e in enumerate(entries):
            if e.get("id") == entry.get("id"):
                entries[i] = entry
        self.save_entries(entries)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _open_parent(store: Store, rel: str, create: bool):
    """Open the directory that will contain rel, one component at a time relative to the
    previous directory's fd and with O_NOFOLLOW, so no symlink (even one swapped in while we
    work) can lead outside the project. Returns (dir_fd, basename). Caller closes dir_fd."""
    parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise OSError("bad path %r" % rel)
    fd = os.open(store.worktree, os.O_RDONLY | _DIRECTORY)
    try:
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(comp, 0o755, dir_fd=fd)
                nfd = os.open(comp, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except (NotADirectoryError, OSError) as e:
                st = os.stat(comp, dir_fd=fd, follow_symlinks=False)
                import stat as _st
                if _st.S_ISLNK(st.st_mode):
                    raise OSError("refusing to follow the symlink %s in %s" % (comp, rel))
                if create and _st.S_ISREG(st.st_mode):
                    os.unlink(comp, dir_fd=fd)  # a file now sits where the directory was
                    os.mkdir(comp, 0o755, dir_fd=fd)
                    nfd = os.open(comp, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                else:
                    raise e
            os.close(fd)
            fd = nfd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def write_file(store: Store, rel: str, mode: str, data: bytes) -> None:
    """Atomically put (mode, data) at rel via dir fds: never follows a symlink, at rel or above it."""
    import stat as _st
    dfd, name = _open_parent(store, rel, create=True)
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if _st.S_ISDIR(st.st_mode):
                os.rmdir(name, dir_fd=dfd)  # only an empty directory; anything else raises
        except FileNotFoundError:
            pass
        tmp = ".%s.bash-bridge-%d" % (name[:80], os.getpid())
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        if mode == "120000":
            os.symlink(os.fsdecode(data), tmp, dir_fd=dfd)
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                os.fchmod(fh.fileno(), 0o755 if mode == "100755" else 0o644)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)  # replaces a symlink itself
    finally:
        os.close(dfd)


def write_blob_to(store: Store, tree: str, rel: str) -> bool:
    """Restore rel from a snapshot tree (used right after a command, while the tree is fresh)."""
    info = store.entry_info(tree, rel)
    if not info:
        return False
    write_file(store, rel, info[0], store.read_blob(info[1]))
    return True


def remove_path(store: Store, rel: str) -> None:
    """Delete a file the Bash call created (via dir fds, never through a symlink). Directories stay."""
    import stat as _st
    try:
        dfd, name = _open_parent(store, rel, create=False)
    except FileNotFoundError:
        return
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not _st.S_ISDIR(st.st_mode):
            os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


def _inside(path: str, root: str) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(root)
    return real == top or real.startswith(top + os.sep)


__all__ = ["Store", "SnapshotError", "Disabled", "find_git_toplevel", "git_blob_sha", "write_blob_to",
           "write_file", "remove_path", "store_dir_for", "ZERO_TREE", "now_ms", "disk_state"]
