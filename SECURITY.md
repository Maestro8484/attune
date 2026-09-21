# Security policy

## Reporting a vulnerability

Please use GitHub's private reporting: go to the
[Security tab](https://github.com/Maestro8484/attune/security/advisories/new) of this
repository and open a draft advisory. That keeps the report private until there's a fix.

If that isn't available to you, open a normal issue saying only that you have a security
report and how to reach you. Don't put the details in a public issue.

This is a one-person project. Expect an acknowledgement within a week, not within a day.

## What's in scope

Attune runs on your own machine, on your own files. The things worth reporting:

- Anything that lets a **machine other than yours** reach a route that changes settings,
  writes files, or sends your Plex key somewhere. The app binds to `127.0.0.1` by default
  and the sensitive routes refuse non-local callers even when it's told to bind wider; a
  way around either of those is a real bug.
- Anything that **leaks your Plex key**, into a log, a playlist file, a served page, or an
  API response.
- Anything that makes Attune **write or delete a file outside the place you pointed it at**,
  including through a crafted filename or tag.
- Anything that makes a **crafted audio file** do more than fail to analyse.
- Anything that lets a **playlist or library file** cause code to run.

## What's out of scope

- **No password on the local web server.** That's the design. It listens on `127.0.0.1` and
  assumes your own machine is yours. If you run it with `--host 0.0.0.0` to reach it from a
  phone, you're on a trusted network by assumption and anyone who can reach the port can
  drive the parts of it that aren't loopback-guarded. Don't put it on the internet.
- **The `bridge/` component** binds to your whole network with no authentication, on
  purpose, and says so in its own README. That's a stated trust model, not a defect.
- **The installer isn't code-signed.** Known, stated in the README and the release notes.
- **A page in your own browser can reach loopback routes** and will look local to the
  server. That's true of every loopback-guarded application and isn't specific to Attune. A
  concrete exploit against Attune specifically is still worth reporting.
- **Vulnerabilities in bundled third-party components.** Report those upstream. Tell us too
  if Attune ships a version you can reach, and we'll update it.

## Checking what you downloaded

`SHA256SUMS.txt` on the [Releases page](https://github.com/Maestro8484/attune/releases)
covers both the installer and the portable zip.

```
Get-FileHash .\AttuneSetup-0.1.0.exe -Algorithm SHA256
```

The releases are not code-signed, so the checksum is the only thing tying the file you have
to the one that was published. Get it from the Releases page, not from a mirror.

## Supported versions

v0.1.0 is the first release. Fixes go into the next release; there are no backports.
