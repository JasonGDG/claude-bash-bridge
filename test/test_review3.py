"""Regression tests for the third Codex review (findings R1 ... R17)."""
import json
import os
import stat
import time

from helpers import LOG_BODY, BridgeCase, sh, unittest

from test_review import state_store

GUARD = 'in=$(cat); case "$in" in *protected.txt*) echo "protected.txt is protected" >&2; exit 2;; esac\n'


def denied_record(tuid, kind="permission-rule", text=None):
    """The shape Claude Code 2.1.303 writes for a call that never ran (captured from a real transcript)."""
    return {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "is_error": True, "tool_use_id": tuid,
        "content": text or "Permission to use Bash with command touch made.txt has been denied."}]},
        "toolUseResult": "Error: " + (text or "Permission to use Bash with command touch made.txt has been denied."),
        "toolDenialKind": kind,
        "permissionDecision": {"decision": "reject", "source": "config", "reasonType": "subcommandResults"}}


def ran_record(tuid, output):
    return {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "is_error": False, "tool_use_id": tuid, "content": output}]},
        "toolUseResult": {"stdout": output, "stderr": "", "interrupted": False}}


class GuardCase(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("protected.txt", "keep\n")
        self.commit()
        script = self.hook_script("guard.sh", GUARD)
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})

    def transcript_add(self, *records):
        with open(self.transcript, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")


class R1DenialNotSpoofable(GuardCase):
    def test_call_that_ran_and_printed_denial_text_keeps_overlap(self):
        # call A really ran (and wrote protected.txt itself); its output quotes a denial message
        self.run_hook("pre", self.event("pre", "./a.sh", "toolu_A"))
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        sh("echo by-A >> protected.txt; echo hacked >> protected.txt", self.proj)
        self.transcript_add(ran_record("toolu_A", "Permission to use Bash with command x has been denied. "
                                                  "PreToolUse:Bash hook error"))
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("at the same time", json.loads(out)["reason"])
        self.assertEqual(self.read("protected.txt"), "keep\nby-A\nhacked\n", "A's change must not be rolled back")

    def test_error_result_with_denial_text_but_no_denial_kind_keeps_overlap(self):
        self.run_hook("pre", self.event("pre", "./a.sh", "toolu_A"))
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        sh("echo hacked >> protected.txt", self.proj)
        rec = ran_record("toolu_A", "Permission to use Bash with command touch made.txt has been denied.")
        rec["message"]["content"][0]["is_error"] = True
        self.transcript_add(rec)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("at the same time", json.loads(out)["reason"])

    def test_real_denial_record_still_clears_overlap(self):
        self.run_hook("pre", self.event("pre", "touch made.txt", "toolu_denied"))
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        self.transcript_add(denied_record("toolu_denied"))
        sh("echo hacked >> protected.txt", self.proj)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("restored", json.loads(out)["reason"])
        self.assertEqual(self.read("protected.txt"), "keep\n")

    def test_interrupted_call_is_not_a_denial(self):
        self.run_hook("pre", self.event("pre", "./a.sh", "toolu_A"))
        self.run_hook("pre", self.event("pre", "echo hacked >> protected.txt", "toolu_echo"))
        self.transcript_add(denied_record("toolu_A", kind="interrupted", text="[Tool call interrupted]"))
        sh("echo hacked >> protected.txt", self.proj)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo hacked >> protected.txt", "toolu_echo"))
        self.assertIn("at the same time", json.loads(out)["reason"])


class R2UndoKeepsConflictingAncestor(BridgeCase):
    def test_edited_file_that_replaced_a_dir_survives_undo(self):
        self.write("a/b", "old\n")
        self.commit()
        self.bash("rm -r a && echo new > a")
        self.write("a", "user edit\n")  # changed again after the Bash call
        code, out, err = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 2, out + err)
        self.assertTrue(os.path.isfile(os.path.join(self.proj, "a")), out)
        self.assertEqual(self.read("a"), "user edit\n")
        self.assertIn("SKIPPED", out)

    def test_force_still_restores_the_dir(self):
        self.write("a/b", "old\n")
        self.commit()
        self.bash("rm -r a && echo new > a")
        self.write("a", "user edit\n")
        code, out, err = self.cli("undo", "--force", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read("a/b"), "old\n")

    def test_restore_never_unlinks_a_file_in_the_way(self):
        from bashbridge.snapshot import Store, write_file
        self.write("x", "precious\n")
        os.environ["BASH_BRIDGE_STATE_DIR"] = self.state
        try:
            with self.assertRaises(OSError):
                write_file(Store(self.proj), "x/y", "100644", b"data")
        finally:
            del os.environ["BASH_BRIDGE_STATE_DIR"]
        self.assertEqual(self.read("x"), "precious\n")


