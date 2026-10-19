"""Regression tests for the bugs found by the live stress test (34 real claude -p sessions)."""
import fcntl
import json
import os
import stat
import time

from helpers import LOG_BODY, BridgeCase, sh, unittest

from test_review import state_store

GUARD = 'cat >/dev/null; echo "protected.txt is protected" >&2; exit 2\n'


class Bug1OrphanPending(BridgeCase):
    """p20d: a denied Bash call (pre ran, post never comes) must not switch the guard off."""

    def setUp(self):
        super().setUp()
        self.write("protected.txt", "keep\n")
        self.commit()
        script = self.hook_script("guard.sh", 'in=$(cat); case "$in" in *protected.txt*) '
                                              'echo "protected.txt is protected" >&2; exit 2;; esac\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})

    def deny_touch(self):
        self.assertEqual(self.run_hook("pre", self.event("pre", "touch made.txt", "toolu_denied"))[:2], (0, ""))

    def test_batch_hook_clears_denied_call(self):
        self.deny_touch()
        ev = {"session_id": self.session, "hook_event_name": "PostToolBatch", "cwd": self.proj,
              "tool_calls": [{"tool_name": "Bash", "tool_input": {"command": "touch made.txt"},
                              "tool_use_id": "toolu_denied", "tool_response": "Permission denied"}]}
        self.assertEqual(self.run_hook("batch", ev)[:2], (0, ""))
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertEqual(self.read("protected.txt"), "keep\n")

    def test_permission_denied_hook_clears_call(self):
        self.deny_touch()
        ev = self.event("pre", "touch made.txt", "toolu_denied")
        ev["hook_event_name"] = "PermissionDenied"
        self.assertEqual(self.run_hook("permission-denied", ev)[:2], (0, ""))
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])

    def test_denied_call_in_the_same_batch(self):
        """The real p20d shape: `touch` (denied by a rule) and the echo in ONE batch. The pre hooks of
        both run before the denial, so only the transcript shows that touch never ran."""
        self.deny_touch()
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        with open(self.transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result",
                     "tool_use_id": "toolu_denied", "is_error": True,
                     "content": "Permission to use Bash with command touch made.txt has been denied."}]},
                     "toolDenialKind": "permission-rule"}) + "\n")
        sh("echo hacked >> protected.txt", self.proj)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("restored", json.loads(out)["reason"])
        self.assertEqual(self.read("protected.txt"), "keep\n")

    def test_same_batch_without_transcript_evidence_stays_ambiguous(self):
        self.deny_touch()
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        sh("echo hacked >> protected.txt", self.proj)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("at the same time", json.loads(out)["reason"])

    def test_other_session_in_flight_still_counts_as_overlap(self):
        other = self.event("pre", "echo z > z.txt", "toolu_other")
        other["session_id"] = "another-session"
        self.run_hook("pre", other)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("at the same time", out["reason"])
        # the other session's batch hook does not clear *our* entries, and ours not theirs
        ev = {"session_id": self.session, "hook_event_name": "PostToolBatch", "cwd": self.proj}
        self.run_hook("batch", ev)
        self.assertEqual(len(os.listdir(os.path.join(state_store(self), "pending"))), 1)


class Bug2Classifier(BridgeCase):
    def test_writers_are_not_read_only(self):
        from bashbridge.classify import is_read_only
        for cmd in ("sed -Ei '' 's/a/b/' f", "sed -ni 'p' f", "sed -ri 's/a/b/' f", "sed -i.bak s/a/b/ f",
                    "sed --in-place s/a/b/ f", "git stash", "git -C . stash", "git branch -D x", "git tag v1",
                    "git config user.name x", "tree -o out.txt", "xxd in out", "rg --pre ./x.sh foo"):
            self.assertFalse(is_read_only(cmd), cmd)
        for cmd in ("sed -n 1,5p f", "sed -E s/a/b/ f", "git status", "git log -1", "tree", "xxd f", "rg foo"):
            self.assertTrue(is_read_only(cmd), cmd)

    def test_combined_sed_flags_are_snapshotted(self):
        self.write("f.txt", "aaa\n")
        self.commit()
        self.bash("sed -Ei '' 's/a+/b/' f.txt")
        self.assertEqual(self.last_changes(), [("M", "f.txt")])


