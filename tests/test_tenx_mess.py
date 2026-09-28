"""End-to-end tests for tenx-mess against throwaway git repos.

Every test runs with a temporary HOME, so nothing on the real machine is touched.
Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tenx-mess" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import scan  # noqa: E402

OLD = int(time.time()) - 60 * 86400


def sh(*cmd, cwd=None, env=None):
    out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError("%s failed: %s" % (" ".join(map(str, cmd)), out.stderr))
    return out.stdout


class Sandbox:
    """A fake HOME with a bare origin, a main checkout, and linked worktrees."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        self.repos = self.home / "Repos"
        self.repos.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home),
                        TENX_MESS_CONFIG=str(self.home / "cfg.json"),
                        GIT_AUTHOR_DATE="@%d +0000" % OLD, GIT_COMMITTER_DATE="@%d +0000" % OLD,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null")
        (self.home / "cfg.json").write_text(json.dumps({
            "code_roots": [str(self.repos)], "use_gh": False, "min_size_mb": 0, "stale_days": 14}))
        self.origin = self.home / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        self.main = self.repos / "app"
        self.git("clone", "-q", str(self.origin), str(self.main))
        (self.main / "README").write_text("hi\n")
        (self.main / ".gitignore").write_text("node_modules/\n")
        self.git("-C", str(self.main), "add", ".")
        self.git("-C", str(self.main), "commit", "-qm", "init")
        self.git("-C", str(self.main), "push", "-q", "origin", "main")
        self.git("-C", str(self.main), "remote", "set-head", "origin", "main")

    def git(self, *args):
        return sh("git", *args, env=self.env)

    def worktree(self, name, merged=False, dirty=False, deps=True):
        path = self.repos / "app.worktrees" / name
        self.git("-C", str(self.main), "worktree", "add", "-q", "-b", name, str(path))
        (path / ("%s.txt" % name)).write_text(name)
        self.git("-C", str(path), "add", ".")
        self.git("-C", str(path), "commit", "-qm", name)
        if merged:
            self.git("-C", str(self.main), "merge", "-q", "--ff-only", name)
            self.git("-C", str(self.main), "push", "-q", "origin", "main")
        if deps:
            (path / "node_modules" / "pkg").mkdir(parents=True)
            (path / "node_modules" / "pkg" / "index.js").write_text("x" * 5000)
        if dirty:
            (path / "README").write_text("edited\n")
            os.utime(path / "README", (OLD, OLD))
        gitdir = sh("git", "-C", str(path), "rev-parse", "--absolute-git-dir", env=self.env).strip()
        os.utime(Path(gitdir) / "logs" / "HEAD", (OLD, OLD))
        return path

    def py(self, script, *args, check=True):
        out = subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args),
                             env=self.env, capture_output=True, text=True)
        if check and out.returncode != 0:
            raise AssertionError(out.stdout + out.stderr)
        return out

    def report(self):
        self.py("scan.py", "--only", "worktrees,dormant")
        reports = sorted((self.home / ".tenx-mess" / "reports").glob("scan-*.json"))
        return {i["title"].rsplit("/", 1)[-1]: i for i in json.loads(reports[-1].read_text())["items"]}

    def plan_id(self, out):
        return out.stdout.split("plan ", 1)[1].split(" ", 1)[0]

    def close(self):
        self.tmp.cleanup()