class R3ProjectConfigCannotWeaken(GuardCase):
    def project_cfg(self, data):
        os.makedirs(os.path.join(self.proj, ".claude"), exist_ok=True)
        with open(os.path.join(self.proj, ".claude", "bash-bridge.json"), "w") as fh:
            json.dump(data, fh)

    def load(self, env_extra=None):
        import subprocess
        code = ("import sys, json; sys.path.insert(0, %r); from bashbridge import config; "
                "print(json.dumps(config.load(%r)))" % (os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "lib"), self.proj))
        env = dict(self.env, **(env_extra or {}))
        p = subprocess.run(["python3", "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return json.loads(p.stdout)

    def test_switches_are_user_only(self):
        self.project_cfg({"guard": "off", "hooks": False, "rules": False, "undo": False})
        cfg = self.load()
        self.assertEqual((cfg["guard"], cfg["hooks"], cfg["rules"], cfg["undo"]), ("revert", True, True, True))
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])

    def test_limits_cannot_go_to_zero_or_negative(self):
        self.project_cfg({"max_untracked": 0, "max_file_bytes": -1, "hook_workers": 1000, "keep_entries": 0,
                          "hook_timeout_s": 0, "max_files": 1})
        cfg = self.load()
        self.assertEqual(cfg["max_untracked"], 5000)
        self.assertEqual(cfg["max_file_bytes"], 20 * 1024 * 1024)
        self.assertEqual(cfg["max_files"], 100000)
        self.assertLessEqual(cfg["hook_workers"], 4)
        self.assertGreaterEqual(cfg["keep_entries"], 10)
        self.assertGreaterEqual(cfg["hook_timeout_s"], 10)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])

    def test_deadlines_and_retention_are_user_only_with_a_note(self):
        """0.3.1 (B1): a project can't shorten hook deadlines or undo retention; it is told so, once."""
        self.project_cfg({"hook_timeout_s": 10, "hook_budget_s": 30, "keep_entries": 10, "undo_days": 1,
                          "undo_max_bytes": 10485760, "store_max_bytes": 104857600, "hook_workers": 2})
        cfg = self.load()
        from bashbridge.config import DEFAULTS
        for k in ("hook_timeout_s", "hook_budget_s", "keep_entries", "undo_days", "undo_max_bytes", "store_max_bytes"):
            self.assertEqual(cfg[k], DEFAULTS[k], k)
        self.assertEqual(cfg["hook_workers"], 2, "concurrency may be lowered")
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertIn("ignored", out["systemMessage"])
        self.assertIn("hook_timeout_s", self.context(out))
        self.assertNotIn("hook_workers", out["systemMessage"].split("only")[0])
        out = self.bash("echo again >> protected.txt")
        self.assertNotIn("ignored", out.get("systemMessage", ""), "once per session")

    def test_project_may_disable_but_it_is_visible(self):
        self.project_cfg({"enabled": False})
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("disabled by", out["systemMessage"])
        self.assertIn("disabled by", self.context(out))
        self.assertIsNone(self.bash("echo again >> protected.txt"), "the note comes once per session")


