"""Compute next MAJOR.MINOR from Conventional Commits since last vX.Y tag.

Prints: ``MAJOR MINOR BUILD`` (space separated).  BUILD = commit count on the
current branch (used as the third, numeric MSI ProductVersion field).
"""
import re
import subprocess


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


def last_tag():
    t = sh("git", "tag", "--list", "v*.*", "--sort=-v:refname").splitlines()
    return t[0] if t else None


def commits_since(tag):
    rng = f"{tag}..HEAD" if tag else "HEAD"
    log = sh("git", "log", rng, "--pretty=%B%x1e")
    return [c.strip() for c in log.split("\x1e") if c.strip()]


def main():
    tag = last_tag()
    if tag:
        m = re.match(r"v(\d+)\.(\d+)", tag)
        major, minor = int(m[1]), int(m[2])
    else:
        major, minor = 1, 0
    msgs = commits_since(tag)
    breaking = any(
        re.search(r"^\w+(\([^)]*\))?!:", m, re.M) or "BREAKING CHANGE:" in m
        for m in msgs
    )
    feature = any(
        re.match(r"^(feat|fix|perf|refactor|chore|docs|build|ci)", m) for m in msgs
    )
    if tag is None:
        pass  # keep 1.0 as first release
    elif breaking:
        major, minor = major + 1, 0
    elif feature:
        minor += 1
    build = int(sh("git", "rev-list", "--count", "HEAD") or "0")
    print(f"{major} {minor} {build}")


if __name__ == "__main__":
    main()
