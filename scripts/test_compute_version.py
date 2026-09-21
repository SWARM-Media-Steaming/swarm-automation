#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import compute_version as cv


class GitFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="compute-version-test.")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.counter = 0

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()

    def commit(self, message: str = "work") -> None:
        self.counter += 1
        (self.repo / f"file{self.counter}.txt").write_text(message, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-q", "-m", message)

    def set_version(self, version: str, header: bool = True) -> None:
        text = ("# comment\n" if header else "") + f"{version}\n"
        (self.repo / "VERSION").write_text(text, encoding="utf-8")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", f"Set version {version}")

    def version(self, channel: str = "stable", run_number: int | None = None, ref: str = "HEAD") -> str:
        return cv.compute_version(str(self.repo), channel, run_number, ref)


class ComputeVersionTests(GitFixture):
    def test_the_commit_that_sets_the_version_is_that_version(self) -> None:
        self.commit("before versioning existed")
        self.set_version("0.1.1")
        self.assertEqual(self.version(), "0.1.1")

    def test_every_later_commit_adds_one_to_the_patch(self) -> None:
        self.set_version("0.1.1")
        self.commit()
        self.assertEqual(self.version(), "0.1.2")
        self.commit()
        self.commit()
        self.assertEqual(self.version(), "0.1.4")

    def test_changing_the_file_restarts_the_patch_at_the_new_value(self) -> None:
        self.set_version("0.1.1")
        self.commit()
        self.commit()
        self.set_version("0.2.0")
        self.assertEqual(self.version(), "0.2.0")
        self.commit()
        self.assertEqual(self.version(), "0.2.1")
        self.set_version("1.0.0")
        self.assertEqual(self.version(), "1.0.0")

    def test_versions_only_ever_increase_across_a_minor_bump(self) -> None:
        self.set_version("0.1.1")
        seen = [self.version()]
        for _ in range(3):
            self.commit()
            seen.append(self.version())
        self.set_version("0.2.0")
        seen.append(self.version())
        self.commit()
        seen.append(self.version())
        as_tuples = [tuple(int(part) for part in version.split(".")) for version in seen]
        self.assertEqual(as_tuples, sorted(as_tuples))
        self.assertEqual(len(set(as_tuples)), len(as_tuples))

    def test_a_promotion_merge_counts_once_however_many_commits_it_carries(self) -> None:
        self.set_version("0.1.1")
        self.git("switch", "-q", "-c", "ai-main")
        for _ in range(4):
            self.commit()
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "Merge ai-main", "ai-main")
        self.assertEqual(self.version(), "0.1.2")

    def test_a_promotion_merge_that_carries_a_minor_bump_is_the_new_minor(self) -> None:
        self.set_version("0.1.1")
        self.commit()
        self.git("switch", "-q", "-c", "ai-main")
        self.commit()
        self.set_version("0.2.0")
        self.commit()
        self.git("switch", "-q", "main")
        self.commit("hotfix on main")
        self.git("merge", "-q", "--no-ff", "-m", "Merge ai-main", "ai-main")
        self.assertEqual(self.version(), "0.2.0")
        self.commit()
        self.assertEqual(self.version(), "0.2.1")

    def test_beta_channel_appends_the_run_number(self) -> None:
        self.set_version("0.1.1")
        self.commit()
        self.assertEqual(self.version("beta", 42), "0.1.2-beta.42")
        with self.assertRaises(cv.VersionError):
            self.version("beta", None)

    def test_a_named_ref_is_computed_independently_of_the_checkout(self) -> None:
        self.set_version("0.1.1")
        self.git("switch", "-q", "-c", "other")
        self.commit()
        self.commit()
        self.git("switch", "-q", "main")
        self.assertEqual(self.version(), "0.1.1")
        self.assertEqual(self.version(ref="other"), "0.1.3")


class ComputeVersionErrorTests(GitFixture):
    def test_a_missing_version_file_is_an_error(self) -> None:
        self.commit()
        with self.assertRaisesRegex(cv.VersionError, "does not exist"):
            self.version()

    def test_malformed_version_files_are_errors(self) -> None:
        for bad in ("0.1", "v0.1.1", "0.1.1-beta", "one.two.three", ""):
            (self.repo / "VERSION").write_text(f"{bad}\n", encoding="utf-8")
            self.git("add", "VERSION")
            self.git("commit", "-q", "-m", f"bad {bad!r}", "--allow-empty")
            with self.assertRaises(cv.VersionError, msg=repr(bad)):
                self.version()

    def test_two_version_lines_are_ambiguous(self) -> None:
        (self.repo / "VERSION").write_text("0.1.1\n0.1.2\n", encoding="utf-8")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", "two")
        with self.assertRaisesRegex(cv.VersionError, "exactly one"):
            self.version()

    def test_a_shallow_clone_is_refused_because_it_cannot_be_counted(self) -> None:
        self.set_version("0.1.1")
        self.commit()
        self.commit()
        shallow = Path(self.temporary.name) / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(shallow)],
            check=True, stderr=subprocess.DEVNULL,
        )
        with self.assertRaisesRegex(cv.VersionError, "full history"):
            cv.compute_version(str(shallow), "stable", None)


class CommandLineTests(GitFixture):
    def test_prints_only_the_version_and_exits_nonzero_on_error(self) -> None:
        script = Path(cv.__file__).resolve()
        self.commit()
        failed = subprocess.run(
            ["python3", str(script), "--repo", str(self.repo)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.set_version("0.1.1")
        self.commit()
        ok = subprocess.run(
            ["python3", str(script), "--repo", str(self.repo), "--channel", "beta", "--run-number", "7"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual((ok.returncode, ok.stdout), (0, "0.1.2-beta.7\n"))


if __name__ == "__main__":
    unittest.main()