class Bug3CommandNotInPayload(BridgeCase):
    def test_text_matching_guard_only_hits_the_protected_file(self):
        """t44: `cat protected.txt` in the command made a grep-guard fire for every file."""
        self.write("protected.txt", "p\n")
        for i in range(5):
            self.write("page%d.txt" % i, "x\n")
        self.commit()
        script = self.hook_script("guard.sh", 'in=$(cat); echo "$BASH_BRIDGE_COMMAND" >> "$HOOK_LOG.cmd"; '
                                              'case "$in" in *protected.txt*) echo no >&2; exit 2;; esac\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        cmd = "cat protected.txt > /dev/null; for f in page*.txt; do echo y >> $f; done"
        out = self.bash(cmd)
        self.assertIsNone(out, "no page file is denied")
        for i in range(5):
            self.assertEqual(self.read("page%d.txt" % i), "x\ny\n")
        with open(self.log + ".cmd") as fh:
            self.assertIn("cat protected.txt", fh.read(), "the command is available as $BASH_BRIDGE_COMMAND")
        log = self.hook_script("l.sh", LOG_BODY)
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": log}]}]}})
        self.bash(cmd)
        for e in self.hook_log():
            self.assertNotIn("command", e["bash_bridge"])
            self.assertNotIn("cat protected", json.dumps(e))


class Bug4LockTimeout(BridgeCase):
    def test_busy_store_is_reported_and_marks_overlap(self):
        """p32: a pre hook that can't get the lock must not let its change land in another entry silently."""
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.commit()
        self.bash("echo warm >> a.txt")
        self.run_hook("pre", self.event("pre", "echo A >> a.txt", "toolu_A"))
        lock = open(os.path.join(state_store(self), "lock"), "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            self.assertEqual(self.run_hook("pre", self.event("pre", "echo B >> b.txt", "toolu_B"))[:2], (0, ""))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        sh("echo A >> a.txt; echo B >> b.txt", self.proj)
        self.run_hook("post", self.event("post", "echo A >> a.txt", "toolu_A"))
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo B >> b.txt", "toolu_B"))
        self.assertIn("could not take a snapshot", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        last = self.entries()[-1]
        self.assertTrue(last["overlap"], "the entry that may contain B's change is flagged")
        code, out, _ = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 2)
        self.assertEqual(self.read("b.txt"), "b\nB\n", "B's change is not undone by undoing A")


class Bug5ManyUntracked(BridgeCase):
    def test_untracked_cap_degrades_only_the_call(self):
        """Review 3: too many untracked files no longer switch bash-bridge off for 7 days; the call
        snapshots tracked files only and says so (to the user and to Claude), rate-limited."""
        self.write("a.txt", "a\n")
        self.commit()
        for i in range(80):
            self.write("gen/%03d.txt" % i, "x")
        env = dict(self.env, BASH_BRIDGE_MAX_UNTRACKED="50")
        start = time.time()
        out = self.bash("echo x >> a.txt && echo n > gen/new.txt", env=env)
        self.assertLess(time.time() - start, 5)
        self.assertIn("max_untracked", out["systemMessage"])
        self.assertIn("max_untracked", self.context(out))
        self.assertEqual(self.last_changes(), [("M", "a.txt")], "tracked files are still covered")
        store = state_store(self)
        with open(os.path.join(store, "meta.json")) as fh:
            self.assertLess(json.load(fh).get("disabled_until", 0), time.time())
        self.assertTrue(os.path.isdir(os.path.join(store, "shadow.git")))
        out = self.bash("echo y >> a.txt", env=env)
        self.assertIsNone(out, "the note is rate-limited")
        code, txt, _ = self.cli("undo", "--project", self.proj, env=env)
        self.assertEqual(code, 0, txt)
        self.assertEqual(self.read("a.txt"), "a\nx\n")
        # back under the cap: untracked files are tracked again, nothing shows up as created
        sh("rm -r gen", self.proj)
        self.bash("echo z > new.txt", env=env)
        self.assertEqual(self.last_changes(), [("A", "new.txt")])


class Bug6TooLarge(BridgeCase):
    def test_grown_file_reported_as_too_large_not_deleted(self):
        self.write("big.txt", "x" * 500)
        self.commit()
        self.logging_hook("Write|Edit")
        env = dict(self.env, BASH_BRIDGE_MAX_FILE_BYTES="1000")
        self.bash("head -c 3000 /dev/zero | tr '\\0' 'y' >> big.txt", env=env)
        self.assertEqual(self.last_changes(), [("B", "big.txt")])
        listing = self.cli("list", "--project", self.proj, env=env)[1]
        self.assertIn("too large", listing)
        self.assertNotIn("deleted", listing)
        log = self.hook_log()
        self.assertEqual([(e["tool_name"], e["tool_input"]["content"]) for e in log], [("Write", "")])
        code, out, _ = self.cli("undo", "--project", self.proj, env=env)
        self.assertIn("TOO LARGE", out)
        self.assertNotIn("changed again", out)
        # shrinking is noticed on the next snapshot (no permanent exclusion)
        self.bash("printf small > big.txt", env=env)
        self.assertEqual(self.last_changes(), [("B", "big.txt")])
        self.bash("printf again > big.txt", env=env)
        self.assertEqual(self.last_changes(), [("M", "big.txt")])


class Bug7RestoreModes(BridgeCase):
    def test_recreated_dirs_and_files_follow_user_umask(self):
        old = os.umask(0o022)
        try:
            self.write("deep/dir/f.txt", "f\n")
            self.commit()
            self.bash("rm -r deep")
            code, out, err = self.cli("undo", "--project", self.proj)
            self.assertEqual(code, 0, out + err)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.proj, "deep")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.proj, "deep/dir")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.proj, "deep/dir/f.txt")).st_mode), 0o644)


