#!/usr/bin/env python3
"""Compute the release version from the VERSION file and git history.

`VERSION` holds the product version as of the commit that last changed it
(`MAJOR.MINOR.PATCH`; blank lines and `#` comments are ignored). Every later
commit on the branch's first-parent line adds one to the patch, so each push to
`main` (a direct commit, or the merge commit a promotion PR lands as) is exactly
one patch. Minor and major are changed only by editing `VERSION`.

    python3 scripts/compute_version.py --channel stable
    python3 scripts/compute_version.py --channel beta --run-number 42

Needs full history (`fetch-depth: 0` in CI); a shallow clone cannot be counted.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

VERSION_FILE = "VERSION"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class VersionError(RuntimeError):
    pass


def git(repo: str, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise VersionError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def parse_version_file(text: str) -> tuple[int, int, int]:
    lines = [line.strip() for line in text.splitlines()]
    entries = [line for line in lines if line and not line.startswith("#")]
    if len(entries) != 1:
        raise VersionError(f"{VERSION_FILE} must contain exactly one MAJOR.MINOR.PATCH line")
    match = VERSION_RE.match(entries[0])
    if not match:
        raise VersionError(f"{VERSION_FILE} has an invalid version: {entries[0]!r}")
    return int(match[1]), int(match[2]), int(match[3])


def compute_version(repo: str, channel: str, run_number: int | None, ref: str = "HEAD") -> str:
    if git(repo, "rev-parse", "--is-shallow-repository") == "true":
        raise VersionError("the patch number counts commits; check out full history (fetch-depth: 0)")
    try:
        text = git(repo, "show", f"{ref}:{VERSION_FILE}")
    except VersionError as error:
        raise VersionError(f"{VERSION_FILE} does not exist at {ref}") from error
    major, minor, patch = parse_version_file(text)
    changed_at = git(repo, "log", "--first-parent", "-1", "--format=%H", ref, "--", VERSION_FILE)
    if not changed_at:
        raise VersionError(f"{VERSION_FILE} has no history on {ref}")
    since = int(git(repo, "rev-list", "--first-parent", "--count", f"{changed_at}..{ref}"))
    version = f"{major}.{minor}.{patch + since}"
    if channel == "beta":
        if run_number is None:
            raise VersionError("--run-number is required for the beta channel")
        return f"{version}-beta.{run_number}"
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--run-number", type=int)
    arguments = parser.parse_args()
    try:
        print(compute_version(arguments.repo, arguments.channel, arguments.run_number, arguments.ref))
    except VersionError as error:
        print(f"compute_version: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
