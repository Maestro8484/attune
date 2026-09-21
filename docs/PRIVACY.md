# What leaves your machine

Short answer: nothing, unless you set up Plex yourself.

Longer answer, with the file and line behind every claim so you can check rather than trust.
Everything below was read out of the source on 2026-09-21.

## There is no telemetry

No analytics, no crash reporting, no usage counting, no update check, no licence check, no
account, no sign-in. There is nothing in Attune that contacts the project, its author, or
any third party, at any time, for any reason.

Attune makes network calls in exactly four places in the shipped app, and they're all listed
below. You can confirm the list yourself:

```
grep -rn "urlopen\|urllib.request\|socket.create_connection" src/ web/ desktop/
```

## The four outbound calls in the app

**1. Your own Plex server.** `src/export.py:301-331` and `src/export.py:757-790`. The
address is whatever you typed into Preferences, under Plex, in the **Server address** field.
Nothing is sent there until you press **Test connection**, **Create Plex playlist**, or one
of the mirror buttons. Your Plex key goes in the request header, because that's how Plex
authenticates; it goes nowhere else.

**2. A MusicIP Mixer on this machine.** `src/musicip_engine.py:45`, default
`http://localhost:10002`. Only reached if you actually have that old program running. It's
the loopback address, meaning this computer talking to itself; nothing reaches the network.

**3 and 4. Two probes asking whether that MusicIP is running.** `web/app.py:2079-2081` and
`desktop/app_desktop.py:206-220`. Same loopback address, a quarter-second connect timeout,
and a `GET /api/version` if something answers. If nothing answers, Attune uses its own engine
and says no more about it.

That's the whole list. `requests` appears once as an import in `desktop/app_desktop.py:47`
and is marked `noqa: F401`, because it's there to make the packaging tool include the library,
not to make a call.

## One thing that opens your browser

The **Show me where, on Plex's site** button, beside the Key field, opens Plex's own page
explaining how to find your key. `web/app.py:1300-1320`. Attune doesn't fetch that page; it
hands the address to your normal browser and your browser goes. The address is fixed in the
source and cannot be supplied by anything else, and the route refuses any caller that isn't
this machine.

Once your browser is on plex.tv, Plex's own privacy policy applies to that visit, the same as
if you had typed the address yourself.

## Your Plex key

Stored in your settings file on this machine. It is never written to a log, never put in a
playlist file, never rendered into a page, and never returned by any of Attune's own
endpoints, including the one the Preferences window reads its values from. The routes that
could change where it's sent, or send it, refuse any caller that isn't this machine.

**Forget this server**, beside the Key field, clears the address, the key and the chosen
library together.

## Where your data sits

| What | Where |
|---|---|
| Settings, including the Plex key | `%APPDATA%\Attune\settings.json` |
| The analysed library | the database file named in Preferences, under Advanced |
| Logs | `%APPDATA%\Attune\logs\`, readable from Preferences, under Advanced |

`%APPDATA%` is `C:\Users\<you>\AppData\Roaming`. Uninstalling deliberately leaves that folder
alone so a reinstall doesn't cost you another overnight scan. Delete it by hand if you want
no trace.

The analysed library lists every music file path, album and track name on your machine. It's
ordinary personal data. Don't post it in a bug report.

## What the analysis stores

For each track: the file path, the tags already in the file, and two sets of numbers derived
from the audio. No audio is kept. The numbers aren't reversible into sound.

## One more, from source only

If you install from a source checkout, `tools/fetch_model.py` downloads the audio model from
this project's own GitHub release assets (`tools/fetch_model.py:46, 165-166`). That's a
deliberate command you type, once, and it isn't part of the installed app, which ships the
model inside it.

The optional `bridge/` and the research scripts in `eval/` and `tools/` are not part of the
app either. `bridge/bridge.py:35` talks to MusicIP, and `bridge/README.md` explains its own
trust model, which is different from the app's and worth reading before you run it.

## Attune serves a local web page, and that is worth understanding

Under the window, Attune is a small web server that your machine talks to. By default it
listens on `127.0.0.1` only, which nothing else on your network can reach.

It can be told to listen on your whole network instead, with `--host 0.0.0.0`, so you can
drive it from a phone. If you do that, understand what you have done: there's no password,
so anyone who can reach the port can browse your library and play it. The routes that could
leak or redirect your Plex key, or write files, refuse any caller that isn't this machine
even then. Don't expose it to the internet.
