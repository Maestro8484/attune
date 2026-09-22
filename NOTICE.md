# Notices, legal considerations and attributions

This file says exactly what Attune is, what is inside it, and under what terms each piece is offered. It is informational. It is not legal advice.

The short version is at the top because it is the part people get wrong.

## 1. Two different things, two different licenses

**Attune's source code is MIT.** Anyone can take it, change it, and ship it, including in a closed product, as long as they keep the copyright notice and the permission notice with it. That is [LICENSE](LICENSE), and it covers the code this project wrote. Two things are not Attune's to license that way, and each is called out where it appears: the CLAP model weights, which are LAION's and are fetched separately rather than kept in the repository (section 8), and one file that carries a port of somebody else's code (section 2). The optional learned head under `src/models/` is described in sections 4 and 8.

**The installed Windows application is offered under the GNU General Public License, version 3 or later.** Not because of anything Attune's own code does, but because of what is bundled inside the installer: a tag-reading library that is GPL, an FFmpeg build that is GPL version 3, and the PyInstaller runtime the app is frozen with. Section 3 explains it and says what follows from it.

Nothing about how you personally use Attune changes because of this. You can analyze your library, make playlists, export them, and never think about any of it again. The distinction matters to one group of people: anyone who wants to take the built application, or parts of it, and pass it on.

## 2. Attune's own code

- Licensed **MIT** ([LICENSE](LICENSE)). Permissive: use, modify and redistribute freely, with attribution and no warranty.
- Written as an **independent reimplementation**. It contains **no source code, binaries, decompiled output, or data files from MusicIP / MusicMagic / Predixis**. No MusicIP program file was ever disassembled or decompiled, and nothing in this repository reads a MusicIP file format. Two of MusicIP's own data files were examined as raw bytes during the research, which is a different thing, and section 4 says so there rather than leaving it for somebody to find.

Earlier versions of this file called that a "clean-room" implementation. That word is wrong and it has been removed. Clean-room means something specific: one team studies the original and writes a specification, and a second team, who never saw the original, builds from that specification alone. Attune was written by one person who did both. What is true, and is the thing actually worth saying, is that where Attune's behaviour matches MusicIP's, it came from measuring the running program through its own local network interface, and never from taking the program apart. Section 4 says exactly where that happened, including the one place it reached the engine that ships on by default.

**One file of Attune's own code carries a port of somebody else's work, and it is credited in both directions.** The log-mel front-end in `src/embed_onnx.py`, which turns audio into the exact input the CLAP model expects without needing torch, is a numpy port of the corresponding numeric path in Hugging Face Transformers 5.13.0 (`feature_extraction_clap.py` and `audio_utils.py`), which is Apache-2.0. The attribution is in that file's own comments, it travels with the file into the installed app, and the Apache-2.0 text is in [`licenses/`](licenses/) with the rest. The generator that writes the notices checks on every run that the attribution comment is still in the shipped file.

## 3. What license the installed application is under, and why

Three bundled components decide this. All three are in the installer, all three are named in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and all three have their full licence text in [`licenses/`](licenses/).

| Inside the installed app | License | How it is used |
|---|---|---|
| **mutagen** 1.48.1 | GPL-2.0-or-later | Imported into both programs, in the same process, to read and write the tags on your files. |
| **FFmpeg** 7.1, the `full_build` from gyan.dev | GPL-3.0-or-later (built with `--enable-gpl --enable-version3`) | Run as a separate program by the analyzer, to decode what libsndfile cannot: m4a, aac, wma, and the mp3 files whose last fraction of a second carries stray bytes libsndfile will not step over. |
| **PyInstaller** 6.21.0 | GPL-2.0-or-later with a bootloader exception | The tool that freezes Attune into an .exe. Its bootloader carries an explicit exception allowing it inside an application under any licence, but its package tree also ends up inside both programs as a side effect of the build. |

mutagen is linked into the program itself, so the program as distributed is a combined work that includes GPL code. mutagen is offered "version 2 or later", so version 3 can be chosen, and choosing version 3 is also what makes the Apache-2.0 components in the bundle compatible with the rest. FFmpeg's build is already version 3, and PyInstaller is likewise "version 2 or later". That gives one consistent answer for the whole install rather than a pile of caveats: **the installed application is conveyed under GPL-3.0-or-later.**

**What this means for the person who installs Attune: nothing.** You may run it, for anything, forever, and you owe nobody anything.

