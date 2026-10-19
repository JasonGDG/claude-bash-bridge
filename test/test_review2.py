"""Regression tests for the second Codex review (findings 13-16 and the partial fixes 1, 6, 7)."""
import hashlib
import json
import os
import stat

from helpers import BridgeCase, sh, unittest

from test_review import meta, state_store


def in_env(pairs):
    """Context manager: temporarily set os.environ entries (None = unset) and clear caches."""
    import contextlib
    import bashbridge.util as util

    @contextlib.contextmanager
    def cm():
        old = {k: os.environ.get(k) for k in pairs}
        for k, v in pairs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        util._PROJECT_ENV.clear()
        try:
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            util._PROJECT_ENV.clear()
    return cm()


class F13SpoofedEnvironment(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.commit()
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"projects": {}}, fh)  # the project is NOT trusted

    def project_env(self, env, hooks=True):
        data = {"env": env}
        if hooks:
            script = self.hook_script("log.sh", 'cat >> "$HOOK_LOG"; echo >> "$HOOK_LOG"\n')
            data["hooks"] = {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}
        self.settings(data)

    def test_project_settings_env_cannot_opt_in(self):
        self.project_env({"BASH_BRIDGE_PROJECT_HOOKS": "1"})
        # Claude Code exports the settings env, so the hook process sees it too
        self.bash("echo x >> a.txt", env=dict(self.env, BASH_BRIDGE_PROJECT_HOOKS="1"))
        self.assertEqual(self.hook_log(), [])

    def test_project_settings_cannot_redirect_config_dir(self):
        fake = os.path.join(self.tmp, "fake-config")
        os.makedirs(fake)
        with open(os.path.join(fake, ".claude.json"), "w") as fh:
            json.dump({"projects": {self.proj: {"hasTrustDialogAccepted": True}}}, fh)
        with open(os.path.join(fake, "bash-bridge.json"), "w") as fh:
            json.dump({"project_hooks": True}, fh)
        self.project_env({"CLAUDE_CONFIG_DIR": fake})
        self.bash("echo x >> a.txt", env=dict(self.env, CLAUDE_CONFIG_DIR=fake))
        self.assertEqual(self.hook_log(), [])

    def test_state_and_config_dirs_inside_or_set_by_project_ignored(self):
        import bashbridge.util as util
        self.project_env({"BASH_BRIDGE_STATE_DIR": "x"}, hooks=False)
        home = util.real_home()
        with in_env({"CLAUDE_PROJECT_DIR": self.proj, "BASH_BRIDGE_STATE_DIR": os.path.join(self.tmp, "s"),
                     "CLAUDE_CONFIG_DIR": os.path.join(self.proj, ".cfg")}):
            self.assertEqual(util.state_dir(), os.path.join(home, ".claude-bash-bridge"))
            self.assertEqual(util.claude_config_dir(), os.path.join(home, ".claude"))
        self.settings({"env": {}})
        with in_env({"CLAUDE_PROJECT_DIR": self.proj, "BASH_BRIDGE_STATE_DIR": os.path.join(self.tmp, "s")}):
            self.assertEqual(util.state_dir(), os.path.join(self.tmp, "s"))

    def test_python_override_ignored_in_hook_mode(self):
        marker = os.path.join(self.tmp, "EVIL-RAN")
        evil = self.hook_script("evil.sh", 'touch "%s"\n' % marker)
        env = dict(self.env, BASH_BRIDGE_PYTHON=evil)
        self.bash("echo x >> a.txt", env=env)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(len(self.entries()), 1, "bash-bridge still ran with the normal python3")

    def test_process_env_opt_in_still_works(self):
        self.project_env({})
        self.bash("echo x >> a.txt", env=dict(self.env, BASH_BRIDGE_PROJECT_HOOKS="1"))
        self.assertEqual(len(self.hook_log()), 1)