class R4OneDeadline(BridgeCase):
    def test_budget_is_total_and_later_hooks_are_skipped(self):
        for i in range(6):
            self.write("f%d.txt" % i, "x\n")
        self.commit()
        script = self.hook_script("slow.sh", "cat >/dev/null; sleep 2\n")
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        with open(os.path.join(self.home, "bash-bridge.json"), "w") as fh:
            json.dump({"hook_budget_s": 1, "hook_workers": 2}, fh)
        start = time.time()
        out = self.bash("for f in f*.txt; do echo y >> $f; done")
        elapsed = time.time() - start
        # old behaviour: every wave of 2 files got the full budget again (3 waves x 1 s + overhead)
        self.assertLess(elapsed, 2.9, "one deadline for all files")
        self.assertIn("budget used up", out["systemMessage"])

    def test_pending_kept_while_post_runs_then_removed(self):
        self.write("a.txt", "a\n")
        self.commit()
        pend = os.path.join(self.tmp, "pending-seen")
        script = self.hook_script("look.sh", 'cat >/dev/null; ls "$BB_PENDING" > "%s"\n' % pend)
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        self.bash("echo warm >> a.txt")
        env = dict(self.env, BB_PENDING=os.path.join(state_store(self), "pending"))
        self.bash("echo b >> a.txt", env=env)
        with open(pend) as fh:
            self.assertIn(".json", fh.read(), "the call is still registered while its hooks run")
        self.assertEqual(os.listdir(os.path.join(state_store(self), "pending")), [])
        self.assertTrue(self.entries()[-1]["final"])


class R5R8PendingLifetime(GuardCase):
    def fake_pending(self, tuid, **kw):
        d = os.path.join(state_store(self), "pending")
        data = dict({"ts": time.time(), "session": "other", "agent": "", "tuid": tuid, "tree": "x"}, **kw)
        with open(os.path.join(d, tuid + ".json"), "w") as fh:
            json.dump(data, fh)
        os.chmod(os.path.join(d, tuid + ".json"), 0o600)
        return os.path.join(d, tuid + ".json")

    def test_pending_of_exited_process_does_not_count(self):
        self.bash("true; echo warm > w.txt")
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        path = self.fake_pending("toolu_dead", pid=p.pid, pid_start="Mon Jan  1 00:00:00 2024")
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertFalse(os.path.exists(path))

    def test_live_other_session_counts(self):
        self.bash("echo warm > w.txt")
        from bashbridge.bridge import proc_identity
        self.fake_pending("toolu_live", pid=os.getpid(), pid_start=proc_identity(os.getpid())[0])
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("at the same time", out["reason"])

    def test_future_and_old_entries_are_stale_and_pruned(self):
        self.bash("echo warm > w.txt")
        fut = self.fake_pending("toolu_future", ts=time.time() + 86400)
        old = self.fake_pending("toolu_old", ts=time.time() - 7200)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertFalse(os.path.exists(fut))
        self.assertFalse(os.path.exists(old))

    def test_overlap_marks_do_not_extend_the_window(self):
        self.bash("echo warm > w.txt")
        p = self.fake_pending("toolu_aging", ts=time.time() - 300)
        os.utime(p, None)  # rewritten just now (mark_overlap does that)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])

    def test_reused_pid_does_not_count(self):
        """0.3.1: the start time must match too, or the pid belongs to another process now."""
        self.bash("echo warm > w.txt")
        path = self.fake_pending("toolu_reused", pid=os.getpid(), pid_start="Mon Jan  1 00:00:00 2024")
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertFalse(os.path.exists(path))

    def test_unknown_identity_counts_for_the_window(self):
        self.bash("echo warm > w.txt")
        self.fake_pending("toolu_unknown", pid=None, pid_start=None)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("at the same time", out["reason"])

    def test_pending_entries_are_checked_before_reading(self):
        """0.3.1 (R6): files listed from pending/ go through check_private_file (no symlinks)."""
        self.bash("echo warm > w.txt")
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as fh:
            json.dump({"ts": time.time(), "session": "x", "tuid": "toolu_link", "tree": "x"}, fh)
        link = os.path.join(state_store(self), "pending", "toolu_link.json")
        os.symlink(target, link)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"], "a symlinked entry is not trusted as a running call")
        self.assertTrue(os.path.exists(target))

    def test_count_is_capped(self):
        self.bash("echo warm > w.txt")
        for i in range(210):
            self.fake_pending("toolu_x%03d" % i, ts=time.time() - 200 + i * 0.01)
        self.bash("echo y > w.txt")
        self.assertLessEqual(len(os.listdir(os.path.join(state_store(self), "pending"))), 200)


