"""Regression tests for the findings of the Codex review (one class per finding)."""
import contextlib
import io
import json
import os
import stat

from helpers import ROOT, BridgeCase, sh, unittest

import bashbridge.cli as cli
from bashbridge.hooks import if_matches


def state_store(case):
    projects = os.path.join(case.state, "projects")
    names = [n for n in os.listdir(projects) if os.path.isdir(os.path.join(projects, n))]
    return os.path.join(projects, names[0])


def meta(case):
    with open(os.path.join(state_store(case), "meta.json")) as fh:
        return json.load(fh)


class F1GuardRollbackConflict(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("target.txt", "keep me\n")
        self.write("other.txt", "o\n")
        self.commit()

    def test_change_after_the_command_is_not_rolled_back(self):
        # the guard denies, but something (here: the guard itself, standing in for a formatter,
        # an editor or a parallel Bash call) writes the file again before the rollback
        script = self.hook_script("guard.sh", 'cat >/dev/null; echo "late write" >> "$CLAUDE_PROJECT_DIR/target.txt";'
                                              ' echo "target.txt is protected" >&2; exit 2\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        out = self.bash("echo bash >> target.txt")
        self.assertEqual(self.read("target.txt"), "keep me\nbash\nlate write\n")
        self.assertIn("did NOT roll it back", out["reason"])

    def test_interleaved_parallel_calls(self):
        t1, t2 = "toolu_p1", "toolu_p2"
        c1, c2 = "echo one > target.txt", "echo two > other.txt"
        for t, c in ((t1, c1), (t2, c2)):
            self.assertEqual(self.run_hook("pre", self.event("pre", c, t))[:2], (0, ""))
        sh(c1, self.proj)
        sh(c2, self.proj)
        for t, c in ((t1, c1), (t2, c2)):
            self.assertEqual(self.run_hook("post", self.event("post", c, t))[0], 0)
        code, out, err = self.cli("undo", "2", "--project", self.proj)
        self.assertEqual(code, 2, out + err)
        self.assertIn("overlapped another Bash call", out)
        code, out, err = self.cli("undo", "2", "--force", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read("target.txt"), "keep me\n")
        self.assertEqual(self.read("other.txt"), "o\n")


class F2UndoRecheck(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "orig\n")
        self.commit()

    def _undo_in_process(self, force=False):
        old_env = dict(os.environ)
        os.environ.update({"BASH_BRIDGE_STATE_DIR": self.state, "CLAUDE_CONFIG_DIR": self.home})
        real = cli.plan_undo

        def racing_plan(store, entries, f=False):
            steps = real(store, entries, f)
            with open(os.path.join(self.proj, "a.txt"), "w") as fh:
                fh.write("written between planning and restore\n")
            return steps

        cli.plan_undo = racing_plan
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                import argparse
                code = cli.cmd_undo(argparse.Namespace(project=self.proj, n=1, dry_run=False, force=force,
                                                       id=None, session=None))
        finally:
            cli.plan_undo = real
            os.environ.clear()
            os.environ.update(old_env)
        return code, buf.getvalue()

    def test_change_between_planning_and_write_is_kept(self):
        self.bash("echo bash > a.txt")
        code, out = self._undo_in_process()
        self.assertEqual(code, 2, out)
        self.assertIn("changed while undoing", out)
        self.assertEqual(self.read("a.txt"), "written between planning and restore\n")

    def test_restore_replaces_symlink_instead_of_following_it(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as fh:
            fh.write("outside\n")
        self.bash("rm a.txt")
        os.symlink(outside, os.path.join(self.proj, "a.txt"))
        code, out, err = self.cli("undo", "--force", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.islink(os.path.join(self.proj, "a.txt")))
        self.assertEqual(self.read("a.txt"), "orig\n")
        with open(outside) as fh:
            self.assertEqual(fh.read(), "outside\n")


class F3HookSources(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.commit()
        self.logging_hook()  # project hook

    def untrust(self, parent=None):
        projects = {parent: {"hasTrustDialogAccepted": True}} if parent else {}
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"projects": projects}, fh)

    def test_untrusted_project_hooks_not_bridged(self):
        self.untrust()
        self.bash("echo x >> a.txt")
        self.assertEqual(self.hook_log(), [])

    def test_trusted_parent_counts(self):
        self.untrust(parent=self.tmp)
        self.bash("echo x >> a.txt")
        self.assertEqual(len(self.hook_log()), 1)

    def test_explicit_opt_in_env(self):
        self.untrust()
        self.bash("echo x >> a.txt", env=dict(self.env, BASH_BRIDGE_PROJECT_HOOKS="1"))
        self.assertEqual(len(self.hook_log()), 1)

    def test_repo_cannot_opt_itself_in(self):
        self.untrust()
        with open(os.path.join(self.proj, ".claude", "bash-bridge.json"), "w") as fh:
            json.dump({"project_hooks": True, "sources": ["project"]}, fh)
        self.bash("echo x >> a.txt")
        self.assertEqual(self.hook_log(), [])

    def test_sources_env_excludes_project(self):
        self.bash("echo x >> a.txt", env=dict(self.env, BASH_BRIDGE_SOURCES="user,plugins"))
        self.assertEqual(self.hook_log(), [])

    def test_disable_all_hooks_in_user_settings(self):
        self.settings({"disableAllHooks": True}, "user")
        self.bash("echo x >> a.txt")
        self.assertEqual(self.hook_log(), [])

    def test_managed_disable_and_managed_only(self):
        managed = os.path.join(self.tmp, "managed.json")
        mscript = self.hook_script("managed.sh", 'cat >/dev/null; echo managed >> "$HOOK_LOG.managed"\n')
        env = dict(self.env, BASH_BRIDGE_MANAGED_SETTINGS=managed)
        with open(managed, "w") as fh:
            json.dump({"disableAllHooks": True}, fh)
        self.bash("echo x >> a.txt", env=env)
        self.assertEqual(self.hook_log(), [])
        with open(managed, "w") as fh:
            json.dump({"allowManagedHooksOnly": True, "hooks": {"PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": mscript}]}]}}, fh)
        self.bash("echo y >> a.txt", env=env)
        self.assertEqual(self.hook_log(), [])
        self.assertTrue(os.path.exists(self.log + ".managed"))

    def test_non_command_hooks_coexist(self):
        script = self.hook_script("c.sh", 'd=$(cat); printf "%s\\n" "$d" >> "$HOOK_LOG"\n')
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "http", "url": "http://127.0.0.1:9/never"},
            {"type": "prompt", "prompt": "x"},
            {"type": "command", "command": script}]}]}})
        self.bash("echo x >> a.txt")
        self.assertEqual(len(self.hook_log()), 1)


