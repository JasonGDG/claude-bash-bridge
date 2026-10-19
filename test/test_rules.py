"""Rules injection: path-scoped .claude/rules and nested CLAUDE.md for Bash-touched files (#90450, #88565)."""
import json
import os

from helpers import BridgeCase, unittest

from bashbridge.rules import expand_braces, glob_match, parse_frontmatter

RULE = """---
paths:
  - "src/api/**"
---

# API rule
Every file changed in src/api ends with the comment // RULE-MARKER-7731
"""


class Globs(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(glob_match("src/api/**", "src/api/users.ts"))
        self.assertTrue(glob_match("src/api/**", "src/api/v1/deep/x.ts"))
        self.assertFalse(glob_match("src/api/**", "src/apix/users.ts"))
        self.assertTrue(glob_match("**/*.ts", "a.ts"))
        self.assertTrue(glob_match("**/*.ts", "x/y/a.ts"))
        self.assertTrue(glob_match("*.md", "README.md"))
        self.assertFalse(glob_match("*.md", "docs/README.md"))
        self.assertTrue(glob_match("src/**/*.{ts,tsx}", "src/a/b.tsx"))
        self.assertFalse(glob_match("src/**/*.{ts,tsx}", "src/a/b.js"))
        self.assertTrue(glob_match("src/components/*.tsx", "src/components/A.tsx"))
        self.assertFalse(glob_match("src/components/*.tsx", "src/components/x/A.tsx"))
        self.assertTrue(glob_match("file[0-9].txt", "file3.txt"))
        self.assertFalse(glob_match("photos [2024/**", "photos [2024/a"))  # invalid -> matches nothing
        self.assertTrue(glob_match("photos \\[2024/**", "photos [2024/a"))
        self.assertEqual(len(expand_braces("{a,b}/{c,d}/*.{ts,tsx}")), 8)

    def test_frontmatter(self):
        self.assertEqual(parse_frontmatter(RULE)[0], ["src/api/**"])
        self.assertEqual(parse_frontmatter('---\npaths: ["a/**", "b/*.ts"]\n---\nx')[0], ["a/**", "b/*.ts"])
        self.assertEqual(parse_frontmatter("---\npaths: a/**, b/**\n---\nx")[0], ["a/**", "b/**"])
        self.assertEqual(parse_frontmatter("no frontmatter")[0], None)
        self.assertNotIn("paths", parse_frontmatter(RULE)[1])


class RulesInjection(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write(".claude/rules/api.md", RULE)
        self.write(".claude/rules/always.md", "# unconditional rule, loaded at launch\nALWAYS-LOADED\n")
        self.write("src/api/CLAUDE.md", "<!-- maintainer note -->\nNESTED-MARKER-4412: use snake_case here\n")
        self.write("src/api/users.ts", "export const users = 1;\n")
        self.write("src/other.ts", "x\n")
        self.commit()

    def test_rule_and_nested_claude_md_injected_for_sed_edit(self):
        out = self.bash("sed -i '' 's/1/2/' src/api/users.ts")
        ctx = self.context(out)
        self.assertIn("RULE-MARKER-7731", ctx)
        self.assertIn("NESTED-MARKER-4412", ctx)
        self.assertNotIn("ALWAYS-LOADED", ctx, "rules without paths already load at launch")
        self.assertNotIn("maintainer note", ctx, "HTML comments are stripped like Claude Code does")
        self.assertNotIn('paths:\n  - "src/api/**"', ctx, "frontmatter is stripped")
        self.assertIn("src/api/users.ts", ctx)

    def test_baseline_without_bridge(self):
        env = dict(self.env, BASH_BRIDGE_DISABLE="1")
        self.assertIsNone(self.bash("sed -i '' 's/1/2/' src/api/users.ts", env=env))

    def test_heredoc_new_file(self):
        out = self.bash("cat > src/api/new.ts <<'EOF'\nexport {}\nEOF")
        self.assertIn("RULE-MARKER-7731", self.context(out))

    def test_unrelated_file_gets_nothing(self):
        self.assertIsNone(self.bash("echo y >> src/other.ts"))

    def test_dedupe_per_session(self):
        self.assertIn("RULE-MARKER-7731", self.context(self.bash("echo a >> src/api/users.ts")))
        self.assertIsNone(self.bash("echo b >> src/api/users.ts"))
        self.session = "another-session"
        self.assertIn("RULE-MARKER-7731", self.context(self.bash("echo c >> src/api/users.ts")))

    def test_skips_what_claude_code_already_loaded(self):
        with open(self.transcript, "w") as fh:
            for p in (".claude/rules/api.md", "src/api/CLAUDE.md"):
                fh.write(json.dumps({"type": "attachment", "attachment": {
                    "type": "nested_memory", "path": os.path.join(self.proj, p)}}) + "\n")
        self.assertIsNone(self.bash("echo a >> src/api/users.ts"))

    def test_reinjected_after_compact(self):
        self.bash("echo a >> src/api/users.ts")
        ev = {"session_id": self.session, "hook_event_name": "SessionStart", "source": "compact",
              "cwd": self.proj}
        self.assertEqual(self.run_hook("session-start", ev)[0], 0)
        self.assertIn("RULE-MARKER-7731", self.context(self.bash("echo b >> src/api/users.ts")))

    def test_piped_read_gets_rules(self):
        # 2.1.296 measurement: piped / cd&& reads load nothing in Claude Code
        out = self.bash("cat src/api/users.ts | head -3")
        self.assertIn("RULE-MARKER-7731", self.context(out))

    def test_cd_and_read(self):
        out = self.bash("cd src/api && cat users.ts")
        self.assertIn("NESTED-MARKER-4412", self.context(out))

    def test_plain_single_read_left_to_claude_code(self):
        self.assertIsNone(self.bash("cat src/api/users.ts"))
        env = dict(self.env, BASH_BRIDGE_RULES_ON_READ="all")
        self.assertIn("RULE-MARKER-7731", self.context(self.bash("cat src/api/users.ts", env=env)))

    def test_rules_opt_out(self):
        env = dict(self.env, BASH_BRIDGE_RULES="0")
        self.assertIsNone(self.bash("echo a >> src/api/users.ts", env=env))

    def test_size_caps(self):
        self.write(".claude/rules/big.md", '---\npaths: ["src/**"]\n---\n' + ("BIG " * 5000))
        out = self.bash("echo a >> src/api/users.ts")
        ctx = self.context(out)
        self.assertLess(len(ctx), 9600)
        self.assertIn("truncated by bash-bridge", ctx)
        self.assertIn("RULE-MARKER-7731", ctx)

    def test_claude_md_excludes(self):
        self.settings({"claudeMdExcludes": [os.path.join(self.proj, "src/api/CLAUDE.md")]})
        ctx = self.context(self.bash("echo a >> src/api/users.ts"))
        self.assertIn("RULE-MARKER-7731", ctx)
        self.assertNotIn("NESTED-MARKER-4412", ctx)

    def test_nested_rules_dir_and_user_rules(self):
        self.write("src/api/.claude/rules/local.md", '---\npaths: ["*.ts"]\n---\nNESTED-RULES-DIR-9\n')
        os.makedirs(os.path.join(self.home, "rules"))
        with open(os.path.join(self.home, "rules", "u.md"), "w") as fh:
            fh.write('---\npaths: ["src/**/*.ts"]\n---\nUSER-RULE-5\n')
        ctx = self.context(self.bash("echo a >> src/api/users.ts"))
        self.assertIn("NESTED-RULES-DIR-9", ctx)
        self.assertIn("USER-RULE-5", ctx)


if __name__ == "__main__":
    unittest.main()
