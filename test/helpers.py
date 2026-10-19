"""Test harness: simulates Claude Code calling bash-bridge's hooks around a Bash tool call."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "bash-bridge")
sys.path.insert(0, os.path.join(ROOT, "lib"))


def sh(cmd, cwd, env=None, check=True):
    p = subprocess.run(["/bin/bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise AssertionError("command failed: %s\n%s" % (cmd, p.stderr.decode()))
    return p


class BridgeCase(unittest.TestCase):
    git = True

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="bb-test-"))
        self.state = os.path.join(self.tmp, "state")
        self.home = os.path.join(self.tmp, "claude-home")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.home)
        os.makedirs(self.proj)
        self.log = os.path.join(self.tmp, "hook.log")
        self.session = "sess-" + uuid.uuid4().hex[:8]
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        open(self.transcript, "w").close()
        self.env = dict(os.environ)
        for k in list(self.env):
            if (k.startswith("BASH_BRIDGE_") and k != "BASH_BRIDGE_PYTHON") or k.startswith("GIT_") or k in ("CLAUDE_PLUGIN_ROOT",):
                del self.env[k]
        self.env.update({
            "BASH_BRIDGE_STATE_DIR": self.state,
            "CLAUDE_CONFIG_DIR": self.home,
            "CLAUDE_PROJECT_DIR": self.proj,
            "BASH_BRIDGE_DEBUG": os.path.join(self.tmp, "debug.log"),
            "HOOK_LOG": self.log,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        })
        # Claude Code records accepted workspace trust here; project hooks are bridged only then
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"projects": {self.proj: {"hasTrustDialogAccepted": True}}}, fh)
        if self.git:
            sh("git init -q -b main . && git config user.email t@t && git config user.name t", self.proj)
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def write(self, rel, text, mode="w"):
        p = os.path.join(self.proj, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as fh:
            fh.write(text)
        return p

    def read(self, rel, mode="r"):
        with open(os.path.join(self.proj, rel), mode) as fh:
            return fh.read()

    def exists(self, rel):
        return os.path.lexists(os.path.join(self.proj, rel))

    def commit(self, msg="init"):
        sh("git add -A && git commit -q -m %s" % msg, self.proj)

    def settings(self, data, where="project"):
        if where == "project":
            path = os.path.join(self.proj, ".claude", "settings.json")
        elif where == "local":
            path = os.path.join(self.proj, ".claude", "settings.local.json")
        else:
            path = os.path.join(self.home, "settings.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh)
        return path

    def hook_script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body)
        os.chmod(path, 0o755)
        return path

    def logging_hook(self, matcher="Write|Edit", event="PostToolUse", where="project", extra=None):
        script = self.hook_script("log-%s.sh" % event, 'cat >> "$HOOK_LOG"; echo >> "$HOOK_LOG"\n')
        h = {"type": "command", "command": script}
        if extra:
            h.update(extra)
        self.settings({"hooks": {event: [{"matcher": matcher, "hooks": [h]}]}}, where)
        return script

    def hook_log(self):
        if not os.path.exists(self.log):
            return []
        res = []
        for line in open(self.log):
            line = line.strip()
            if line:
                res.append(json.loads(line))
        return res

    def event(self, mode, cmd, tuid, cwd=None):
        ev = {
            "session_id": self.session, "transcript_path": self.transcript, "cwd": cwd or self.proj,
            "permission_mode": "auto", "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "test"}, "tool_use_id": tuid,
        }
        ev["hook_event_name"] = {"pre": "PreToolUse", "post": "PostToolUse",
                                 "post-failure": "PostToolUseFailure"}.get(mode, mode)
        if mode == "post":
            ev["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False}
        return ev

    def run_hook(self, mode, event, env=None, raw=None):
        start = time.time()
        p = subprocess.run([BIN, "hook", mode], input=(raw if raw is not None else json.dumps(event)).encode(),
                           env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.proj)
        elapsed = time.time() - start
        return p.returncode, p.stdout.decode(), p.stderr.decode(), elapsed

    def bash(self, cmd, cwd=None, env=None, expect_ok=True):
        """Simulate one Bash tool call: PreToolUse hook, the command, PostToolUse hook."""
        self.n += 1
        tuid = "toolu_%04d" % self.n
        code, out, _err, t_pre = self.run_hook("pre", self.event("pre", cmd, tuid, cwd), env)
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "PreToolUse must print nothing")
        p = subprocess.run(["/bin/bash", "-c", cmd], cwd=cwd or self.proj, env=env or self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        mode = "post" if p.returncode == 0 else "post-failure"
        if expect_ok and p.returncode != 0:
            raise AssertionError("bash failed: %s\n%s" % (cmd, p.stderr.decode()))
        code, out, _err, t_post = self.run_hook(mode, self.event(mode, cmd, tuid, cwd), env)
        self.assertEqual(code, 0)
        self.last_times = (t_pre, t_post)
        return json.loads(out) if out.strip() else None

    def cli(self, *args, env=None):
        p = subprocess.run([BIN] + list(args), env=env or self.env, cwd=self.proj,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def entries(self):
        code, out, err = self.cli("list", "--json", "-n", "0", "--project", self.proj)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def last_changes(self):
        e = self.entries()
        return sorted(tuple(c) for c in e[-1]["changes"]) if e else []

    def context(self, out):
        if not out:
            return ""
        return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