class F4F5PreImages(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "v1\n")
        self.commit()

    def undo(self, *a):
        code, out, err = self.cli("undo", *a, "--project", self.proj)
        return code, out + err

    def test_project_gc_prune_does_not_break_undo(self):
        self.write("a.txt", "v2-staged\n")
        sh("git add a.txt", self.proj)
        sha = sh("git rev-parse :a.txt", self.proj).stdout.decode().strip()
        self.bash("echo v3 > a.txt")
        self.assertTrue(meta(self)["seeded"], "scenario needs a seeded shadow index")
        sh("git reset -q && git reflog expire --all --expire=now && git gc -q --prune=now", self.proj)
        self.assertNotEqual(sh("git cat-file -e %s" % sha, self.proj, check=False).returncode, 0,
                            "the staged blob is gone from the project store")
        code, out = self.undo()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("a.txt"), "v2-staged\n")

    def test_shadow_gc_prune_now_keeps_undo(self):
        self.bash("echo v2 > a.txt")
        env = dict(os.environ, GIT_DIR=os.path.join(state_store(self), "shadow.git"))
        sh("git gc -q --prune=now", self.proj, env=env)
        code, out = self.undo()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("a.txt"), "v1\n")

    def test_expired_pre_image_reported(self):
        self.bash("echo v2 > a.txt")
        sh("rm -r '%s'" % os.path.join(state_store(self), "blobs"), self.proj)
        code, out = self.undo()
        self.assertEqual(code, 2)
        self.assertIn("EXPIRED", out)
        self.assertEqual(self.read("a.txt"), "v2\n")

    def test_old_format_entry_is_expired(self):
        self.bash("echo v2 > a.txt")
        path = os.path.join(state_store(self), "entries.jsonl")
        with open(path) as fh:
            e = json.loads(fh.readline())
        del e["files"]
        with open(path, "w") as fh:
            fh.write(json.dumps(e) + "\n")
        code, out = self.undo()
        self.assertEqual(code, 2)
        self.assertIn("EXPIRED", out)
        self.assertIn("expired", self.cli("list", "--project", self.proj)[1])

    def test_entries_expire_with_their_pre_images(self):
        self.bash("echo v2 > a.txt")
        store = state_store(self)
        path = os.path.join(store, "entries.jsonl")
        with open(path) as fh:
            e = json.loads(fh.readline())
        old_sha = e["files"][0]["pre"][1]
        e["ts"] -= 40 * 86400
        with open(path, "w") as fh:
            fh.write(json.dumps(e) + "\n")
        self.bash("echo v3 > b.txt")
        ids = [x["id"] for x in self.entries()]
        self.assertEqual(ids, [2])
        self.assertFalse(os.path.exists(os.path.join(store, "blobs", old_sha[:2], old_sha[2:])))