**What this means for anyone redistributing the built application:** the GPL's conditions come with it. The complete corresponding source for Attune's own part is this public repository, and every bundled component's source is published by its own project. The licence texts are in [`licenses/`](licenses/), with [`licenses/SOURCES.md`](licenses/SOURCES.md) recording where each hand-placed one came from and when. The release installer copies that folder next to the program so the texts travel with the application and not only with the source; the last item in section 10 is the check that confirms it did.

**Corresponding source for the bundled FFmpeg.** The binary shipped is an unmodified build published by gyan.dev, which states its builds are GPLv3 and links to the exact FFmpeg commit each one was built from ([gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/), source at [ffmpeg.org](https://ffmpeg.org/download.html)). Attune does not patch it, wrap it, or link against it; it is launched as a separate program. If you would rather have the source from us than fetch it yourself, open an issue on this repository and we will send you the corresponding source for the exact build shipped.

**Two libraries are LGPL and are worth naming separately**, because the way they are built differs. **libsndfile** is a DLL that ships next to the program and is loaded at run time, which is the ordinary LGPL arrangement. **libsoxr** is compiled into the `python-soxr` extension module rather than shipped as its own DLL. Both wheels are the unmodified ones from PyPI, and both projects publish their source. Under the GPL-3.0 position above, LGPL-2.1-or-later terms are compatible either way.

**If you want a build with no GPL parts in it**, three things have to change: mutagen, which is what reads and writes tags; the bundled FFmpeg; and the tool the app is frozen with. Replacing mutagen was considered for v0.1.0 and rejected, because Attune writes tags through it and it is not a drop-in swap.

**Leaving FFmpeg out costs more than the unusual formats.** It is not only m4a, aac and wma. Measured on this project's own library: 22 of a random 300 mp3 files, 7.3%, could not be decoded without it, and in the folder where loose singles live the figure was 875 of 4,007, nearly 22%, tested in full rather than sampled. On a 20,000-file mp3 collection that is roughly 1,500 songs that would quietly never be analyzed, with nothing on screen to say so. Anyone dropping FFmpeg should expect to lose about a fourteenth of an ordinary mp3 library, not three unusual formats.

## 4. Relationship to MusicIP / MusicMagic

Attune is inspired by, and aims to be a spiritual successor to, the discontinued **MusicIP Mixer** (formerly *MusicMagic Mixer*, by Predixis/MusicIP). To be unambiguous:

- **Not affiliated, not endorsed.** "MusicIP", "MusicMagic" and "Predixis" are trademarks of their respective owners. They are used here only *nominatively*, to describe what Attune is compatible with and descended from. This is standard nominative fair use and does not imply any endorsement or partnership.
- **No proprietary code or binaries are included or redistributed.** Do not commit `MusicMagicMixer.exe`, `mipcore.exe`, `genpuid`, `*.m3lib`, `register.key`, `client.pem`, or any other MusicIP asset to this repository. `.gitignore` blocks the common ones as a safety net, but the responsibility is yours.
- **No MusicIP binary was ever disassembled, decompiled or inspected as code**, and no proprietary MusicIP file format is read by anything here. The acoustic descriptors Attune analyzes audio with (MFCC, chroma, spectral features, tempo) are standard published technique and owe MusicIP nothing.
- **Some of Attune's behaviour was measured off the running program, and that should be said rather than implied.** MusicIP was run on the author's own library through the local interface described below, its outputs were recorded, and its knobs were swept to work out what they actually did. Three results of that came back into this repository. Two are in the engine that ships on by default (`src/hybrid.py`): the variety control turned out not to be a diversity rule at all but an independent coin flip per candidate at `p = 1/(1+variety)`, and loudness was picked as the energy axis because measurement showed MusicIP holds that axis strongly. The third is the optional learned engine described below. Watching a program you own do its job, on your own files, and writing down what it does, is the ordinary and permitted way to learn from software; it is not the same thing as copying it, and this project would rather name it than let someone find it in a source comment.
- **Two MusicIP data files were examined as bytes during the research, and that should be said too.** MusicIP's library file, and the analysis blobs it had written into the tags of the author's own audio files, were looked at as raw bytes: the readable text inside them, the headers, and a statistical look at how the bytes were laid out. No layout was decoded, nothing from either file is in Attune or in this repository, nothing here reads them, and no MusicIP program file was disassembled or decompiled.
- **MusicIP's own agreement was read, and this is what it says on the subject.** The licence text that comes with MusicIP Mixer was found and read on 2026-09-21. Its single clause of this kind forbids reverse-engineering, decompiling, modifying or disassembling the *object code portions* of the program, except where law permits it regardless. No object code was touched here: the program was run, watched through its own local interface, and two data files it had written were looked at as bytes. That is set out so a reader can check the scope against the agreement themselves instead of taking the paragraphs above on trust. It is a description of what the agreement says, not a legal opinion about it.
- **The optional MusicIP-interop tools** in [`tools/`](tools/) talk to MusicIP's *own local HTTP API*, the `localhost:10002` interface documented for years by the Squeezebox and Logitech Media Server community. That is checkable rather than merely asserted: the endpoints are described on the [Lyrion/Squeezebox wiki](https://wiki.lyrion.org/index.php/Integrating_MusicIP_with_SqueezeCenter) and used by open-source plugins including [lms-mipmixer](https://github.com/CDrummond/lms-mipmixer). `tools/parse_musicip_library.py` reads the saved text response of that API's `/api/songs` endpoint. Nothing here opens a `.m3lib` or any other MusicIP file. Using a program's own network API to read your own library's data is ordinary interoperability; it does not copy or modify the program. These tools are for people who still run MusicIP and want to migrate their catalog or benchmark Attune against it. They are not required to use Attune.

**The optional "learned" engine is a distillation of MusicIP's similarity function: a model trained to reproduce MusicIP's rankings over the author's own library.** That is the project's own description of it in its evaluation notes, and it is the accurate one. Attune ships it as a second, optional engine, turned on only by setting the engine to `learned` (in the app's settings, or with `--engine learned` when running the server directly). Its model file is `src/models/metric_head.onnx`. The rankings it learned from were obtained through the local HTTP API described above, over the author's own audio. No MusicIP code, binary or data file was copied, decompiled or redistributed, and nothing derived from anyone else's library is in it. But the weights encode another program's judgments, which is a real thing to disclose rather than something to find in a source comment. The default setting is `auto`, which uses a running MusicIP if it finds one on your own machine and otherwise the V2 engine; neither of those touches this model.

> If you hold rights to MusicIP / MusicMagic / Predixis IP and have a concern, please open an issue. This project is built in good faith and will address problems promptly.

## 5. Your music library

- Attune only reads audio files **you already have on your own storage**. It does not download, upload, share or redistribute music.
- Whether you have the right to possess and analyze those files is **your responsibility**, governed by where you live and how you obtained them. Attune takes no position and stores nothing off your machine.
- Attune extracts **acoustic feature vectors** (numeric descriptors) and tag metadata into a local SQLite file. Those derived features are not the music and cannot reconstruct it.
- Attune reaches the network only where you have pointed it at something, and nothing leaves the machine by default. If you configure a Plex server, exporting a playlist sends track paths and playlist names to that server, because that is what exporting to it means. If you run MusicIP, Attune talks to it on your own machine.

## 6. Do not publish personal or derived data

When sharing forks, screenshots or bug reports, do **not** include:

- Your `data/` directory, `*.db` files, or `library.json`. They hold your library's file paths and metadata, which is personal information.
- Any **ground-truth capture** (`groundtruth/`). It is derived from running MusicIP over *your* library and reveals its contents. Keep it local.

`.gitignore` excludes all of the above by default.

## 7. Everything else inside the installed application

[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists every component that is actually inside the installed app, with its version, its license, which of the two programs it is in, and a link to its license text under [`licenses/`](licenses/). It is generated by [`tools/gen_third_party_notices.py`](tools/gen_third_party_notices.py), which reads the two frozen programs and the build environment rather than a hand-kept list, so it cannot quietly drift from what ships.

At the time of writing that is 48 Python packages, 14 native libraries and programs, and one port of third-party source into Attune's own files (section 2), across `Attune.exe` and `AttuneAnalyzer.exe`. The bundled FFmpeg is a static build with 64 further libraries compiled inside it; the generator reads that list out of the binary itself and prints it under FFmpeg's entry, and all of them are conveyed under FFmpeg's own GPL version 3. Most of the rest is MIT or BSD. The ones that carry conditions worth knowing about are:

| Component | License | Note |
|---|---|---|
| mutagen | GPL-2.0-or-later | See section 3. This is the reason the built app is GPL. |
| FFmpeg | GPL-3.0-or-later | See section 3. Separate program, unmodified gyan.dev build. |
| libsndfile | LGPL-2.1-or-later | DLL inside the soundfile wheel. Decodes wav, flac, ogg, opus. |
| libsoxr | LGPL-2.1-or-later | Compiled into the python-soxr extension. Resampling. |
| certifi | MPL-2.0 | The list of certificate authorities Python trusts. |
| PyInstaller | GPL-2.0-or-later with bootloader exception | The tool the app is frozen with. See section 3. |

The Microsoft redistributable runtimes in the bundle (the Visual C++ runtime, the OpenMP runtime, the WebView2 loader, and the .NET reference assemblies pythonnet carries) ship no licence file with the files themselves. The Visual C++ runtime, the OpenMP runtime and the WebView2 loader each have their own entry in THIRD_PARTY_NOTICES.md, with a link to Microsoft's page and a sentence saying why no text is shipped. The .NET reference assemblies do not have an entry of their own, because they arrive inside the pythonnet wheel rather than as a package the notices generator can see; they are accounted for in pythonnet's entry, which says plainly that the `System.*.dll` and `netstandard.dll` files beside `Python.Runtime.dll` are Microsoft's and not pythonnet's to license. They are Microsoft's copyright under Microsoft's terms; pythonnet's MIT licence covers pythonnet's own code, not the assemblies it redistributes.

Regenerate the notices after any change to what the build bundles:

```
python tools/gen_third_party_notices.py --app dist/Attune --venv ../mixer-ng/.venv-standalone
```

## 8. The models

**The audio embedding model is [`laion/larger_clap_music`](https://huggingface.co/laion/larger_clap_music), Apache-2.0** per its model card, checked 2026-09-20 and again 2026-09-22. Attune ships it as `src/models/clap_music.onnx`: the published checkpoint's audio tower and projection, exported to ONNX with `torch.onnx.export` in float32, with the per-clip normalization and the mean over three windows baked into the graph so the file's output is the finished track vector. **The weights are not retrained, fine-tuned, quantized or otherwise altered.** The export changes the file format and where the pooling happens, not the numbers. The exact export provenance is recorded in `src/models/clap_norm.json` under `_provenance`, and `clap_norm.json` also holds the feature constants used with it.

**The optional learned head is the author's own weights.** `src/models/metric_head.onnx` and `src/models/learned_norm.json` were produced on the author's machine over a pool of 17,020 of his own tracks, with MusicIP's rankings as the training target. The head carries the date 2026-07-09 in `src/engine.py`; the statistics file beside it is dated 2026-07-18. Section 4 says what that means. It is not used unless the engine is set to `learned`.

## 9. Patents

Attune's acoustic analysis uses long-established published methods (MFCC, chroma, spectral descriptors, tempo), and it does not implement MusicIP's acoustic fingerprinting.

Beyond that, this project makes no claim in either direction about anyone's patents. No patent search has been carried out, nobody here is qualified to carry one out, and a statement of non-infringement by a non-lawyer in a file like this would be worth nothing to a reader and nothing to the project. Attune is offered with no patent warranty of any kind: see the "AS IS" clause in the MIT licence and the warranty and patent sections of the GPL, which are the actual terms you have.

An earlier version of this section asserted that Attune did not practise any MusicIP or Predixis patent. That assertion has been withdrawn, because it was not something this project had established.

## 10. Before the repository goes public, or before a release

- [x] **Real copyright holder set in [LICENSE](LICENSE).** Done: "Copyright (c) 2026 Joe Schmidt", matching `pyproject.toml` and the installer.
- [x] **MIT versus copyleft decided.** The source stays MIT; the built application goes out under GPL-3.0-or-later for the reasons in section 3. This is a settled position, not an open question.
- [x] `git status` shows **no** `data/`, `*.db`, `*.m3lib`, `library.json`, `groundtruth/` or MusicIP binaries staged. **Checked 2026-09-21 on `release/firstrun-2026-09-20`: zero matches.**
- [x] Nothing personal is tracked. **Checked 2026-09-21: the command below returns nothing.** A wider sweep that also looked for the author's name, his server names and address ranges returned only the Plex feature's own module filenames, which are code, not data.

      git ls-files | grep -Ei 'm3lib|\.db$|library\.json|groundtruth/|register\.key'

      This should return nothing. `tools/capture_groundtruth.py` is a script, not a capture, and the trailing slash above is what keeps it out of the results.

- [x] **Working tree is clean of personal strings. Checked 2026-09-21 at full strength** (5 literals loaded from `.leakpatterns`, 243 files in scope, `[TREE] clean.`, exit 0). Two files were NOT read because they are binary and that is not the same as clean: `desktop/installer/attune.ico` and `src/models/metric_head.onnx`. Note `.leakpatterns` is gitignored, so it is absent inside a worktree and must be passed with `--patterns` pointing at the main checkout, or the scan silently runs at half strength and still exits 0.
- [ ] **The original instruction, kept:** `python tools/leak_check.py --patterns <path to .leakpatterns> --require-patterns`. It must report clean. `--require-patterns` matters: without the machine-specific literals the scan runs the generic rules alone, still says OK, and has found nothing because it was looking for nothing. The flag turns that into a refusal. CI runs without it on purpose, since the literals file never reaches a CI checkout.
- [ ] **History is NOT clean. Measured 2026-09-21: 101 potential leaks**, across the working history of this branch, plus one hit in a commit message. This is Joe's decision and it does not block a draft release; it blocks making the repository public. **History is clean too**, not just the current files. A string removed from the latest version still sits in old commits: `python tools/leak_check.py --all`, which runs the tree, the full history and the commit authors in one pass. The narrow manual equivalent is `git log -p -S '<email-or-token>' --all`.
- [ ] If history is not clean, it must be rewritten before publishing. This bit the project once already: an email address that was clean at the tip and present in every old commit.
- [x] **The notices match the build that is actually shipping. Ticked 2026-09-21 by the session that built it.** Both programs were rebuilt from the branch at `1f4c32e` (full build, GUI and analyzer, 20:39 to 20:49), and the generator in section 7 was then run under the build environment's own Python 3.12 against that build: exit 0, 48 Python packages, **14** native components, 1 ported source, 6 hand-placed texts present, 0 unattributed names, 0 unattributed binaries. The file it wrote and the `licenses/` folder it filled were byte-identical to what was already committed, so nothing in the notices moved between the previous build and this one. Counted a second way, bounded by section heading in Python rather than by an open-ended range: 14 under "Native libraries and programs", 1 under "ported", 0 under "could not account for". The tool now refuses to run under any Python whose major.minor differs from the one recorded in each frozen program's own PyInstaller cookie (observed the same evening: under 3.14 it printed REFUSING, exited 2, and wrote nothing). The earlier history of this line, kept for the record: the notices on disk had been internally consistent with the file but not with section 7, which said 15 native entries because the Intel oneTBB row had been removed from the generator (the component is not in any build made from the recorded build environment) and the prose was not updated with it; and an earlier count that "confirmed" 15 had used an `awk` range that ran to the end of the file and swept in a heading from the next section.
- [x] **The license texts actually reach the installed app. Ticked 2026-09-21, observed twice against the real installer** (not a stand-in build): a silent install into a scratch folder on the maintainer's PC, and a silent install of the same file, hash checked, inside Windows Sandbox on a machine with nothing of the project on it. Both times the folder beside `Attune.exe` held `licenses` (120 files), `THIRD_PARTY_NOTICES.md`, `NOTICE.md` and `LICENSE.txt`. The portable zip was opened the same day and holds the same four under its single `Attune` folder, which it did not before 2026-09-21. Both uninstalls removed the program folder and left `%APPDATA%\Attune` untouched. The original instruction, kept: install to a scratch folder and confirm a `licenses` folder and `THIRD_PARTY_NOTICES.md` are sitting next to `Attune.exe`. Section 3 says they are; this is the check that makes that sentence true rather than intended.

---

**How this was checked, 2026-09-20.** The component list, the versions and the GPL facts were read out of the built application at `dist/Attune` and out of the build environment, not from any earlier document. The frozen archives inside both `.exe` files were enumerated by the generator. FFmpeg's version and configure flags are read as plain strings inside `ffmpeg.exe` itself, on every run of the generator, so they cannot be stale while the binary is the one described. The CLAP model's licence was read from its Hugging Face model card on the same day, over the network; that one claim is not checkable from this disk alone. It was read again over the network on 2026-09-22 and still said Apache-2.0. The learned head's origin was read from `src/models/learned_norm.json` and `src/engine.py`. The mp3 decoding figures in section 3 come from this project's own measurement over 300 random mp3 files and one folder of 4,007, recorded in the release campaign's bundle-diet report. Two cold reviewers, one of them ChatGPT's Codex, read this file against the built app before it was committed, and the false statements they found were corrected. If this file and the shipping build ever disagree, the build is right and this file is stale; re-run the generator and fix it.
