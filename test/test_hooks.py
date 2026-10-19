"""Hook bridging: the user's Write|Edit hooks see Bash changes (issues #87971, #89251, #87575 comment)."""
import json
import os
import time

from helpers import ROOT, BridgeCase, unittest


class HookBridge(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("src/app.py", "def f():\n    return 1\n")
        self.write("target.txt", "keep me\n")
        self.commit()

    def test_logging_hook_sees_synthesized_edit_and_write(self):
        self.logging_hook("Write|Edit")
        self.bash("sed -i '' 's/return 1/return 2/' src/app.py && echo hi > new.txt")
        log = self.hook_log()
        by_tool = {e["tool_name"]: e for e in log}
        self.assertEqual(sorted(by_tool), ["Edit", "Write"])
        edit, write = by_tool["Edit"], by_tool["Write"]
        self.assertEqual(edit["tool_input"]["file_path"], os.path.join(self.proj, "src/app.py"))
        self.assertEqual(edit["tool_input"]["old_string"], "    return 1\n")
        self.assertEqual(edit["tool_input"]["new_string"], "    return 2\n")
        self.assertEqual(write["tool_input"]["content"], "hi\n")
        self.assertEqual(write["tool_response"]["type"], "create")
        for e in log:
            self.assertEqual(e["hook_event_name"], "PostToolUse")
            self.assertEqual(e["session_id"], self.session)
            self.assertEqual(e["transcript_path"], self.transcript)
            self.assertEqual(e["cwd"], self.proj)
            self.assertTrue(e["bash_bridge"]["synthetic"])

    def test_without_bridge_hook_never_fires(self):
        """Baseline for the README: same Bash change with bash-bridge disabled."""
        self.logging_hook("Write|Edit")
        env = dict(self.env, BASH_BRIDGE_DISABLE="1")
        self.bash("sed -i '' 's/return 1/return 2/' src/app.py", env=env)
        self.assertEqual(self.hook_log(), [])

    def test_multiedit_and_regex_matchers(self):
        self.logging_hook("^(Edit|MultiEdit)$")
        self.bash("echo x >> src/app.py")
        self.assertEqual([e["tool_name"] for e in self.hook_log()], ["Edit"])

    def test_blocking_hook_exit2_reason_relayed(self):
        script = self.hook_script("lint.sh", 'cat >/dev/null; echo "lint: line 2 uses tabs" >&2; exit 2\n')
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [
            {"type": "command", "command": script}]}]}})
        out = self.bash("printf '\\tx\\n' >> src/app.py")
        self.assertEqual(out["decision"], "block")
        self.assertIn("lint: line 2 uses tabs", out["reason"])
        self.assertIn("src/app.py", out["reason"])

    def test_json_block_and_additional_context(self):
        script = self.hook_script("j.sh", 'cat >/dev/null; echo \'{"decision":"block","reason":"R-1",'
                                          '"hookSpecificOutput":{"hookEventName":"PostToolUse",'
                                          '"additionalContext":"CTX-1"},"systemMessage":"SYS-1"}\'\n')
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command", "command": script}]}]}})
        out = self.bash("echo n > n.txt")
        self.assertIn("R-1", out["reason"])
        self.assertIn("CTX-1", self.context(out))
        self.assertIn("SYS-1", out["systemMessage"])

    def test_match_all_and_bash_matchers_not_rerun(self):
        a = self.hook_script("all.sh", 'cat >/dev/null; echo all >> "$HOOK_LOG.all"\n')
        b = self.hook_script("bash.sh", 'cat >/dev/null; echo b >> "$HOOK_LOG.bash"\n')
        self.settings({"hooks": {"PostToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": a}]},
            {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": b}]}]}})
        self.bash("echo x >> src/app.py")
        self.assertFalse(os.path.exists(self.log + ".all"))
        self.assertFalse(os.path.exists(self.log + ".bash"))
        env = dict(self.env, BASH_BRIDGE_INCLUDE_MATCH_ALL="1")
        self.bash("echo y >> src/app.py", env=env)
        self.assertTrue(os.path.exists(self.log + ".all"))

    def test_user_local_and_plugin_sources(self):
        u = self.hook_script("u.sh", 'cat >/dev/null; echo user >> "$HOOK_LOG.src"\n')
        l = self.hook_script("l.sh", 'cat >/dev/null; echo local >> "$HOOK_LOG.src"\n')
        plug = os.path.join(self.tmp, "fake-plugin")
        os.makedirs(os.path.join(plug, "hooks"))
        os.makedirs(os.path.join(plug, "scripts"))
        with open(os.path.join(plug, "scripts", "p.sh"), "w") as fh:
            fh.write('#!/bin/bash\ncat >/dev/null; echo "plugin:$CLAUDE_PLUGIN_ROOT" >> "$HOOK_LOG.src"\n')
        os.chmod(os.path.join(plug, "scripts", "p.sh"), 0o755)
        with open(os.path.join(plug, "hooks", "hooks.json"), "w") as fh:
            json.dump({"hooks": {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/scripts/p.sh"}]}]}}, fh)
        os.makedirs(os.path.join(self.home, "plugins"))
        with open(os.path.join(self.home, "plugins", "installed_plugins.json"), "w") as fh:
            json.dump({"version": 2, "plugins": {"fake@mkt": [{"scope": "user", "installPath": plug}]}}, fh)
        self.settings({"enabledPlugins": {"fake@mkt": True},
                       "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": u}]}]}},
                      "user")
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": l}]}]}},
                      "local")
        self.bash("echo x >> src/app.py")
        got = sorted(open(self.log + ".src").read().split())
        self.assertEqual(got, sorted(["user", "local", "plugin:" + plug]))

    def test_recursion_guard(self):
        # a user hook that itself calls bash-bridge's hook (as a nested session would)
        inner = os.path.join(ROOT, "bin", "bash-bridge")
        script = self.hook_script("rec.sh", 'ev=$(cat); echo "$ev" | "%s" hook post > "$HOOK_LOG.rec"; '
                                            'echo "active=$BASH_BRIDGE_ACTIVE" >> "$HOOK_LOG.rec"\n' % inner)
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command",
                                                                                "command": script}]}]}})
        self.bash("echo x >> src/app.py")
        self.assertEqual(open(self.log + ".rec").read().strip(), "active=1")
        # bash-bridge's own hooks.json is never collected, even when its dir is listed as a plugin dir
        from bashbridge.hooks import collect
        os.environ["CLAUDE_CONFIG_DIR"] = self.home
        try:
            cfg = {"sources": ["plugins"], "plugin_dirs": [ROOT]}
            self.assertEqual(collect("PreToolUse", self.proj, cfg), [])
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]

    def test_timeout_respected(self):
        script = self.hook_script("slow.sh", "cat >/dev/null; sleep 30\n")
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": script, "timeout": 1}]}]}})
        start = time.time()
        out = self.bash("echo x >> src/app.py")
        self.assertLess(time.time() - start, 8)
        self.assertIn("timed out", out["systemMessage"])

    def test_if_condition(self):
        self.logging_hook("Edit|Write", extra={"if": "Edit(*.py)"})
        self.bash("echo x >> target.txt && echo y >> src/app.py")
        self.assertEqual([os.path.basename(e["tool_input"]["file_path"]) for e in self.hook_log()], ["app.py"])

    def test_formatter_hook_changes_are_part_of_undo(self):
        fmt = self.hook_script("fmt.sh", 'f=$(python3 -c "import sys,json;print(json.load(sys.stdin)[\'tool_input\']'
                                         '[\'file_path\'])"); echo "# formatted" >> "$f"\n')
        self.settings({"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command",
                                                                                "command": fmt}]}]}})
        self.bash("echo x >> src/app.py")
        self.assertTrue(self.read("src/app.py").endswith("# formatted\n"))
        code, out, err = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read("src/app.py"), "def f():\n    return 1\n")

    # ---- issue #89251: PreToolUse guards on Write|Edit are routed around by Bash
    def _guard(self):
        script = self.hook_script("guard.sh", 'f=$(python3 -c "import sys,json;print(json.load(sys.stdin)'
                                              '[\'tool_input\'][\'file_path\'])"); '
                                              'case "$f" in *target.txt) echo "target.txt is protected" >&2; '
                                              'exit 2;; esac; exit 0\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Write|Edit|NotebookEdit", "hooks": [
            {"type": "command", "command": script}]}]}})

    def test_pre_guard_reverts_denied_change(self):
        self._guard()
        out = self.bash("sed -i '' 's/keep/LOST/' target.txt && echo ok >> src/app.py")
        self.assertEqual(self.read("target.txt"), "keep me\n", "denied change must be rolled back")
        self.assertIn("ok", self.read("src/app.py"), "other files are untouched")
        self.assertEqual(out["decision"], "block")
        self.assertIn("target.txt is protected", out["reason"])
        self.assertIn("restored", out["reason"])

    def test_pre_guard_report_mode(self):
        self._guard()
        env = dict(self.env, BASH_BRIDGE_GUARD="report")
        out = self.bash("sed -i '' 's/keep/LOST/' target.txt", env=env)
        self.assertIn("LOST", self.read("target.txt"))
        self.assertIn("still on disk", out["reason"])

    def test_pre_guard_new_file_removed(self):
        script = self.hook_script("deny-new.sh", 'cat >/dev/null; echo \'{"hookSpecificOutput":{"hookEventName":'
                                                 '"PreToolUse","permissionDecision":"deny",'
                                                 '"permissionDecisionReason":"no new files"}}\'\n')
        self.settings({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
            {"type": "command", "command": script}]}]}})
        out = self.bash("echo x > secret.env")
        self.assertFalse(self.exists("secret.env"))
        self.assertIn("no new files", out["reason"])

    def test_opt_out_hooks_only(self):
        self.logging_hook()
        env = dict(self.env, BASH_BRIDGE_HOOKS="0")
        self.bash("echo x >> src/app.py", env=env)
        self.assertEqual(self.hook_log(), [])
        self.assertEqual(len(self.entries()), 1, "undo still recorded")

    def test_config_file_opt_out(self):
        self.logging_hook()
        with open(os.path.join(self.proj, ".claude", "bash-bridge.json"), "w") as fh:
            json.dump({"enabled": False}, fh)
        self.bash("echo x >> src/app.py")
        self.assertEqual(self.hook_log(), [])
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