class F6StatePermissions(BridgeCase):
    def setUp(self):
        super().setUp()
        self.old_umask = os.umask(0o022)
        self.write("a.txt", "a\n")
        self.commit()

    def tearDown(self):
        os.umask(self.old_umask)
        super().tearDown()

    def test_private_modes(self):
        script = self.hook_script("mk.sh", 'cat >/dev/null; touch "$HOOK_LOG.created"\n')
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})
        self.bash("echo x >> a.txt")
        store = state_store(self)
        for d in (self.state, os.path.join(self.state, "projects"), store, os.path.join(store, "shadow.git")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, "entries.jsonl")).st_mode), 0o600)
        blobs = os.path.join(store, "blobs")
        for d, _s, files in os.walk(blobs):
            for f in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600)
        # the user's own hooks keep the normal umask
        self.assertEqual(stat.S_IMODE(os.stat(self.log + ".created").st_mode), 0o644)

    def test_symlinked_state_dir_refused(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        link = os.path.join(self.tmp, "state-link")
        os.symlink(target, link)
        env = dict(self.env, BASH_BRIDGE_STATE_DIR=link)
        self.assertIsNone(self.bash("echo x >> a.txt", env=env))
        self.assertEqual(os.listdir(target), [])
        code, out, _ = self.cli("list", "--project", self.proj, env=env)
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)

    def test_symlinked_project_store_refused(self):
        self.bash("echo x >> a.txt")
        store = state_store(self)
        sh("mv '%s' '%s.real' && ln -s '%s.real' '%s'" % (store, store, store, store), self.proj)
        code, out, err, _ = self.run_hook("pre", self.event("pre", "echo y >> a.txt", "t-x"))
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(os.listdir(os.path.join(store + ".real", "pending"))), 0)