class R6BatchOnlyItsIds(GuardCase):
    def batch(self, calls):
        ev = {"session_id": self.session, "hook_event_name": "PostToolBatch", "cwd": self.proj}
        if calls is not None:
            ev["tool_calls"] = calls
        self.assertEqual(self.run_hook("batch", ev)[:2], (0, ""))

    def test_other_pending_call_of_the_session_is_kept(self):
        self.run_hook("pre", self.event("pre", "touch made.txt", "toolu_inflight"))
        self.batch([{"tool_name": "Read", "tool_use_id": "toolu_unrelated"}])
        self.assertEqual(len(os.listdir(os.path.join(state_store(self), "pending"))), 1)

    def test_malformed_batch_changes_nothing(self):
        self.run_hook("pre", self.event("pre", "touch made.txt", "toolu_inflight"))
        for calls in (None, "x", [1, None, {"tool_use_id": 5}], {}):
            self.batch(calls)
        self.assertEqual(len(os.listdir(os.path.join(state_store(self), "pending"))), 1)


class R7TranscriptParsing(BridgeCase):
    def results(self, **kw):
        from bashbridge.bridge import transcript_results, _TRANSCRIPT_CACHE
        _TRANSCRIPT_CACHE.clear()
        return transcript_results(self.transcript, **kw)

    def add(self, rec, raw=None):
        with open(self.transcript, "a") as fh:
            fh.write((raw if raw is not None else json.dumps(rec)) + "\n")

    def test_whitespace_and_nested_shapes(self):
        self.add(None, raw=json.dumps(denied_record("toolu_a"), indent=None, separators=(", ", " : ")))
        self.add(ran_record("toolu_b", "ok"))
        self.assertEqual(self.results(), {"toolu_a": "denied", "toolu_b": "done"})

    def test_partial_first_record_is_dropped(self):
        self.add(denied_record("toolu_cut"))
        self.add(ran_record("toolu_ok", "x" * 100))
        size = os.path.getsize(self.transcript)
        res = self.results(tail_bytes=size - 20)
        self.assertNotIn("toolu_cut", res)
        self.assertEqual(res.get("toolu_ok"), "done")

    def test_record_limit(self):
        for i in range(30):
            self.add(ran_record("toolu_%02d" % i, "o"))
        res = self.results(max_records=10)
        self.assertEqual(len(res), 10)
        self.assertIn("toolu_29", res)

    def test_duplicate_ids_are_unknown(self):
        """0.3.1 (B2): an id with two result records is ambiguous, in either order."""
        self.add(denied_record("toolu_a"))
        self.add(ran_record("toolu_a", "x"))
        self.add(ran_record("toolu_b", "x"))
        self.add(denied_record("toolu_b"))
        self.assertEqual(self.results(), {"toolu_a": "unknown", "toolu_b": "unknown"})

    def test_multi_result_record_is_never_a_denial(self):
        rec = denied_record("toolu_a")
        rec["message"]["content"].append(dict(rec["message"]["content"][0], tool_use_id="toolu_b"))
        self.add(rec)
        self.assertEqual(self.results(), {"toolu_a": "done", "toolu_b": "done"})


