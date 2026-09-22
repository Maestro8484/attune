# Attune: where it came from

> **DRAFT, written 2026-09-21, NOT YET REVIEWED. Do not publish anything from this file until that review has passed.** It makes claims about a dead company's product, about what was and was not done to it, and about a listening test with five tracks in it. Every one of those is the kind of sentence that is hard to take back. The review that clears it is `release-campaign/PASTE_LEGAL_PROMOTION_REVIEW.md`, and the standing rulings it has to satisfy are in `NOTICE.md` sections 2, 4 and 9.

This is the origin story, plus the facts behind it. It's here so that anything written about Attune later - a release note, a forum post, an interview answer - comes off the same set of true things instead of being made up fresh each time.

Everything with a number in it was measured. Where a number is shaky, this file says so.

---

## The short version

I've spent years using a dead program. MusicIP Mixer stopped being developed around 2010, its servers went off, and nobody open-sourced it. It did one thing nothing else does properly: you point at a song, and it hands you a playlist of songs that actually sound like it. Not "people who liked this also liked", not genre tags. The sound.

The thing that finally pushed me into building something was not nostalgia. It was the six steps it took to get a mix into my car. MusicIP only talks to a version of Winamp from the 2000s, so it was: make the mix, save an m3u, import that into MusicBee, make a MusicBee playlist, export it to a folder, copy the folder to a USB stick. Every time.

So I built Attune. It analyzes your music once, then builds sounds-like playlists instantly, and gets them onto a device in one click.

Then the project taught me two things I didn't want to learn, both covered below.

---

## The long version

### First I had to find out what MusicIP was actually doing

The old program has a local web interface, which is how the Squeezebox plugins used to drive it. It isn't documented anywhere useful and it doesn't switch itself on. You have to start the program, open its preferences, tick a box, and press start, every single launch. Turn the program off and the interface dies with it.

With that running I captured what it actually does: 17,447 tracks in the library, and 540 mixes made by asking it the same questions with its own knobs in different positions. All of it through that interface, on my own machine, with my own music. Nothing was taken apart.

### What the famous "magic" turned out to be

Everyone, me included, assumed MusicIP built a playlist by walking - pick a track, then pick something close to *that* one, and so on, wandering across your library. That's the story people tell about it.

It doesn't do that. The test was simple: if it walks, then the mix starting from track two should match the tail of the mix starting from track one. Agreement came out at exactly zero, across every seed and every setting.

What it really does is much plainer. It ranks your whole library once, by one distance function. Then it walks down that single ranked list flipping a biased coin at each track, keeping the ones that come up heads, until the playlist is full. That's it.

The "variety" slider isn't diversity logic at all. It's the bias on the coin, and it's exactly one over one-plus-the-setting. Set variety to 1 and it keeps about half of what it passes. Set it to 9 and it keeps about a tenth, so it reaches ten times deeper into the same list. "Cross-genre journeys at high variety" are just reaching further down one ranking. The coin is reseeded about once a second, which is why the same request twice in a row gives you different playlists.

I got that wrong first, published the wrong answer to myself, and had to overturn it. Also got the range of the "style" dial wrong: it goes to roughly 845, not 100, so my whole first sweep covered about an eighth of it.

### The two things I didn't want to learn

**One: the obvious upgrade made it worse.** The plan was to swap in a bigger, newer, smarter audio model. It scored best on the similarity measure we'd been using, better than anything else tried. Then I sat down and listened to five sets of playlists blind. It came dead last. Not close - last on four of the five.

So the measure we'd been steering by didn't predict what sounded good. It rewarded more-of-the-same, which is the opposite of what a good mix does. That killed the whole "throw a bigger model at it" direction.

**Two: the plan to just wrap MusicIP got thrown out.** The original plan, written down and everything, was that MusicIP would stay the engine underneath and Attune would be a modern shell on top. Then the same listening test put the simplest homemade engine - the one we'd already "moved past" - above the real MusicIP. So the shell became the product, and MusicIP became an option you can still switch on if you happen to own it.

That listening test is five tracks and one of its five rounds has a labelling note I never resolved. It's a pilot, not a study. I'd say the homemade engine holds its own against the original, and I wouldn't say more than that from five tracks.

---

## The facts, with numbers

Use these rather than inventing new ones.