class F7IndexSeeding(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.commit()

    def test_plain_repo_is_seeded(self):
        self.bash("echo x >> a.txt")
        self.assertTrue(meta(self)["seeded"])

    def test_index_lock_prevents_seeding(self):
        open(os.path.join(self.proj, ".git", "index.lock"), "w").close()
        try:
            self.bash("echo x >> a.txt")
        finally:
            os.remove(os.path.join(self.proj, ".git", "index.lock"))
        self.assertFalse(meta(self)["seeded"])
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_skip_worktree_prevents_seeding(self):
        sh("git update-index --skip-worktree b.txt", self.proj)
        self.bash("echo x >> b.txt")
        self.assertFalse(meta(self)["seeded"])
        self.assertEqual(self.last_changes(), [("M", "b.txt")])

    def test_assume_unchanged_prevents_seeding(self):
        sh("git update-index --assume-unchanged a.txt", self.proj)
        self.bash("echo x >> a.txt")
        self.assertFalse(meta(self)["seeded"])
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_untracked_cache_and_sparse_prevent_seeding(self):
        for cfg in ("core.untrackedCache true", "core.sparseCheckout true", "core.fsmonitor true"):
            sh("git config %s" % cfg, self.proj)
            st = os.path.join(self.state, "projects")
            if os.path.isdir(st):
                sh("rm -r '%s'" % st, self.proj)
            self.bash("echo x >> a.txt")
            self.assertFalse(meta(self)["seeded"], cfg)
            sh("git config --unset %s" % cfg.split()[0], self.proj)

    def test_linked_worktree(self):
        wt = os.path.join(self.tmp, "wt")
        sh("git worktree add -q '%s' -b wt" % wt, self.proj)
        env = dict(self.env, CLAUDE_PROJECT_DIR=wt)
        self.bash("echo w >> a.txt && echo n > new.txt", cwd=wt, env=env)
        code, out, _ = self.cli("list", "--json", "--project", wt, env=env)
        changes = sorted(tuple(c) for c in json.loads(out)[-1]["changes"])
        self.assertEqual(changes, [("A", "new.txt"), ("M", "a.txt")])
        self.assertEqual(self.read("a.txt"), "a\n", "main checkout untouched")

    def test_nested_repository_not_reported(self):
        sh("mkdir inner && cd inner && git init -q && echo i > i.txt", self.proj)
        self.bash("echo z >> inner/i.txt && echo x >> a.txt")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])


class F8PlaceholderQuoting(BridgeCase):
    def test_hostile_plugin_path(self):
        self.write("a.txt", "a\n")
        self.commit()
        plug = os.path.join(self.tmp, "pl $(touch PWNED) \"q\" 'x` `")
        os.makedirs(os.path.join(plug, "hooks"))
        os.makedirs(os.path.join(plug, "scripts"))
        with open(os.path.join(plug, "scripts", "p.sh"), "w") as fh:
            fh.write('#!/bin/sh\ncat >/dev/null; echo "ran:$1" >> "$HOOK_LOG.pl"\n')
        os.chmod(os.path.join(plug, "scripts", "p.sh"), 0o755)
        with open(os.path.join(plug, "hooks", "hooks.json"), "w") as fh:
            json.dump({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/p.sh\" shell"},
                {"type": "command", "command": "/bin/sh", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/p.sh", "exec"]},
            ]}]}}, fh)
        os.makedirs(os.path.join(self.home, "plugins"))
        with open(os.path.join(self.home, "plugins", "installed_plugins.json"), "w") as fh:
            json.dump({"version": 2, "plugins": {"pl@m": [{"scope": "user", "installPath": plug}]}}, fh)
        self.settings({"enabledPlugins": {"pl@m": True}}, "user")
        self.bash("echo x >> a.txt")
        with open(self.log + ".pl") as fh:
            self.assertEqual(sorted(fh.read().split()), ["ran:exec", "ran:shell"])
        for d in (self.proj, self.tmp, plug):
            self.assertFalse(os.path.exists(os.path.join(d, "PWNED")), d)