class R9R10Classifier(BridgeCase):
    def ro(self, cmd):
        from bashbridge.classify import is_read_only, _HELPER_CACHE
        _HELPER_CACHE.clear()
        return is_read_only(cmd, self.proj)

    def test_git_readers_are_writers_when_config_runs_programs(self):
        self.write("a.txt", "a\n")
        self.commit()
        self.assertTrue(self.ro("git diff"))
        self.assertTrue(self.ro("git log -p | head"))
        sh("git config diff.external ./ext.sh", self.proj)
        self.assertFalse(self.ro("git diff"))
        self.assertFalse(self.ro("cd . && git log -p"))
        sh("git config --unset diff.external && git config diff.x.textconv ./conv.sh", self.proj)
        self.assertFalse(self.ro("git show HEAD"))
        sh("git config --unset diff.x.textconv && git config core.fsmonitor ./hook.sh", self.proj)
        self.assertFalse(self.ro("git status"))
        sh("git config core.fsmonitor true", self.proj)
        self.assertTrue(self.ro("git status"))

    def test_git_flags_and_env_that_run_programs(self):
        for cmd in ("git diff --ext-diff", "git log --show-signature", "git -c diff.external=x diff",
                    "GIT_EXTERNAL_DIFF=./x git diff", "git grep -O foo", "git cat-file --textconv HEAD:a",
                    "PAGER=./x git log", "git --git-dir=/tmp/x log", "LESSOPEN='|./x %s' cat a",
                    "git help log", "git log --format=%G?", "git log --pretty='format:%h %GS'",
                    "git -P log", "git --paginate log"):
            self.assertFalse(self.ro(cmd), cmd)
        self.assertTrue(self.ro("LC_ALL=C grep x a.txt"))

    def test_less_and_more_are_not_read_only(self):
        self.assertFalse(self.ro("less a.txt"))
        self.assertFalse(self.ro("more a.txt"))


class R12InvalidConfig(GuardCase):
    def test_bad_values_fall_back_to_defaults(self):
        with open(os.path.join(self.home, "bash-bridge.json"), "w") as fh:
            json.dump({"hook_workers": "x", "hook_timeout_s": [1], "guard": 5, "keep_entries": "many",
                       "hooks": "yes"}, fh)
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        env = dict(self.env, BASH_BRIDGE_MAX_FILES="lots", BASH_BRIDGE_GUARD="maybe")
        out = self.bash("echo hacked >> protected.txt", env=env)
        self.assertIn("restored", out["reason"])
        with open(os.path.join(self.tmp, "debug.log")) as fh:
            self.assertIn("invalid value for hook_workers", fh.read())


class R13ExactModes(BridgeCase):
    def test_private_file_stays_private_after_undo_and_rollback(self):
        old = os.umask(0o022)
        try:
            self.write("secret.txt", "s\n")
            os.chmod(os.path.join(self.proj, "secret.txt"), 0o600)
            self.commit()
            self.bash("sed -i.bak 's/s/t/' secret.txt && rm secret.txt.bak")
            code, out, err = self.cli("undo", "--project", self.proj)
            self.assertEqual(code, 0, out + err)
        finally:
            os.umask(old)
        self.assertEqual(self.read("secret.txt"), "s\n")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.proj, "secret.txt")).st_mode), 0o600)
        files = self.entries()[-1]["files"]
        self.assertEqual([f.get("perm") for f in files if f["rel"] == "secret.txt"], [0o600])

    def test_guard_rollback_keeps_mode(self):
        self.write("protected.txt", "keep\n")
        os.chmod(os.path.join(self.proj, "protected.txt"), 0o640)
        self.commit()
        script = self.hook_script("guard.sh", GUARD)
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        out = self.bash("echo hacked >> protected.txt")
        self.assertIn("restored", out["reason"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.proj, "protected.txt")).st_mode), 0o640)


class R15CommandTruncation(BridgeCase):
    def test_truncation_is_flagged(self):
        from bashbridge.bridge import command_env
        self.assertEqual(command_env("echo hi"), {"BASH_BRIDGE_COMMAND": "echo hi"})
        env = command_env("echo " + "\u00e4" * 40000)
        self.assertEqual(env["BASH_BRIDGE_COMMAND_TRUNCATED"], "1")
        self.assertLessEqual(len(env["BASH_BRIDGE_COMMAND"].encode("utf-8")), 64 * 1024)

    def test_hook_sees_the_flag(self):
        self.write("a.txt", "a\n")
        self.commit()
        seen = os.path.join(self.tmp, "seen")
        script = self.hook_script("env.sh", 'cat >/dev/null; echo "${BASH_BRIDGE_COMMAND_TRUNCATED:-0} ${#BASH_BRIDGE_COMMAND}" > "%s"\n' % seen)
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        self.bash("echo b >> a.txt # " + "x" * 70000)
        with open(seen) as fh:
            flag, n = fh.read().split()
        self.assertEqual(flag, "1")
        self.assertEqual(int(n), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
