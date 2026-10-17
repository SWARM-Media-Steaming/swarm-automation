"""Size guards: bounded patches, build-output detection and input-limit parsing."""
import unittest

import prompt_budget as pb


def section(name: str, size: int) -> str:
    return f"diff --git a/{name} b/{name}\n+++ b/{name}\n" + "+x\n" * size


class BoundDiffTests(unittest.TestCase):
    def test_a_patch_within_the_budget_is_returned_unchanged(self):
        diff = section("a.py", 3)
        self.assertEqual(pb.bound_diff(diff, "stat", 10_000), diff)

    def test_over_budget_keeps_small_files_whole_and_names_the_rest(self):
        diff = section("small.py", 5) + section("huge.bin", 50_000)
        bounded = pb.bound_diff(diff, " small.py | 5 +\n huge.bin | 50000 +", 5_000)
        self.assertLessEqual(len(bounded), 5_000 + 600)
        self.assertIn("diff --git a/small.py", bounded)
        self.assertNotIn("diff --git a/huge.bin", bounded)
        self.assertIn("Patches omitted for size", bounded)
        self.assertIn("huge.bin", bounded)
        self.assertIn("Change summary", bounded)

    def test_thousands_of_files_stay_bounded(self):
        diff = "".join(section(f"web/target/f{n}.h", 20) for n in range(3000))
        self.assertLess(len(pb.bound_diff(diff, "stat", 50_000)), 60_000)


class GeneratedPrefixTests(unittest.TestCase):
    def test_finds_the_top_most_build_directory(self):
        added = ["web/target/debug/build/x/ssl.h", "web/target/release/y", "src/app.py", "node_modules/a/b.js"]
        self.assertEqual(pb.generated_prefixes(added), ["node_modules", "web/target"])

    def test_ordinary_paths_and_a_file_named_like_a_directory_are_left_alone(self):
        self.assertEqual(pb.generated_prefixes(["src/target.rs", "docs/venv", "a/b/c.py"]), [])

    def test_summary_ranks_areas_by_file_count(self):
        paths = ["web/target/a"] * 3 + ["src/x.py"]
        self.assertTrue(pb.top_level_summary(paths).startswith("web/target (3 files)"))


class InputLimitTests(unittest.TestCase):
    CODEX = ('Error: turn/start: turn/start failed: Input exceeds the maximum length of 1048576 characters. '
             '(code -32602), data: {"input_error_code":"input_too_large","max_chars":1048576,"actual_chars":9187573}')

    def test_reads_the_codex_refusal(self):
        self.assertEqual(pb.parse_input_limit(self.CODEX), (9187573, 1048576))

    def test_other_wording_is_recognised_without_numbers(self):
        self.assertEqual(pb.parse_input_limit("API Error: prompt is too long"), (0, 0))

    def test_unrelated_failures_are_not_input_limits(self):
        for text in ("", "usage limit reached", "provider exited 1", "rate limit"):
            self.assertIsNone(pb.parse_input_limit(text), text)


if __name__ == "__main__":
    unittest.main()