class F14F15StoreBounds(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("small.txt", "s\n")
        self.write("big.txt", "B" * 300000)
        self.commit()

    def user_cfg(self, data):
        with open(os.path.join(self.home, "bash-bridge.json"), "w") as fh:
            json.dump(data, fh)

    def test_per_call_cap_checked_before_reading(self):
        self.user_cfg({"undo_max_bytes": 100000})
        self.bash("echo x >> small.txt && echo y > big.txt")
        files = {f["rel"]: f for f in self.entries()[-1]["files"]}
        self.assertTrue(files["small.txt"]["kept"])
        self.assertFalse(files["big.txt"]["kept"])
        code, out, err = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 2)
        self.assertIn("EXPIRED", out)
        self.assertEqual(self.read("small.txt"), "s\n")
        self.assertIn("expired", self.cli("list", "--project", self.proj)[1])

    def test_project_store_cap_evicts_oldest(self):
        self.user_cfg({"store_max_bytes": 3000})
        for i in range(4):
            self.write("r%d.bin" % i, os.urandom(1500).hex())  # incompressible-ish, ~3 KB raw
            self.commit("c%d" % i)
            self.bash("echo z > r%d.bin" % i)
        ids = [e["id"] for e in self.entries()]
        self.assertLess(len(ids), 4)
        self.assertEqual(ids[-1], 4, "the newest entry is always kept")
        from bashbridge.snapshot import Store
        with __import__("test_review2").in_env({"BASH_BRIDGE_STATE_DIR": self.state, "CLAUDE_PROJECT_DIR": self.proj,
                                                 "CLAUDE_CONFIG_DIR": self.home}):
            self.assertLessEqual(Store(self.proj).store_bytes(), 3000 + 4000)

    def test_project_config_may_only_lower_limits(self):
        from bashbridge import config
        with open(os.path.join(self.proj, ".claude", "bash-bridge.json") if os.path.isdir(
                os.path.join(self.proj, ".claude")) else self.write(".claude/bash-bridge.json", "{}"), "w") as fh:
            json.dump({"keep_entries": 100000, "undo_max_bytes": 10 ** 12, "undo_days": 9999,
                       "store_max_bytes": 10 ** 13, "hook_budget_s": 10 ** 6, "undo_days_unknown": 1,
                       "max_file_bytes": 1000}, fh)
        with in_env({"CLAUDE_PROJECT_DIR": self.proj, "CLAUDE_CONFIG_DIR": self.home}):
            cfg = config.load(self.proj)
        d = config.DEFAULTS
        for k in ("keep_entries", "undo_max_bytes", "undo_days", "store_max_bytes", "hook_budget_s"):
            self.assertEqual(cfg[k], d[k], k)
        self.assertEqual(cfg["max_file_bytes"], 1000, "lowering is allowed")


class F16FdRelativeRestore(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("d/f.txt", "f\n")
        self.commit()

    def store(self):
        from bashbridge.snapshot import Store
        return Store(self.proj)

    def test_symlinked_parent_refused_by_write_and_remove(self):
        from bashbridge.snapshot import remove_path, write_file
        outside = os.path.join(self.tmp, "out")
        os.makedirs(outside)
        with open(os.path.join(outside, "f.txt"), "w") as fh:
            fh.write("outside\n")
        sh("rm -r d && ln -s '%s' d" % outside, self.proj)
        with in_env({"BASH_BRIDGE_STATE_DIR": self.state, "CLAUDE_PROJECT_DIR": self.proj}):
            st = self.store()
            with self.assertRaises(OSError):
                write_file(st, "d/f.txt", "100644", b"evil\n")
            with self.assertRaises(OSError):
                remove_path(st, "d/f.txt")
        with open(os.path.join(outside, "f.txt")) as fh:
            self.assertEqual(fh.read(), "outside\n")

    def test_file_in_place_of_directory_is_replaced(self):
        from bashbridge.snapshot import write_file
        sh("rm -r d && echo notadir > d", self.proj)
        with in_env({"BASH_BRIDGE_STATE_DIR": self.state, "CLAUDE_PROJECT_DIR": self.proj}):
            write_file(self.store(), "d/f.txt", "100755", b"back\n")
        self.assertEqual(self.read("d/f.txt"), "back\n")
        self.assertTrue(os.access(os.path.join(self.proj, "d/f.txt"), os.X_OK))

    def test_dotdot_rejected(self):
        from bashbridge.snapshot import write_file
        with in_env({"BASH_BRIDGE_STATE_DIR": self.state, "CLAUDE_PROJECT_DIR": self.proj}):
            with self.assertRaises(OSError):
                write_file(self.store(), "../escape.txt", "100644", b"x")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))


