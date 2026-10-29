---
name: psc-release
description: Publish a built PSC Robotics OS image as a GitHub release on PSC-Robotics/PSC-Comp-OS - tag the commit, attach the .img.xz, write release notes with its SHA-256, and verify the upload. Use this whenever an image has been built and checked and someone wants it released, published, put on GitHub, tagged, or made downloadable, including branch builds (published as pre-releases), even if they just say "release it" or "put it on GitHub".
---

# Releasing a PSC Robotics OS image

Every image that is built and verified gets a GitHub release. The release is how
PSC-Robotics members download images, so it has to point at the exact commit the
image was built from, carry the exact file, and say what changed.

`scripts/release.py` does the mechanical part and refuses to continue if anything
is wrong. Your part is the judgement around it: confirming the image is ready,
choosing latest or pre-release, and writing notes a robot club member can follow.

## Before releasing

1. **The image is built and checked.** Only release an image that has been mounted
   and checked (library version, services, config). An unverified image wastes
   people's time flashing it.
2. **Know which commit built it.** Usually the branch's latest commit. If commits were
   made after the build, use the build's commit with `--commit`, or say plainly in
   the notes that later commits didn't change the image (as with 2026.0.17).
3. **Push the commit first.** The script stops if the commit isn't on GitHub. Pushing
   publishes code, so only push to `PSC-Robotics/PSC-Comp-OS`, never a personal
   account or fork. The repository must stay owned by PSC-Robotics.

## Latest or pre-release

- A build from `main` with a plain version (`2026.0.18`) is the **latest** release.
- A build from any other branch is a **pre-release**, with a version suffix such as
  `2026.0.17-bookworm1`, so it never replaces the latest release people download.
  The script insists on `--prerelease` for a suffixed version.
- Each version is released once. To fix a broken release, build a new version
  rather than replacing the file.

Don't create releases for old versions retrospectively. The user decided that
rebuilding old images is a waste of time.

## Writing the notes

Write the notes in a Markdown file. The script appends the checksum section itself.
Base them on `git log <previous release>..<commit>`. Write for students and
mentors, not developers:

```markdown
PSC Robotics OS for the brain board's Raspberry Pi (Pi 3, Pi 4 and Pi 400). Flash
`PSC.Robotics.OS-image-<version>.img.xz` to an SD card with Raspberry Pi Imager or
Etcher: see [Updating your Brain Board](https://docs.psc-robotics.uk/doc/updating-the-robot-os-xlSm2XpJ0E).

Robot library: `sr-robot3 <PSC_SR_VERSION>`. Default sign-in: `robot` / `robot`.

## What's new in <version>

- **The change people will notice**, in plain words, and what to do about it.
- Link features to their page on docs.psc-robotics.uk.
```

- Name the asset with dots, as above: GitHub replaces the spaces in
  `PSC Robotics OS-image-....img.xz` with dots.
- Get the library version from `PSC_SR_VERSION` in `files/python/install-packages.sh`
  at that commit.
- For a pre-release, say first that it's experimental, what it's for, and what hasn't
  been tested on real hardware.
- The default sign-in `robot` / `robot` is public and documented, so it's fine to
  include. Never include anything else secret.

## Publishing

Always do a dry run first and read its output: the commit, the previous release,
the file and the checksum.

```bash
python3 .claude/skills/psc-release/scripts/release.py \
  --image "PSC Robotics OS-image-<version>.img.xz" --notes /path/to/notes.md --dry-run
```

If it's right, run it again without `--dry-run`, adding `--prerelease` for a branch
build. The script then:

- checks the image is named correctly, origin is PSC-Robotics, and this account can
  publish;
- checks the commit is on GitHub, the tree is clean, and the version isn't released yet;
- scans the commits since the previous release for secrets (API keys, tokens,
  private keys);
- tags, pushes the tag, creates the release with the image, and then checks GitHub's
  copy has the same size and SHA-256 as the local file.

If it stops, fix what it says. Don't work around a check: each one stops a release
that is wrong or unsafe.

## Afterwards

- Tell the user the release URL, whether it's latest or a pre-release, and the checksum.
- The repository is private, so only PSC-Robotics members can download it. Mention
  this if the image is meant for students or other teams.
- Update the docs if they need it: a version row on the PSC Robotics OS page, and
  the "latest version" on the Brain Board and Updating your Brain Board pages.
- Delete the previous local image file once the new one is released (the user's
  preference), keeping images from other branches.
