# Where the hand-placed licence texts came from

Most of the texts under `licenses/` are copied straight out of the package that ships them, by `tools/gen_third_party_notices.py`, every time it runs. The ones below are not: those components ship no licence text at all, inside the bundle or inside their wheel, so the text was fetched once by hand and committed. This file records the source and the date for each, because a licence text nobody can trace back is not much better than no licence text.

The generator itself never touches the network. It checks these files exist and fails if one has gone missing.

| Component folder | File | Taken from | When | Why by hand |
|---|---|---|---|---|
| `ffmpeg` | `COPYING.GPLv3` | https://www.gnu.org/licenses/gpl-3.0.txt | 2026-09-20 | The GPL version 3 text, for the bundled FFmpeg build. |
| `openssl` | `LICENSE.txt` | https://raw.githubusercontent.com/openssl/openssl/master/LICENSE.txt | 2026-09-20 | OpenSSL 3 is Apache-2.0; CPython's LICENSE.txt does not include it. |
| `flatbuffers` | `LICENSE` | https://raw.githubusercontent.com/google/flatbuffers/master/LICENSE | 2026-09-20 | The flatbuffers wheel declares Apache-2.0 but ships no text. |
| `proxy-tools` | `LICENSE.txt` | https://raw.githubusercontent.com/jtushman/proxy_tools/master/LICENSE.txt | 2026-09-20 | The proxy_tools wheel ships no text. Its metadata declares MIT while the file the project publishes is a 3-clause BSD text; both are permissive and what upstream publishes is what is shipped here. |
| `hugging-face-transformers-log-mel-front-end` | `LICENSE` | https://www.apache.org/licenses/LICENSE-2.0.txt | 2026-09-20 | Apache-2.0, for the port of Transformers code in src/embed_onnx.py. |
| `sqlite` | `PUBLIC-DOMAIN-NOTE.txt` | written by this project, recording https://www.sqlite.org/copyright.html | 2026-09-20 | SQLite has no licence to reproduce. This is a note saying so, not a licence. |

Everything else under `licenses/` is generated. Do not edit it by hand; re-run the generator instead.