class F9IfConditions(unittest.TestCase):
    def test_conditions(self):
        f = "/p/src/pkg/mod.py"
        self.assertTrue(if_matches(None, "Edit", f, "/p"))
        self.assertTrue(if_matches("Edit", "Edit", f, "/p"))
        self.assertTrue(if_matches("Edit(*.py)", "Edit", f, "/p"))
        self.assertTrue(if_matches("Edit(src/**/*.py)", "Edit", f, "/p"))
        self.assertTrue(if_matches("Write(src/**)", "Edit", f, "/p"))
        self.assertTrue(if_matches("Edit(//p/src/**)", "Edit", f, "/p"))
        self.assertFalse(if_matches("Edit(*.ts)", "Edit", f, "/p"))
        self.assertFalse(if_matches("Bash(git *)", "Edit", f, "/p"))
        self.assertFalse(if_matches("Edit(", "Edit", f, "/p"), "unknown syntax must not run the hook")
        self.assertFalse(if_matches("Edit(*.py) && Write", "Edit", f, "/p"))


class F10NativeShape(BridgeCase):
    def test_synthesized_events_match_captured_native_events(self):
        with open(os.path.join(ROOT, "test", "fixtures", "native_posttooluse.json")) as fh:
            native = {e["tool_name"]: e for e in json.load(fh)["events"]}
        self.write("a.txt", "one\ntwo\n")
        self.commit()
        self.logging_hook()
        cmd = "echo three >> a.txt && echo n > new.txt"
        pre = self.event("pre", cmd, "toolu_n")
        post = self.event("post", cmd, "toolu_n")
        for ev in (pre, post):
            ev.update({"prompt_id": "p", "effort": {"level": "medium"}})
        post["duration_ms"] = 12
        self.run_hook("pre", pre)
        sh(cmd, self.proj)
        self.run_hook("post", post)
        synth = {e["tool_name"]: e for e in self.hook_log()}
        for tool in ("Edit", "Write"):
            n, s = native[tool], synth[tool]
            self.assertEqual(set(s) - set(n), {"bash_bridge"}, tool)
            self.assertEqual(set(n) - set(s), set(), tool)
            self.assertEqual(set(s["tool_input"]), set(n["tool_input"]), tool)
            self.assertEqual(set(s["tool_response"]), set(n["tool_response"]), tool)
            for h in s["tool_response"]["structuredPatch"]:
                self.assertEqual(set(h), set(n["tool_response"]["structuredPatch"][0]))
        self.assertEqual(synth["Edit"]["duration_ms"], 12)
        self.assertEqual(synth["Edit"]["tool_response"]["originalFile"], "one\ntwo\n")


class F11RuleSymlinks(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("src/x.ts", "x\n")
        self.write(".claude/rules/api.md", '---\npaths: ["src/**"]\n---\nINSIDE-RULE\n')
        outside = os.path.join(self.tmp, "outside-rules")
        os.makedirs(outside)
        with open(os.path.join(outside, "o.md"), "w") as fh:
            fh.write('---\npaths: ["src/**"]\n---\nOUTSIDE-RULE\n')
        os.symlink(outside, os.path.join(self.proj, ".claude", "rules", "shared"))
        os.symlink(os.path.join(self.proj, ".claude", "rules"), os.path.join(self.proj, ".claude", "rules", "loop"))
        self.commit()

    def test_outside_symlink_skipped_loop_survived(self):
        ctx = self.context(self.bash("echo y >> src/x.ts"))
        self.assertIn("INSIDE-RULE", ctx)
        self.assertNotIn("OUTSIDE-RULE", ctx)
        self.assertEqual(ctx.count("INSIDE-RULE"), 1)

    def test_user_can_allow_following(self):
        with open(os.path.join(self.home, "bash-bridge.json"), "w") as fh:
            json.dump({"follow_rule_symlinks": True}, fh)
        self.assertIn("OUTSIDE-RULE", self.context(self.bash("echo y >> src/x.ts")))


if __name__ == "__main__":
    unittest.main()