class F6ExistingStateFiles(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.commit()

    def test_loose_modes_are_fixed(self):
        self.bash("echo x >> a.txt")
        store = state_store(self)
        for name in ("entries.jsonl", "meta.json"):
            os.chmod(os.path.join(store, name), 0o644)
        self.bash("echo y >> a.txt")
        for name in ("entries.jsonl", "meta.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, name)).st_mode), 0o600, name)

    def test_symlinked_state_file_refused(self):
        self.bash("echo x >> a.txt")
        store = state_store(self)
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("untouched\n")
        os.remove(os.path.join(store, "entries.jsonl"))
        os.symlink(victim, os.path.join(store, "entries.jsonl"))
        self.assertIsNone(self.bash("echo y >> a.txt"))
        with open(victim) as fh:
            self.assertEqual(fh.read(), "untouched\n")
        code, out, _ = self.cli("list", "--project", self.proj)
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)


class F7IndexParsing(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.write("dir/b.txt", "b\n")
        self.commit()

    def index(self):
        with open(os.path.join(self.proj, ".git", "index"), "rb") as fh:
            return fh.read()

    def test_plain_index_accepted(self):
        from bashbridge.snapshot import index_is_plain
        self.assertTrue(index_is_plain(self.index()))

    def test_unknown_extension_or_bad_checksum_rejected(self):
        from bashbridge.snapshot import index_is_plain
        data = self.index()
        body = data[:-20] + b"ABCD" + (4).to_bytes(4, "big") + b"xxxx"
        self.assertFalse(index_is_plain(body + hashlib.sha1(body).digest()))
        self.assertFalse(index_is_plain(data[:-1] + bytes([data[-1] ^ 1])))
        self.assertFalse(index_is_plain(data[:40]))

    def test_index_v4_not_seeded(self):
        sh("git update-index --index-version 4", self.proj)
        self.bash("echo x >> a.txt")
        self.assertFalse(meta(self)["seeded"])
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_intent_to_add_not_seeded(self):
        self.write("ita.txt", "i\n")
        sh("git add -N ita.txt", self.proj)
        self.bash("echo x >> a.txt")
        self.assertFalse(meta(self)["seeded"])


class F1Overlap(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("target.txt", "keep\n")
        self.commit()
        script = self.hook_script("guard.sh", 'cat >/dev/null; echo "protected" >&2; exit 2\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": script}]}]}})

    def test_overlapping_calls_are_not_rolled_back(self):
        c1, c2 = "echo one >> target.txt", "echo two > other.txt"
        self.run_hook("pre", self.event("pre", c1, "toolu_o1"))
        self.run_hook("pre", self.event("pre", c2, "toolu_o2"))
        sh(c1, self.proj)
        code, out, _e, _t = self.run_hook("post", self.event("post", c1, "toolu_o1"))
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertIn("at the same time", res["reason"])
        self.assertEqual(self.read("target.txt"), "keep\none\n")
        self.assertIn("overlapped", self.cli("list", "--project", self.proj)[1])

    def test_sequential_calls_still_roll_back(self):
        out = self.bash("echo one >> target.txt")
        self.assertIn("restored", out["reason"])
        self.assertEqual(self.read("target.txt"), "keep\n")


if __name__ == "__main__":
    unittest.main()
