"""A hook must never break the session: exit 0 and an empty stdout on any internal error."""
import json
import os
import stat

from helpers import BIN, BridgeCase, unittest


class Safety(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "a\n")
        self.commit()

    def assert_silent(self, mode, event=None, env=None, raw=None):
        code, out, err, _t = self.run_hook(mode, event or self.event(mode, "echo x > a.txt", "t1"), env, raw)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        return err

    def test_garbage_stdin(self):
        for raw in ("", "not json", "[1,2]", "{", '{"tool_name": 5}'):
            for mode in ("pre", "post", "post-failure", "session-start", "unknown-mode"):
                self.assert_silent(mode, raw=raw)

    def test_corrupt_settings(self):
        self.settings("{ this is not json", "project")
        self.settings('{"hooks": {"PostToolUse": "nope"}}', "user")
        self.settings('{"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [5, null, {"type": "command"}]}]}}',
                      "local")
        out = self.bash("echo x > a.txt")
        self.assertIsNone(out)
        self.assertEqual(len(self.entries()), 1)

    def test_missing_git(self):
        """No git: exit 0, and Claude gets a one-time note instead of silence."""
        env = dict(self.env, PATH="/nonexistent-dir")
        self.assert_silent("pre", env=env)
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo x > a.txt", "t1"), env)
        self.assertEqual(code, 0)
        self.assertIn("git is not installed", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assert_silent("pre", self.event("pre", "echo y > a.txt", "t2"), env=env)
        self.assert_silent("post", self.event("post", "echo y > a.txt", "t2"), env=env)

    def test_unwritable_state_dir(self):
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro)
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            env = dict(self.env, BASH_BRIDGE_STATE_DIR=os.path.join(ro, "state"))
            self.assert_silent("pre", env=env)
            self.assert_silent("post", env=env)
        finally:
            os.chmod(ro, 0o755)

    def test_corrupt_shadow_repo(self):
        self.bash("echo 1 > a.txt")
        projects = os.path.join(self.state, "projects")
        key = os.listdir(projects)[0]
        with open(os.path.join(projects, key, "shadow.git", "index"), "wb") as fh:
            fh.write(b"garbage")
        self.assert_silent("pre")
        code, out, _e, _t = self.run_hook("post", self.event("post", "echo x > a.txt", "t1"))
        self.assertEqual(code, 0)
        self.assertIn("could not take a snapshot", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        # the broken shadow repo is rebuilt on the next call
        self.bash("echo 2 > a.txt")
        self.assertEqual(len(self.entries()), 2)

    def test_disable_env(self):
        env = dict(self.env, BASH_BRIDGE_DISABLE="1")
        self.assertIsNone(self.bash("echo x > a.txt", env=env))
        self.assertFalse(os.path.exists(self.state))

    def test_non_bash_tools_ignored(self):
        ev = self.event("post", "x", "t9")
        ev["tool_name"] = "Edit"
        self.assert_silent("post", ev)

    def test_background_command_ignored(self):
        ev = self.event("pre", "sleep 1 > x", "t8")
        ev["tool_input"]["run_in_background"] = True
        self.assert_silent("pre", ev)
        self.assertFalse(os.path.exists(os.path.join(self.state, "projects")))

    def test_launcher_without_python(self):
        import subprocess
        env = dict(self.env, PATH="/nonexistent", BASH_BRIDGE_PYTHON="/nonexistent/python")
        # absolute fallbacks may exist on this machine; the launcher must exit 0 either way
        p = subprocess.run(["/bin/sh", BIN, "hook", "pre"], input=b"{}", env=env, stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")


if __name__ == "__main__":
    unittest.main()
