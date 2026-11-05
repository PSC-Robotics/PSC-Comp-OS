#!/usr/bin/env python3
"""
Publish a built PSC Robotics OS image as a GitHub release.

Tags the commit, pushes the tag, creates the release with the image attached and
its SHA-256 in the notes, then checks GitHub's copy matches the local file.

    release.py --image "PSC Robotics OS-image-2026.0.18.img.xz" --notes notes.md [--commit main]
               [--prerelease] [--dry-run]

Run with --dry-run first: it does every check and prints what it would do, without
changing anything locally or on GitHub.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "PSC-Robotics/PSC-Comp-OS"  # Releases must belong to the PSC-Robotics organisation.
IMAGE_NAME = re.compile(r"^PSC Robotics OS-image-(?P<version>[0-9]{4}\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?)\.img\.xz$")
SECRETS = re.compile(
    r"ol_api_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
)


def run(*command: str, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(
        command, check=False, capture_output=capture, text=True, errors="replace")
    if check and result.returncode != 0:
        sys.exit(f"Failed: {' '.join(command)}\n{result.stderr.strip()}")
    return (result.stdout or "").strip()


def fail(message: str) -> None:
    sys.exit(f"Stopped: {message}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, help="the built .img.xz file")
    parser.add_argument("--notes", required=True, help="Markdown release notes (the checksum section is added)")
    parser.add_argument("--commit", default="HEAD", help="the commit the image was built from (default HEAD)")
    parser.add_argument("--prerelease", action="store_true", help="mark as a pre-release, not the latest")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    args = parser.parse_args()

    image = Path(args.image).resolve()
    match = IMAGE_NAME.match(image.name)
    if not match:
        fail(f"{image.name!r} isn't named like 'PSC Robotics OS-image-<version>.img.xz'")
    if not image.is_file():
        fail(f"{image} doesn't exist")
    version = match["version"]
    if "-" in version and not args.prerelease:
        fail(f"{version} has a suffix (a branch build): pass --prerelease, so it isn't marked as the latest")

    # The repo and the account.
    origin = run("git", "remote", "get-url", "origin")
    if f"github.com/{REPO}" not in origin.replace(".git", ""):
        fail(f"origin is {origin}, not {REPO}: releases must be published by PSC-Robotics")
    view = json.loads(run("gh", "repo", "view", REPO, "--json", "owner,viewerPermission,visibility"))
    if view["owner"]["login"] != "PSC-Robotics":
        fail(f"{REPO} is owned by {view['owner']['login']}, not PSC-Robotics")
    if view["viewerPermission"] not in ("ADMIN", "MAINTAIN", "WRITE"):
        fail(f"this GitHub account can't publish to {REPO} ({view['viewerPermission']})")

    # The commit must already be on GitHub, and the working tree clean.
    commit = run("git", "rev-parse", f"{args.commit}^{{commit}}")
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        fail("there are uncommitted changes: commit or stash them first")
    run("git", "fetch", "-q", "origin")
    remote_branches = run("git", "branch", "-r", "--contains", commit)
    if not remote_branches:
        fail(f"commit {commit[:7]} isn't on GitHub yet: push its branch first")

    # The tag and release mustn't exist already.
    if run("git", "tag", "-l", version) or run("git", "ls-remote", "--tags", "origin", f"refs/tags/{version}"):
        fail(f"tag {version} already exists (each version is released once)")
    if subprocess.run(["gh", "release", "view", version, "--repo", REPO], capture_output=True).returncode == 0:
        fail(f"release {version} already exists on GitHub")

    # Nothing secret in what's being published.
    # The previous release is the newest one on GitHub that this commit builds on.
    previous = ""
    for tag in run("gh", "release", "list", "--repo", REPO, "--json", "tagName", "--jq", ".[].tagName").split():
        run("git", "fetch", "-q", "origin", f"refs/tags/{tag}:refs/tags/{tag}", check=False)
        if subprocess.run(["git", "merge-base", "--is-ancestor", tag, commit]).returncode == 0:
            previous = tag
            break
    history = run("git", "log", "-p", f"{previous}..{commit}" if previous else commit)
    if found := SECRETS.findall(history):
        fail(f"possible secrets in the commits being released: {sorted(set(found))[:3]}")

    # GitHub replaces spaces in asset names with dots.
    asset_name = image.name.replace(" ", ".")
    checksum = sha256(image)
    notes = Path(args.notes).read_text().rstrip() + (
        f"\n\n## Checksum\n\nSHA-256 of `{asset_name}`:\n\n```\n{checksum}\n```\n")
    notes_file = image.with_suffix(".release-notes.md")

    print(f"Release {version} ({'pre-release' if args.prerelease else 'latest'}) of {REPO}")
    print(f"  commit   {commit[:7]} on {', '.join(b.strip() for b in remote_branches.splitlines())}")
    print(f"  previous {previous or '(none)'}")
    print(f"  image    {image.name} ({image.stat().st_size / 1048576:.1f} MiB) -> {asset_name}")
    print(f"  sha256   {checksum}")
    if args.dry_run:
        print("\nDry run: nothing changed. Notes would be:\n")
        print(notes)
        return

    notes_file.write_text(notes)
    run("git", "tag", "-a", version, "-m", f"PSC Robotics OS {version}", commit)
    run("git", "push", "origin", f"refs/tags/{version}")
    create = ["gh", "release", "create", version, str(image), "--repo", REPO,
              "--title", f"PSC Robotics OS {version}", "--notes-file", str(notes_file), "--verify-tag"]
    create.append("--prerelease" if args.prerelease else "--latest")
    print(run(*create))
    notes_file.unlink()

    # Check GitHub's copy is complete and identical.
    assets = json.loads(run("gh", "release", "view", version, "--repo", REPO, "--json", "assets"))["assets"]
    asset = next((a for a in assets if a["name"] == asset_name), None)
    if asset is None or asset["state"] != "uploaded" or asset["size"] != image.stat().st_size:
        fail(f"the uploaded image doesn't match: {asset}")
    if asset.get("digest") and asset["digest"] != f"sha256:{checksum}":
        fail(f"GitHub's checksum {asset['digest']} doesn't match {checksum}")
    print(f"Published and verified: https://github.com/{REPO}/releases/tag/{version}")


if __name__ == "__main__":
    main()
