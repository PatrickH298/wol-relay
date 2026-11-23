#!/usr/bin/env python3
"""Version checks for the image workflow.

The version lives in one place: `__version__ = "X.Y.Z"` in wol_relay.py.

  version.py check-pr BASE_SHA
      For pull requests. Fails if the image changed (IMAGE_FILES differ
      from BASE_SHA) without a version bump, if the version went backwards
      or skipped (e.g. 1.0.0 -> 1.0.2), or if the new version's tag
      already exists.

  version.py check-release
      For builds from main. Fails if the version's git tag already exists
      but IMAGE_FILES differ from it, so a published version is never
      overwritten with different code. Outputs create_tag=true when the
      tag doesn't exist yet.

Both write `version=X.Y.Z` (and check-release `create_tag=...`) to
$GITHUB_OUTPUT when it's set, and print them either way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

SCRIPT = "wol_relay.py"
# Anything that changes what ends up in the image.
IMAGE_FILES = ("wol_relay.py", "Dockerfile", ".dockerignore")
VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)

Version = tuple[int, int, int]


def fail(message: str) -> None:
    # ::error:: shows the message on the PR's checks summary.
    print(f"::error::{message}")
    raise SystemExit(1)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=check
    )


def parse(text: str | None) -> Version | None:
    if text is None:
        return None
    matches = VERSION_RE.findall(text)
    if len(matches) > 1:
        fail(f"{SCRIPT} sets __version__ more than once")
    return tuple(int(p) for p in matches[0]) if matches else None


def fmt(v: Version) -> str:
    return ".".join(str(p) for p in v)


def head_version() -> Version:
    with open(SCRIPT, encoding="utf-8") as f:
        version = parse(f.read())
    if version is None:
        fail(f'{SCRIPT} must contain a line like: __version__ = "1.2.3"')
    return version


def version_at(rev: str) -> Version | None:
    result = git("show", f"{rev}:{SCRIPT}", check=False)
    return parse(result.stdout) if result.returncode == 0 else None


def changed_image_files(rev: str) -> list[str]:
    result = git("diff", "--name-only", rev, "HEAD", "--", *IMAGE_FILES)
    return [line for line in result.stdout.splitlines() if line]


def tag_exists(tag: str) -> bool:
    return git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def output(**values: str) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    print("\n".join(lines))
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def next_versions(v: Version) -> list[Version]:
    major, minor, patch = v
    return [(major + 1, 0, 0), (major, minor + 1, 0), (major, minor, patch + 1)]


def check_pr(base: str) -> None:
    head = head_version()
    base_version = version_at(base)
    changed = changed_image_files(base)

    if base_version is None:
        # First versioned release: main has no __version__ yet.
        print(f"base has no version; {fmt(head)} will be the first release")
    elif head < base_version:
        fail(f"version went backwards: {fmt(base_version)} -> {fmt(head)}")
    elif head == base_version:
        if changed:
            fail(
                f"image files changed ({', '.join(changed)}) but the version "
                f"is still {fmt(head)}. Bump __version__ in {SCRIPT}: "
                f"MAJOR for breaking changes, MINOR for new features, "
                f"PATCH for fixes."
            )
        print(f"version unchanged at {fmt(head)}; no image files changed")
    elif head not in next_versions(base_version):
        options = ", ".join(fmt(v) for v in next_versions(base_version))
        fail(
            f"{fmt(base_version)} -> {fmt(head)} skips versions; "
            f"the next version should be one of: {options}"
        )
    else:
        print(f"version bumped {fmt(base_version)} -> {fmt(head)}")

    if head != base_version and tag_exists(f"v{fmt(head)}"):
        fail(f"v{fmt(head)} has already been released; pick a new version")

    output(version=fmt(head))


def check_release() -> None:
    head = head_version()
    tag = f"v{fmt(head)}"
    if tag_exists(tag):
        changed = changed_image_files(tag)
        if changed:
            fail(
                f"{tag} is already released, but image files differ from it "
                f"({', '.join(changed)}). Refusing to overwrite a published "
                f"version; bump __version__ in {SCRIPT}."
            )
        print(f"{tag} exists and the image files match it; rebuilding")
        output(version=fmt(head), create_tag="false")
    else:
        print(f"{tag} is a new release")
        output(version=fmt(head), create_tag="true")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["check-pr"] and len(args) == 2:
        check_pr(args[1])
    elif args == ["check-release"]:
        check_release()
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
