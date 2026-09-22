# Releasing Attune

How a release is cut. Everything up to the last section is a command anybody can run on
a machine that can build Attune. The last section is the maintainer's alone: it needs
his GitHub account, and no session or script may do it for him.

## What you need

- A built app folder, produced by `python desktop/build.py`. It lands in `dist/Attune`.
- Inno Setup 6, the free installer compiler, from jrsoftware.org. `desktop/build_installer.py`
  looks for `ISCC.exe` in the three usual places and tells you if it is missing.
- 7-Zip, optional. Without it the portable zip is built with Python instead, more slowly.
- About 2 GB of free disk for the build folder and the artifacts together.

## 1. Set the version

Edit `VERSION` at the repository root. One line, no leading "v", nothing else:

    0.2.0

That file is the only place the version is written in *code*. `pyproject.toml`, the installer
script and both release scripts all read it. Never type a version number into any of them.

The **documentation** is the exception, and it has to be done by hand, because a download
instruction needs a real filename. Grep for the old version and update every hit:

    grep -rn "0\.1\.0" README.md INSTALL.md SECURITY.md docs/

Today that is the download filename in README.md and INSTALL.md, the `Get-FileHash` example
in three files, and the "Attune is v0.1.0" line in README.md. The links to the Releases page
carry no version and do not need touching.

## 2. Build the app

    python desktop/build.py

The build needs `desktop/ffbin/ffmpeg.exe`, which is git-ignored and absent from a fresh
clone or a worktree. Without it the build refuses rather than quietly producing an app that
cannot decode part of a real library. See INSTALL.md, under Development.

## 3. Make the release artifacts

    python tools/make_release.py --dist dist/Attune --out ../attune-release/v0.2.0

Before this step, regenerate the third-party notices against the build you just made,
with the build environment's own Python (it refuses any other):

    ..\mixer-ng\.venv-standalone\Scripts\python tools\gen_third_party_notices.py --app dist\Attune --venv ..\mixer-ng\.venv-standalone

Both the installer and the zip pick up `licenses\`, `THIRD_PARTY_NOTICES.md`, `NOTICE.md`
and `LICENSE` from the repository root at this point, so commit them first: what is in the
tree when the script runs is what ships beside `Attune.exe`.

The staging folder must be outside the repository; the script refuses an output folder
inside the folder it is packaging. It produces three files and prints their sizes and
checksums:

    AttuneSetup-<version>.exe
    Attune-<version>-win64.zip
    SHA256SUMS.txt

It does nothing else. No network, no git, no tagging, no uploading.

### What it refuses, and why

If the build folder contains any SQLite database (`*.db`, `*.sqlite`, or any of their
`-wal`, `-shm` or `-journal` sidecars) the script stops and names the file. A database
in there is almost certainly a personal music library, which lists every file path,
album and track name on the machine. `desktop/package.py` creates exactly that, on
purpose, for local testing; it is not a release tool and it refuses to run without
`--i-know-this-contains-my-library`. If you hit this refusal, build again into a clean
folder rather than deleting the database and carrying on.

The installer script refuses a second time, at the compiler, if the build folder holds a
file named `mixer.db`, and its file list excludes every database extension a third time. Three guards, because shipping someone's library once is
unrecoverable.

## 4. Check it before you publish it

First, the personal-data scan, with the machine-specific literals and with the flag that
refuses to pass without them:

    python tools/leak_check.py . --all --patterns ../attune/.leakpatterns --require-patterns

`.leakpatterns` is git-ignored and lives only in the maintainer's main checkout, so a
worktree or a CI checkout does not have it. Without it the scan runs the generic rules alone
and cannot see this machine's own paths, names or addresses. `--require-patterns` turns that
from a warning into a refusal, so a release can never be signed off by a scan that was in no
position to find anything. Continuous integration deliberately runs without the flag, and
without the literals, and that is correct: it is a second pair of eyes on the generic rules,
not the release gate.

Read the `[TREE CONTENTS]` section. That is what a push of the current tree would expose.
`[HISTORY CONTENTS]` is already public and fixing it means rewriting history, which is the
repository owner's decision and nobody else's.

Then the artifacts:

    cd ../attune-release/v0.2.0
    sha256sum -c SHA256SUMS.txt

Then install it somewhere disposable and take it back out again:

    AttuneSetup-0.2.0.exe /VERYSILENT /SUPPRESSMSGBOXES /DIR=C:\Temp\attune-test /NOICONS /CURRENTUSER
    C:\Temp\attune-test\unins000.exe /VERYSILENT /SUPPRESSMSGBOXES

Keep that test folder short. Windows refuses paths over 259 characters and Attune's own
files use about 120 of them.

## 5. Publish. Maintainer only.

Creating a tag and a GitHub Release uses the maintainer's own account. No script here
does it and no automated session may.

    git tag -a v0.2.0 -m "Attune 0.2.0"
    git push origin v0.2.0

    gh release create v0.2.0 --draft --title "Attune 0.2.0" --notes-file NOTES.md \
      ../attune-release/v0.2.0/AttuneSetup-0.2.0.exe \
      ../attune-release/v0.2.0/Attune-0.2.0-win64.zip \
      ../attune-release/v0.2.0/SHA256SUMS.txt

That leaves a draft nobody else can see. Open it on github.com, read it once more, and
press **Publish release**.

## Not done here

The installer is not built by continuous integration. The one environment that has ever
produced a working frozen Attune is the maintainer's PC, recorded package by package in
`desktop/requirements-build.txt`. A second build environment would be a second thing to
debug. CI runs the tests and the personal-data scan; it does not build.

The installer is not code-signed. Windows will show "Windows protected your PC" on first
run, and because the file is unsigned that warning returns with every new version rather
than fading as downloads accumulate. The release notes tell people to click More info,
then Run anyway.
