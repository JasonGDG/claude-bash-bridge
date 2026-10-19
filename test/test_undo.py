"""Undo for Bash changes, which /rewind does not cover (#87575)."""
import os

from helpers import BridgeCase, sh, unittest

DOC = "".join("line %02d\n" % i for i in range(1, 41))
DOC = DOC.replace("line 10\n", "line 10\n<!-- BEGIN generated -->\nold generated block\n<!-- END generated -->\n")


class Undo(BridgeCase):
    def setUp(self):
        super().setUp()
        self.write("doc.md", DOC)
        self.write("keep.txt", "keep\n")
        self.write("gone.txt", "will be deleted\n")
        self.commit()

    def undo(self, *args):
        code, out, err = self.cli("undo", *args, "--project", self.proj)
        return code, out + err

    def test_restore_recreate_remove(self):
        self.bash("echo changed > keep.txt && rm gone.txt && echo new > created.txt")
        code, out = self.undo()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("keep.txt"), "keep\n")
        self.assertEqual(self.read("gone.txt"), "will be deleted\n")
        self.assertFalse(self.exists("created.txt"))
        self.assertIn("restore", out)
        self.assertIn("recreate", out)
        self.assertIn("remove", out)
        self.assertEqual(sh("git status --porcelain", self.proj).stdout.decode(), "")

    def test_dry_run_changes_nothing(self):
        self.bash("echo changed > keep.txt && echo new > created.txt")
        code, out = self.undo("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would restore", out)
        self.assertIn("would remove", out)
        self.assertEqual(self.read("keep.txt"), "changed\n")
        self.assertTrue(self.exists("created.txt"))
        self.assertFalse(self.entries()[-1].get("undone"))

    def test_sed_range_truncation_recovered(self):
        """#87575 / #87971 comments: a sed range delete whose end pattern does not match
        deletes everything to EOF ("The excision took more than intended")."""
        self.bash("sed -i '' '/BEGIN generated/,/END generatd/d' doc.md")  # typo in end pattern
        self.assertLess(len(self.read("doc.md").splitlines()), 15, "file was truncated")
        code, out = self.undo()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("doc.md"), DOC)

    def test_undo_last_n_in_order(self):
        self.bash("echo one > keep.txt")
        self.bash("echo two > keep.txt && echo n > n.txt")
        self.bash("echo three > keep.txt")
        code, out = self.undo("2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("keep.txt"), "one\n")
        self.assertFalse(self.exists("n.txt"))
        code, out = self.undo()
        self.assertEqual(self.read("keep.txt"), "keep\n")

    def test_conflict_skipped_unless_forced(self):
        self.bash("echo bash > keep.txt")
        self.write("keep.txt", "edited later by the Edit tool\n")
        code, out = self.undo()
        self.assertEqual(code, 2)
        self.assertIn("SKIP", out)
        self.assertEqual(self.read("keep.txt"), "edited later by the Edit tool\n")
        code, out = self.undo("--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("keep.txt"), "keep\n")

    def test_untracked_and_dirty_pre_images(self):
        self.write("untracked.txt", "not in git yet\n")
        self.write("keep.txt", "dirty working copy\n")
        self.bash("rm untracked.txt && echo over > keep.txt")
        self.undo()
        self.assertEqual(self.read("untracked.txt"), "not in git yet\n")
        self.assertEqual(self.read("keep.txt"), "dirty working copy\n")

    def test_binary_unicode_and_mode(self):
        sh("head -c 4096 /dev/urandom > bin.dat && printf 'u' > 'ü file.txt' && chmod +x keep.txt", self.proj)
        before_bin = self.read("bin.dat", "rb")
        self.bash("head -c 100 /dev/urandom > bin.dat && rm 'ü file.txt' && chmod -x keep.txt")
        self.undo()
        self.assertEqual(self.read("bin.dat", "rb"), before_bin)
        self.assertEqual(self.read("ü file.txt"), "u")
        self.assertTrue(os.access(os.path.join(self.proj, "keep.txt"), os.X_OK))

    def test_list_and_slash(self):
        self.bash("echo a > keep.txt")
        code, out, _ = self.cli("list", "--project", self.proj)
        self.assertIn("echo a > keep.txt", out)
        self.assertIn("modified", out)
        p = sh("'%s' slash --dry-run" % os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "bash-bridge"),
               self.proj, env=self.env, check=False)
        self.assertIn("would restore", p.stdout.decode())

    def test_undo_command_itself_is_not_recorded(self):
        self.bash("echo a > keep.txt")
        self.bash("bash-bridge undo --project . || true")  # not on PATH here: still must not be recorded
        self.assertEqual(len(self.entries()), 1)

    def test_nothing_to_undo(self):
        code, out = self.undo()
        self.assertEqual(code, 1)
        self.assertIn("nothing to undo", out)


class UndoNonGit(BridgeCase):
    git = False

    def test_non_git_restore(self):
        self.write("a.txt", "orig\n")
        self.bash("echo zap > a.txt && echo n > b.txt")
        code, out, err = self.cli("undo", "--project", self.proj)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read("a.txt"), "orig\n")
        self.assertFalse(self.exists("b.txt"))


if __name__ == "__main__":
    unittest.main()