class ClassifyTests(unittest.TestCase):
    def base(self, **kw):
        w = {"locked": False, "users": [], "age_days": 50, "merged": False, "pr": None,
             "detached": False, "on_branch": False, "dirty": 0, "deps_bytes": 100, "ref": "origin/main"}
        w.update(kw)
        return scan.classify_worktree(w, 14)

    def test_merged_clean_is_finished_remove(self):
        tier, _, actions, default = self.base(merged=True, pr={"state": "MERGED", "number": 1})
        self.assertEqual((tier, default), ("finished", "worktree_remove"))
        self.assertIn("strip_deps", actions)

    def test_merged_dirty_strips_deps_only(self):
        tier, _, actions, default = self.base(merged=True, pr={"state": "MERGED", "number": 1}, dirty=3)
        self.assertEqual((tier, default), ("finished", "strip_deps"))
        self.assertNotIn("worktree_remove", actions)

    def test_uncommitted_work_on_fresh_branch_is_review(self):
        tier, _, _, default = self.base(merged=True, dirty=20)
        self.assertEqual((tier, default), ("review", "strip_deps"))

    def test_recent_locked_or_in_use_is_keep(self):
        self.assertEqual(self.base(age_days=2)[0], "keep")
        self.assertEqual(self.base(locked=True, merged=True)[0], "keep")
        self.assertEqual(self.base(users=[{"pid": 1, "command": "claude"}])[0], "keep")

    def test_detached_commit_only_here_never_removed(self):
        tier, _, actions, _ = self.base(detached=True, on_branch=False)
        self.assertEqual(tier, "review")
        self.assertNotIn("worktree_remove", actions)

    def test_closed_pr_is_review_remove(self):
        tier, _, _, default = self.base(pr={"state": "CLOSED", "number": 9})
        self.assertEqual((tier, default), ("review", "worktree_remove"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_merged_worktree_is_removed_and_branch_kept(self):
        path = self.s.worktree("done-feature", merged=True)
        items = self.s.report()
        item = items["done-feature"]
        self.assertEqual(item["tier"], "finished")
        out = self.s.py("clean.py", "plan", "--select", item["id"])
        self.assertIn("worktree remove", out.stdout)
        self.assertTrue(path.exists(), "plan must not delete anything")
        self.s.py("clean.py", "run", "--plan", self.s.plan_id(out))
        self.assertFalse(path.exists())
        branches = self.s.git("-C", str(self.s.main), "branch", "--list", "done-feature")
        self.assertIn("done-feature", branches)
        logs = list((self.s.home / ".tenx-mess" / "log").glob("clean-*.json"))
        self.assertEqual(json.loads(logs[0].read_text())["results"][0]["status"], "done")

    def test_strip_deps_keeps_code_and_changes(self):
        path = self.s.worktree("wip", dirty=True)
        item = self.s.report()["wip"]
        self.assertEqual(item["default_action"], "strip_deps")
        out = self.s.py("clean.py", "plan", "--select", item["id"])
        self.s.py("clean.py", "run", "--plan", self.s.plan_id(out))
        self.assertFalse((path / "node_modules").exists())
        self.assertEqual((path / "README").read_text(), "edited\n")
        self.assertTrue((path / "wip.txt").exists())

    def test_worktree_that_got_dirty_after_scan_is_skipped(self):
        path = self.s.worktree("late-edit", merged=True)
        item = self.s.report()["late-edit"]
        out = self.s.py("clean.py", "plan", "--select", item["id"])
        (path / "new-work.txt").write_text("do not lose me")
        run = self.s.py("clean.py", "run", "--plan", self.s.plan_id(out))
        self.assertIn("uncommitted", run.stdout)
        self.assertTrue((path / "new-work.txt").exists())

    def test_edited_plan_is_refused(self):
        self.s.worktree("tamper", merged=True)
        item = self.s.report()["tamper"]
        out = self.s.py("clean.py", "plan", "--select", item["id"])
        pid = self.s.plan_id(out)
        plan_path = self.s.home / ".tenx-mess" / "plans" / ("%s.json" % pid)
        plan = json.loads(plan_path.read_text())
        plan["steps"][0]["path"] = str(self.s.home)
        plan_path.write_text(json.dumps(plan))
        run = self.s.py("clean.py", "run", "--plan", pid, check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("edited", run.stdout)
        self.assertTrue(self.s.home.exists())

    def test_symlinked_deps_are_not_followed(self):
        outside = self.s.home / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        path = self.s.worktree("linky", merged=True, deps=False, dirty=True)
        os.symlink(outside, path / "node_modules")
        item = self.s.report()["linky"]
        self.assertNotIn("strip_deps", item["actions"])
        self.assertTrue((outside / "keep.txt").exists())

    def approval_for(self, item_id):
        """What the confirmation page writes after the user approves one item."""
        self.s.py("clean.py", "preview", "--out", str(self.s.home / "seed-out.json"))
        seed = json.loads((self.s.home / ".tenx-mess" / "seed.json").read_text())
        item = next(i for i in seed["items"] if i["id"] == item_id)
        action = item["default_action"]
        return {"nonce": seed["nonce"], "typed": "clean",
                "selection": [{"id": item_id, "action": action}],
                "commands": item["options"][action]["commands"]}

    def plan_with(self, approval):
        path = self.s.home / "approval.json"
        path.write_text(json.dumps(approval))
        return self.s.py("clean.py", "plan", "--approval", str(path), check=False)

    def test_page_approval_with_matching_commands_plans(self):
        self.s.worktree("page-ok", merged=True)
        item = self.s.report()["page-ok"]
        out = self.plan_with(self.approval_for(item["id"]))
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("approved on the page", out.stdout)

    def test_page_approval_with_other_commands_is_refused(self):
        self.s.worktree("page-edit", merged=True)
        item = self.s.report()["page-edit"]
        approval = self.approval_for(item["id"])
        approval["commands"] = ["rm -rf ~"]
        out = self.plan_with(approval)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("differ", out.stdout)

    def test_stale_or_untyped_page_approval_is_refused(self):
        self.s.worktree("page-stale", merged=True)
        item = self.s.report()["page-stale"]
        approval = self.approval_for(item["id"])
        untyped = self.plan_with(dict(approval, typed=""))
        self.assertNotEqual(untyped.returncode, 0)
        self.assertIn("clean", untyped.stdout)
        old = dict(approval)
        self.approval_for(item["id"])  # a new preview makes the old approval stale
        out = self.plan_with(old)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("current preview", out.stdout)

    def test_keep_tier_cannot_be_planned(self):
        self.s.worktree("fresh", merged=False)
        gitdir = self.s.git("-C", str(self.s.repos / "app.worktrees" / "fresh"), "rev-parse", "--absolute-git-dir").strip()
        os.utime(Path(gitdir) / "logs" / "HEAD", None)  # touched now: recent
        item = self.s.report()["fresh"]
        self.assertEqual(item["tier"], "keep")
        out = self.s.py("clean.py", "plan", "--select", item["id"], check=False)
        self.assertIn("never enters a plan", out.stdout)


if __name__ == "__main__":
    unittest.main()
