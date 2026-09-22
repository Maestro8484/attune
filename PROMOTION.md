# Attune: where it came from

This is the origin story, plus the facts behind it. It's here so that anything written about Attune later -- a release note, a forum post, an interview answer -- comes off the same set of true things instead of being made up fresh each time.

Everything with a number in it was measured, or its source is named beside it. Where a number is shaky, this file says so.

---

## The short version

Attune is a modern retrofit of the programs that made a PC the center of a music collection. Winamp, MusicMatch Jukebox, MediaMonkey, iTunes, MoodLogic, MusicIP Mixer: the ones that ripped the CDs, burned them back, filled a playlist for you, and in one case could hear that two songs sounded alike. Some of them are still around. Three died with their companies (MusicMatch in 2007, MoodLogic and MusicIP in 2008), none were open-sourced, and the best idea of the lot went to the grave with them.

Attune takes that idea, a playlist built from the sound of one song, and brings it into the present. It's a desktop app that runs on your own machine, analyzes your files once, builds sounds-like playlists instantly, pushes them to Plex, and copies them to a USB stick for the car. No account. No server. Nothing that can go dark.

The idea I wanted back was MusicIP Mixer's. You point at a song and it hands you a playlist of songs that actually sound like it. Not "people who liked this also liked", not genre tags. The sound. I used it for years after the company behind it dissolved in 2008, because nothing else did the job. Apple's Genius did something close inside iTunes, but it worked by comparing your library against everyone else's on Apple's servers, and it only worked while Apple felt like running them.

That's the other half of why Attune is built the way it is. Everything's a subscription now, and a subscription is a rental. If you don't have possession of a thing, digital or otherwise, you don't own it. Your music files are on your own disk. So the tool that understands them should be too.

The thing that finally pushed me into building was not nostalgia. It was the six steps it took to get a MusicIP mix into my car. It was built to hand mixes to the players of the 2000s (Winamp, iTunes, Windows Media Player), so on my machine it was: make the mix, save an m3u, import that into MusicBee, make a MusicBee playlist, export it to a folder, copy the folder to a USB stick. Every time.

So I built Attune. The playlists it writes go into a folder on the network drive right next to the music, which is where my original Logitech Squeezebox reads them from through Lyrion Music Server. No path in the playlist ever needs editing. Then the project taught me two things I didn't want to learn, both covered below.

---

## The lineage, and where the stories are

The field these programs belong to is called music information retrieval, and the specific job is content-based music similarity: deciding two songs are alike from the audio itself rather than from what other people played. If you want to read the history, these are the accounts worth your time. Dates and facts below were checked against the linked pages on 2026-09-21.