- MusicIP Mixer shipped roughly 2005 to 2010. Predixis, later MusicIP Corporation. The company wound down, the online parts died, it was never open-sourced.
- The library it was measured against: 17,447 tracks, about 1,200 hours.
- 540 mixes captured from the running program, 54 seed songs times 10 knob settings.
- Same seed, same settings, variety at 0 versus 9: the two 25-track playlists share 6 tracks.
- The "does it walk" test: zero positional agreement, every seed, every setting.
- Rank order never breaks. Zero inversions across 160,243 ordered pairs.
- The variety coin: one over one-plus-the-setting, accurate to within one percent at every setting and at every depth down the list.
- The style dial runs to about 845, not the 0 to 100 that gets quoted. Past about 950 the only songs left are the seed's own artist.
- What MusicIP holds onto while it roams across genres: loudness, brightness and texture, strongly. Tempo is the weakest of the four, but it is held.
- Genre isn't a term in the engine at all. Songs of the same genre come out together anyway, two to seven times more often than chance, purely from how they sound.
- The blind listening test: five tracks, six engines, scored best-to-worst. Simplest homemade engine 25, its successor 22, real MusicIP 19, plain hand-built features 19, the raw audio model 11, the big new model 9.
- That big new model had the best similarity score of anything measured, 0.157, and came last by ear.
- Attune's own library today is about 21,000 tracks.
- Roughly one mp3 in fourteen in a real library can't be decoded without ffmpeg. Measured at 22 of a random 300.

---

## Lines that work

**One sentence:** Pick a song you love, get a playlist that sounds like it. Offline, on your own machine, for the music files you already own.

**The hook:** MusicIP Mixer died in 2010 and nothing replaced it. I took it apart the honest way, through its own interface, and found out its famous magic is a coin flip.

**The turn:** The bigger, newer audio model won on the numbers and came dead last by ear. So we stopped trusting the numbers.

**The reversal:** The plan was to keep MusicIP as the engine and build a nicer shell on top. Then the shell's own engine beat it in a blind listen, and the plan went in the bin.

**Why not Chromaprint, which everyone suggests:** that's for telling you two files are the same recording. The distance between two *different* songs under it means nothing, so you can't build a sounds-like tool on it. Different job, good tool for that job.

**Why it's free:** it stays free and open. Donations if anyone insists, nothing else. The origin story is worth more than a paid tier.

---

## Things that must never go in public copy

These aren't style preferences. Each one has a reason.

- **Don't say "clean-room".** It means something specific - one team studies the original and writes a spec, a second team who never saw it builds from the spec. That isn't what happened here. One person did both. `NOTICE.md` says the true thing instead.
- **Don't make any claim about patents in either direction.** No patent search has been done and nobody here is qualified to do one. An earlier draft asserted Attune didn't practise anyone's patent; that was withdrawn because it hadn't been established.
- **Don't say MusicIP's stored analysis was decoded.** It wasn't. The raw bytes were preserved and never characterized. "Harvested and kept" is true; "decoded" and "reverse-engineered" are not.
- **Don't state "Attune beats MusicIP" as settled.** Five tracks, one round with an unresolved labelling note. Say it holds its own, and say the sample size.
- **Don't quote the head-to-head precision benchmark** that seems to show Attune winning by a mile. It rewards returning more of the same artist and album, which is exactly what MusicIP deliberately avoids, so it's measuring the wrong thing. Ears decided this, not that number.
- **No machine names, no network share names, no personal file paths, no library names.** The research notes are full of them; none of it belongs in public copy.
- **Don't link or quote the private research folder.** It holds MusicIP's own program and its own licence keys, and it stays private.

---

## What's true today, in case this file goes stale

An earlier version of this material described Attune as a command-line tool with export "on the roadmap". That stopped being true. It's a Windows desktop app, it exports .m3u8 playlists, it copies to a device, and it pushes playlists to Plex.

Two things worth getting right. The default engine setting is "auto", which uses MusicIP if it finds it running and Attune's own engine otherwise. Almost nobody has MusicIP, so almost everybody gets Attune's engine. And the built application is under the GPL because of what's bundled inside it, while the source is MIT. `NOTICE.md` explains that properly.

Staleness check, since this file is a record of one moment:

```
git log -1 --oneline
git log -1 --oneline -- PROMOTION.md
```

Same commit both times means nothing has landed since this was last true.