class Bug8FileReplacedByDir(BridgeCase):
    def test_undo_in_one_go_and_marked_done(self):
        self.write("thing", "file\n")
        self.commit()
        self.bash("rm thing && mkdir thing && echo x > thing/inner")
        code, out, err = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read("thing"), "file\n")
        self.assertTrue(self.entries()[-1]["undone"])
        code, out, _ = self.cli("undo", "--project", self.proj)
        self.assertIn("nothing to undo", out)


class Bug9Unreadable(BridgeCase):
    def test_unreadable_new_file_recorded_and_no_fallback(self):
        self.write("a.txt", "a\n")
        self.commit()
        self.bash("echo s > secret.txt && chmod 000 secret.txt && echo b > b.txt")
        try:
            self.assertEqual(self.last_changes(), [("A", "b.txt"), ("U", "secret.txt")])
            self.bash("echo c >> a.txt")
            self.assertEqual(self.last_changes(), [("M", "a.txt")])
            with open(os.path.join(self.tmp, "debug.log")) as fh:
                self.assertNotIn("retrying path by path", fh.read())
            self.assertIn("unreadable", self.cli("list", "--project", self.proj)[1])
        finally:
            os.chmod(os.path.join(self.proj, "secret.txt"), 0o644)


class Bug10And11Cli(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.commit()

    def test_list_shows_expired_when_blob_missing(self):
        self.bash("echo x > a.txt")
        sh("rm -r '%s'" % os.path.join(state_store(self), "blobs"), self.proj)
        self.assertIn("expired", self.cli("list", "--project", self.proj)[1])

    def test_newline_in_name_is_escaped(self):
        self.bash("printf x > \"$(printf 'bad\\nname')\"")
        out = self.cli("list", "--project", self.proj)[1]
        self.assertIn("bad\\nname", out)

    def test_git_rm_hint(self):
        self.bash("git rm -q a.txt")
        code, out, _ = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("a.txt"), "a\n")
        self.assertIn("git index", out)


class PerfParallelHooks(BridgeCase):
    def test_guard_over_many_files_runs_in_parallel(self):
        for i in range(12):
            self.write("p%02d.txt" % i, "x\n")
        self.commit()
        script = self.hook_script("slow.sh", "cat >/dev/null; sleep 0.5\n")
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        start = time.time()
        self.bash("for f in p*.txt; do echo y >> $f; done")
        elapsed = time.time() - start
        print("\n    guard over 12 files with a 0.5 s hook: %.1f s (sequential would be >= 6 s)" % elapsed)
        self.assertLess(elapsed, 4.5)


if __name__ == "__main__":
    unittest.main()