| Program | Years | What it did that Attune carries forward | Where to read about it |
|---|---|---|---|
| MusicMatch Jukebox | 1997 to 2007 | Rip, tag, burn, Auto DJ. The PC as the hub of a collection. Bundled with the iPod on Windows until iTunes for Windows came out in 2003. Yahoo bought it in 2004 and shut it on 31 August 2007. | [Wikipedia](https://en.wikipedia.org/wiki/Musicmatch_Jukebox), [Slashdot on the Yahoo downgrade](https://tech.slashdot.org/story/07/07/09/0124242/yahoo-downgrades-musicmatch-jukebox) |
| MoodLogic | 2001 to 2008 | Playlists by mood and tempo from a central database of song profiles, plus tag fixing. Bought by All Media Guide in 2006; Macrovision switched it off on 3 March 2008. | [Wikipedia](https://en.wikipedia.org/wiki/MoodLogic), [a 2004 review](http://eworldui.net/blog/post/2004/06/29/Review-MoodLogic.aspx) |
| MusicMagic Mixer, later MusicIP Mixer | about 2004 to 2008 | The one Attune descends from. Analyzed the audio itself and built a "Power Mix" from one song, cross-genre, no tags needed. Predixis, renamed MusicIP in 2006, dissolved in 2008. | [Duke Listens on Predixis](http://static.echonest.com/DukeListens/predixis_musicmagic_mixer.html) and [round two](http://dukelistens.playlistmachinery.com/DukeListens/predixis_round_2.html), [a Winamp-forum comparison against Gracenote's generator](https://forums.winamp.com/forum/winamp/winamp-discussion/257488-short-review-gracenotes-playlist-generator-vs-predixis-music-magic-musicip), [a 2019 look back](https://oldmanmetal.com/2019/04/15/musicip-mixer-playlist-generator-extraordinaire/), [the Squeezebox forum the day it died](https://forums.slimdevices.com/forum/user-forums/3rd-party-software/72677-music-ip-gone-now-what), [where the company went](https://en.wikipedia.org/wiki/AmpliFIND) |
| Pandora's Music Genome Project | 2000 onward | The other road: humans scoring each song on hundreds of attributes, 20 to 30 minutes a track. Proof that "sounds like" was worth that much effort. | [Blogccasion, 2006](https://blog.tomayac.com/2006/03/13/pandora-and-the-music-genome-project-153643/), [Forbes, 2019](https://www.forbes.com/sites/insights-teradata/2019/10/01/how-pandora-knows-what-you-want-to-hear-next/) |
| iTunes Genius | 2008 onward | One button, one song, a playlist. Built from everyone's libraries on Apple's servers rather than from the audio. | [MIT Technology Review, 2010](https://www.technologyreview.com/2010/06/02/91325/how-itunes-genius-really-works/), [Apple's own terms](https://www.apple.com/legal/internet-services/itunes/us/genius.html) |
| Squeezebox and Lyrion Music Server (once Logitech Media Server) | 2000s onward | The community that kept MusicIP alive after the company, by driving it through its local interface. Still the best documentation of that interface, and still the player Attune's playlists go to in my house. | [Lyrion wiki](https://wiki.lyrion.org/index.php/Integrating_MusicIP_with_SqueezeCenter), [lms-mipmixer](https://github.com/CDrummond/lms-mipmixer) |

Basically, three roads were tried between 1997 and 2008: humans tagging everything (Pandora, MoodLogic), everyone's libraries compared on a server (Genius), and the audio itself, analyzed on your own machine (MusicIP). Attune takes the third road, because it's the only one that still works when the company is gone.

---

## The long version

### First I had to find out what MusicIP was actually doing

The old program has a local web interface, which is how the Squeezebox plugins used to drive it. It's documented mostly by the Squeezebox community, and it's off until you switch it on in the program's preferences. Turn the program off and the interface dies with it.

With that running I captured what it actually does: 17,447 tracks in the library, and 540 mixes made by asking it the same questions with its own sliders in different positions. All of it through that interface, on my own machine, with my own music. The program was never taken apart: no disassembly, no decompiling. Two of its data files were looked at as bytes, its library file and the analysis it writes into a track's own tags; nothing was decoded from them and nothing in Attune reads them. The list at the bottom says how to talk about that.

### What the famous "magic" turned out to be

I assumed MusicIP built a playlist by walking -- pick a track, then pick something close to *that* one, and so on, wandering across your library. That's how I'd always heard it described.

It doesn't do that. The test was simple: if it walks, then the mix starting from track two should match the tail of the mix starting from track one. Agreement came out at zero, or within a rounding error of it, across five starting songs and four variety settings.

What it really does is much plainer. It ranks your whole library once, by one measure of how alike two songs are. Then it walks down that single ranked list flipping a biased coin at each track, keeping the ones that come up heads, until the playlist is full. That's it.

The "variety" slider isn't diversity logic at all. It's the bias on the coin, and it's exactly one over one-plus-the-setting. Set variety to 1 and it keeps about half of what it passes. Set it to 9 and it keeps about a tenth, so it reaches about ten times deeper into the same list than it does with variety off. "Cross-genre journeys at high variety" are just reaching further down one ranking. Also, the coin is reseeded about once a second, which is why the same request twice in a row gives you different playlists.

Basically, all of MusicIP is one way of measuring how alike two songs are, plus five lines of logic around it. I got that wrong first, published the wrong answer to myself, and had to overturn it. I also got the range of the "style" dial wrong: it goes to roughly 845, not 100, so my whole first sweep covered about an eighth of it.

### The two things I didn't want to learn

One: the obvious upgrade made it worse. The plan was to swap in a bigger, newer, smarter listening model (the part that turns a song into numbers). It scored best on the number we'd been using to judge similarity, better than anything else tried. Then I sat down and listened to five sets of playlists. It came last overall, and never higher than fifth of six in any round.

So the number we'd been steering by didn't predict what sounded good. It rewarded more-of-the-same, which is the opposite of what a good mix does. That killed the whole "throw a bigger model at it" direction.

Two: the plan to just wrap MusicIP got thrown out. The original plan, written down and everything, was that MusicIP would do the picking underneath and Attune would be a modern face on top. Then the same listening test put my simplest homemade song-matcher -- the one we'd already "moved past" -- above the real MusicIP. So the face became the product, and MusicIP became an option you can still switch on if you happen to own it.

That listening test is five songs, and it was only partly blind: the three outside contenders, MusicIP among them, were shuffled and unlabelled, while my own were labelled as mine. One of its five rounds has a labelling note I never resolved. So here's what it does and doesn't prove. In five songs, my own song-matcher scored higher than the real MusicIP. Five songs isn't enough to call that a win, so I don't. It's enough that I stopped needing MusicIP.

---

## The facts, with numbers

Use these rather than inventing new ones.

- MusicMagic Mixer came from Predixis, founded 2000 and renamed MusicIP Corporation in 2006. The company dissolved in 2008; its fingerprint service went on to AmpliFIND and then to Sony's Gracenote in 2011. The Mixer itself was never open-sourced. (Company dates from Wikipedia's AmpliFIND entry, read 2026-09-21.)
- Attune runs without MusicIP. If you happen to have MusicIP running, Attune can use it to pick the songs instead of its own matcher.
- The library MusicIP was measured against: 17,447 tracks, about 1,200 hours.
- 540 mixes captured from the running program, 54 starting songs times 10 slider settings.
- Same starting song, same settings, variety at 0 versus 9: the two 25-track playlists share 6 tracks. One song, style at the setting it comes with.
- The "does it walk" test: no match at all in 18 of the 20 tries and next to none (0.021) in the other two, over five starting songs and four variety settings. If it walked, the match would be near perfect.
- Rank order never breaks. Rerun on 2026-09-21 against the live program with the output kept: in 37,803,531 pairs of songs checked, the order never once flipped. Five variety settings, one starting song.
- The variety coin: one over one-plus-the-setting, accurate to within one percent at each of the four settings tested (1, 3, 6 and 9) and at every depth down the list.
- The style dial runs to about 845, not the 0 to 100 we had assumed. Past about 950 the only songs left are the starting song's own artist, measured on one song.
- What MusicIP holds onto while it roams across genres: loudness, brightness and texture, strongly. Tempo is the weakest of everything measured, but it is held.
- With its genre filter off, songs of the same genre still come out together two to seven times more often than chance, purely from how they sound.
- The listening test, partly blind as described above: five songs, six contenders, scored best to worst. My simplest song-matcher 25, its successor 22, the real MusicIP 19, plain hand-built measurements of the sound 19, the listening model on its own 11, the big new model 9.
- That big new model had the best similarity number of anything measured, 0.157, and came last by ear.
- Attune's own library today is about 21,000 tracks.
- Roughly one mp3 in fourteen in a real library can't be decoded without ffmpeg. Measured at 22 of a random 300.
- Playlists written to the folder where the music lives play as-is on an original Logitech Squeezebox through Lyrion Music Server, because every path in them already points at the right place. That's my own setup, running today.

---

## Lines that work

One sentence: Pick a song you love, get a playlist that sounds like it. Offline, on your own machine, for the music files you already own.

The lineage: Winamp, MusicMatch, MediaMonkey, iTunes, MusicIP: the programs that made the PC the center of a music collection. The best idea of that era, a playlist from the sound of one song, died with MusicIP in 2008. Attune brings it back and adds the two things a collection needs today: a push to Plex and a copy to the USB stick in the car.

The hook: MusicIP shut its doors in 2008 and nothing replaced its Mixer. I worked it out the honest way, through its own interface, and found out its famous magic is a coin flip.

The turn: The bigger, newer listening model won on the numbers and came dead last by ear. So we stopped trusting the numbers.

The reversal: The plan was to keep MusicIP doing the picking and build a nicer face on top. Then my own song-matcher scored higher than MusicIP in a five-song listen, and the plan went in the bin.

Why local-first: A subscription is a rental. If you don't have possession of a thing, digital or otherwise, you don't own it. Your music is on your own disk; the tool that understands it should be too, with no account and no server that can go away.

Why not Chromaprint, which everyone suggests: that's for telling you two files are the same recording. The distance between two *different* songs under it means nothing, so you can't build a sounds-like tool on it. Different job, good tool for that job.

Why it's free: it stays free and open. Donations if anyone insists, nothing else. The origin story is worth more than a paid tier.

---

## Copy-paste write-ups

Same shape as the ChronoBloom launch kit: one block per destination, links filled in when the repository is public. Nothing in these says more than the facts above.

### Hackaday.io project page

Project name: Attune

One-liner field: Pick a song, get a playlist that sounds like it. Offline, on your own files, on your own PC. A modern successor to MusicIP Mixer.

Description:

Attune is a Windows desktop app that analyzes your music collection once, then builds playlists from the sound of one starting song. Not from tags, not from what other people played. From the audio.

The lineage matters. Between about 1997 and 2008 a run of PC programs made the computer the center of a music collection: MusicMatch Jukebox ripped and burned, MoodLogic profiled by mood, and Predixis's MusicMagic Mixer (later MusicIP) analyzed the audio itself and built cross-genre mixes from one song. All three are dead. None were open-sourced. I kept using MusicIP for years after its company dissolved, because nothing replaced it. Attune is that idea rebuilt for now.

What it does:

- Analyzes each track once with a music-trained listening model plus plain measurements of the sound (timbre, tempo, loudness, brightness). Everything is stored in one local database file you can open.
- Builds a sounds-like playlist from any starting song instantly, with Similarity and Variety sliders.
- Exports .m3u8 playlists, copies them with their files to a folder or a USB stick for a car stereo, and pushes them straight to a Plex server.
- Written into the folder where the music lives, the playlists play as-is on a Logitech Squeezebox through Lyrion Music Server. That's how mine get to the living room.
- Runs entirely on your machine. No account, no server, nothing leaves the PC unless you point it at your own Plex.
- If you still run MusicIP, Attune can use it to pick the songs instead of its own matcher.

What I learned measuring MusicIP through its own local interface, without touching its code: its famous "variety" slider is a biased coin flip down one ranked list, with the bias at exactly one over one-plus-the-setting. And the bigger, newer listening model I planned to upgrade to won on the similarity numbers and came last by ear in a five-song listening test. So the numbers stopped deciding what goes in. Ears do.

Source is MIT. The installer is GPL because of what's bundled inside it (a tag library and FFmpeg). Details in the NOTICE file in the repository.

Links:

- Source and docs: [TODO GitHub link]
- Installer: [TODO release link]
- Demo video: [TODO YouTube link]

### Reddit post (r/selfhosted, r/musichoarder, r/Plex)

Post after the repository and the installer are public. Fill the links.

Title: Built an offline "sounds like this song" playlist maker for my own music files, with Plex push and USB export. Open source. Modern successor to MusicIP Mixer.

Body:

If you ever used MusicIP Mixer (or MusicMagic before it), you know the idea: pick one song, get a playlist of songs that actually sound like it, across genres, from the audio itself. The company died in 2008. I kept using the dead program for years because nothing else did the job. So I built a replacement.

Attune is a Windows desktop app. It analyzes your library once (a music-trained listening model plus plain measurements of the sound, all stored in one local database file, SQLite), then builds a playlist from any starting song instantly. Similarity and Variety sliders, like the original.

Stuff this sub might care about:

- Fully local. No account, no cloud, nothing leaves your machine unless you point it at your own Plex server.
- Exports .m3u8, copies playlist plus files to a folder or a USB stick for the car, and creates the playlist on Plex directly.
- Playlists written next to the music on the NAS play as-is on my original Logitech Squeezebox through Lyrion Music Server. No path rewriting.
- The listening model runs on the CPU (ONNX Runtime). No CUDA, no torch in the installed app.
- About one mp3 in fourteen in a real collection won't decode without FFmpeg, so it's bundled. That's why the installer is GPL while the source is MIT.
- If you still have MusicIP running, Attune detects it and can use it to pick the songs. Otherwise you get Attune's own, which scored higher than MusicIP in a five-song listen. Too few songs to call it a win, enough that I stopped needing MusicIP.

One thing I found out on the way, by measuring the old program through its own local interface: MusicIP's variety slider was never "diversity logic". It ranks your whole library once and flips a biased coin down the list. The bias is exactly one over one-plus-the-setting. Twenty years of forum lore about "journeys" was a coin flip.

GitHub: [TODO repo link]
Installer: [TODO release link]
Demo: [TODO YouTube link]

Happy to answer questions about the analysis, the Plex push, or what MusicIP actually did under the hood.

---

## Things that must never go in public copy

These aren't style preferences. Each one has a reason.

- Don't say "clean-room". It means something specific: one team studies the original and writes a spec, a second team who never saw it builds from the spec. That isn't what happened here. One person did both. `NOTICE.md` says the true thing instead.
- Don't make any claim about patents in either direction. No patent search has been done and nobody here is qualified to do one. An earlier draft asserted Attune didn't practise anyone's patent; that was withdrawn because it hadn't been established.
- Don't say MusicIP's stored analysis was decoded. It wasn't. The analysis blobs were harvested out of the tags of my own files and kept, their byte layout was studied, one partial reading gave a weak signal and was dropped, and nothing in Attune reads them. "Harvested, looked at, never decoded" is true; "decoded" and "reverse-engineered" are not. The same goes for the program's library file: looked at as bytes, never decoded, not read by anything here.
- Don't state "Attune beats MusicIP" as settled. Five songs, only partly blind, one round with an unresolved labelling note. Say it scored higher in a five-song listen, and say that five songs isn't enough to call it a win.
- Don't quote the head-to-head precision benchmark that seems to show Attune winning by a mile. It rewards returning more of the same artist and album, which is exactly what MusicIP deliberately avoids, so it's measuring the wrong thing. Ears decided this, not that number.
- No machine names, no network share names, no personal file paths, no library names. The research notes are full of them; none of it belongs in public copy. "The NAS" and "the network drive" are fine; its name is not.
- Don't link or quote the private research folder. It holds MusicIP's own program and its own licence keys, and it stays private.

---

## What's true today, in case this file goes stale

It's a Windows desktop app. It exports .m3u8 playlists, copies them to a folder or device, and pushes them to Plex. An earlier version of this material called it a command-line tool with export "on the roadmap"; that stopped being true.

The built application is under the GPL because of what's bundled inside it, while the source is MIT. `NOTICE.md` explains that properly.

Reviewed against the research record on 2026-09-21: every number traced to its source, the wrong ones corrected, the rank-order and coin-flip checks rerun against the live program that day, and the lineage links opened and read.

Staleness check, since this file is a record of one moment:

```
git log -1 --oneline
git log -1 --oneline -- PROMOTION.md
```

Same commit both times means nothing has landed since this was last true.
