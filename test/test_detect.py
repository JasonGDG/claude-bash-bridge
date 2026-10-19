"""Change detection: what counts as created / modified / deleted after a Bash call."""
import os
import time

from helpers import BridgeCase, sh, unittest


class GitDetect(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("a.txt", "alpha\nbeta\ngamma\n")
        self.write("dir/b.txt", "bee\n")
        self.write(".gitignore", "ignored/\n*.log\n")
        self.commit()

    def test_create_modify_delete(self):
        self.bash("echo new > c.txt && printf 'x\\n' >> a.txt && rm dir/b.txt")
        self.assertEqual(self.last_changes(), [("A", "c.txt"), ("D", "dir/b.txt"), ("M", "a.txt")])

    def test_rename_is_delete_plus_create(self):
        self.bash("mv a.txt renamed.txt")
        self.assertEqual(self.last_changes(), [("A", "renamed.txt"), ("D", "a.txt")])

    def test_sed_in_place(self):
        self.bash("sed -i '' 's/beta/BETA/' a.txt")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])
        self.assertIn("BETA", self.read("a.txt"))

    def test_heredoc(self):
        self.bash("cat > dir/new.md <<'EOF'\n# hello\nworld\nEOF")
        self.assertEqual(self.last_changes(), [("A", "dir/new.md")])

    def test_python_write(self):
        self.bash("python3 - <<'EOF'\nopen('a.txt','w').write('rewritten\\n')\nEOF")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_spaces_and_unicode(self):
        self.bash("printf 'x' > 'with space.txt' && printf 'y' > 'ümlaut-日本.txt'")
        names = [c[1] for c in self.last_changes()]
        self.assertIn("with space.txt", names)
        self.assertTrue(any("mlaut" in n and n.endswith(".txt") for n in names), names)

    def test_binary(self):
        self.bash("head -c 2048 /dev/urandom > blob.bin")
        self.assertEqual(self.last_changes(), [("A", "blob.bin")])

    def test_ignored_files_not_reported(self):
        self.bash("mkdir -p ignored && echo x > ignored/a && echo y > run.log && echo z > kept.txt")
        self.assertEqual(self.last_changes(), [("A", "kept.txt")])

    def test_dirty_file_modified_again(self):
        self.write("a.txt", "dirty before the call\n")
        self.bash("echo more >> a.txt")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_commit_inside_bash_still_detected(self):
        # the file is clean before and after (committed in the same command)
        self.bash("echo changed > a.txt && git add a.txt && git commit -q -m x")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_git_checkout_restore(self):
        self.write("a.txt", "local edit\n")
        self.bash("git checkout -- a.txt")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_read_only_command_takes_no_snapshot(self):
        self.bash("cat a.txt | head -1 && ls")
        self.assertEqual(self.entries(), [])
        pend = os.path.join(self.state, "projects")
        self.assertFalse(os.path.isdir(pend) and any(
            os.listdir(os.path.join(pend, d, "pending")) for d in os.listdir(pend)
            if os.path.isdir(os.path.join(pend, d, "pending"))))

    def test_no_change_no_entry(self):
        self.bash("python3 -c 'print(1)'")
        self.assertEqual(self.entries(), [])

    def test_failed_command_still_detected(self):
        self.bash("echo partial > a.txt; exit 3", expect_ok=False)
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_mode_change(self):
        self.bash("chmod +x a.txt")
        self.assertEqual(self.last_changes(), [("M", "a.txt")])

    def test_project_index_untouched(self):
        idx = os.path.join(self.proj, ".git", "index")
        before = (os.path.getmtime(idx), open(idx, "rb").read())
        self.bash("echo q > q.txt && sed -i '' 's/alpha/A/' a.txt")
        after = (os.path.getmtime(idx), open(idx, "rb").read())
        self.assertEqual(before, after, "the project's .git/index must not be written")
        self.assertEqual(sh("git status --porcelain", self.proj).stdout.decode().count("\n"), 2)

    def test_typical_latency(self):
        self.bash("echo warm > w.txt")  # first call creates the shadow repo
        times = []
        for i in range(3):
            self.bash("echo %d >> w.txt" % i)
            times.append(sum(self.last_times))
        print("\n    pre+post latency (small repo, warm): %s ms" % [int(t * 1000) for t in times])
        self.assertLess(min(times), 1.5)


class NonGitDetect(BridgeCase):
    git = False

    def test_non_git_dir(self):
        self.write("x.txt", "1\n")
        self.write("node_modules/pkg/index.js", "ignored by default excludes\n")
        self.bash("echo 2 >> x.txt && echo n > y.txt && echo z > node_modules/pkg/new.js")
        self.assertEqual(self.last_changes(), [("A", "y.txt"), ("M", "x.txt")])


class HugeRepoCap(BridgeCase):
    git = False

    def test_cap_bails_out_quickly_with_note(self):
        for i in range(300):
            self.write("f/%03d.txt" % i, "x")
        env = dict(self.env, BASH_BRIDGE_MAX_FILES="100")
        start = time.time()
        out = self.bash("echo y > f/new.txt", env=env)
        elapsed = time.time() - start
        self.assertIsNotNone(out)
        self.assertIn("inactive", out.get("systemMessage", ""))
        self.assertIn("inactive", self.context(out), "Claude is told too")
        self.assertLess(elapsed, 3.0)
        # the note is shown once per session, later calls stay silent
        out2 = self.bash("echo z > f/new2.txt", env=env)
        self.assertIsNone(out2)

    def test_git_index_cap(self):
        sh("git init -q -b main .", self.proj)
        for i in range(150):
            self.write("g/%03d.txt" % i, "x")
        sh("git add -A && git -c user.email=t@t -c user.name=t commit -q -m i", self.proj)
        env = dict(self.env, BASH_BRIDGE_MAX_FILES="100")
        out = self.bash("echo y > g/new.txt", env=env)
        self.assertIn("150 tracked files exceed", out.get("systemMessage", ""))


if __name__ == "__main__":
    unittest.main()
