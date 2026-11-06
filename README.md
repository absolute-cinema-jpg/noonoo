# NooNoo

Your own video & audio downloader with a Windows XP-era look. No daily limits, no ads.

It's a front end for [yt-dlp](https://github.com/yt-dlp/yt-dlp), so it works with 1,800+ sites
(YouTube, X/Twitter, Instagram, Facebook, TikTok, Reddit, Vimeo, SoundCloud, Twitch, Bandcamp...).

## Install

Double-click **`Install NooNoo.command`**. It builds **NooNoo.app**, puts it in Applications and opens it.
After that, open NooNoo from Launchpad, Spotlight or the Dock like any other app. Run the installer
again after changing the code. (In a terminal, `./build_app.sh --open` does the same.)

The first launch downloads the engine (yt-dlp, ffmpeg, ffprobe, deno, about 120 MB) into
`~/Library/Application Support/NooNoo/bin`. Nothing is installed system-wide. macOS will ask once
whether NooNoo may use your Downloads folder.

Each tool is verified before it replaces anything, including when you update it: deno, ffmpeg and
ffprobe must carry their publisher's Apple Developer ID signature, and yt-dlp must match the checksums
yt-dlp signs with its PGP key. If yt-dlp ever changes that key, updates fail with a message and keep
the working copy until a new NooNoo release pins the new key.

The app is a small native window (Swift + WebKit). It runs NooNoo's Python engine, using the
built-in macOS `python3`, as a hidden helper. Prefer a browser? Run `python3 -m noonoo`. Engine
errors are written to `~/Library/Logs/NooNoo.log`.

## Using it

- **Search tab**: paste one or more links (or press ⌘V anywhere) or type keywords. Playlists and
  channels expand into one row per video. Filter by media type, source or uploader on the left.
  Double-click or **Download** saves video; **Download Audio** saves audio only.
  The panel at the bottom right sets **Quality**, **Video as**, **Audio as** and **Save to** for new downloads.
- **Monitor tab**: live progress, speed and ETA, including conversion progress. Each download shows
  its format, its folder, and whether your browser login was used.
- **Columns**: right-click any list header to show or hide columns (including thumbnails), drag
  headers to reorder them, and drag their edges to resize. Options › Columns does the same.
- **Library tab**: every download folder, with its subfolders. Play files in the built-in player,
  open them, reveal them in Finder, or move them to the Trash.

### Best quality
NooNoo always downloads the highest resolution available, up to your Quality setting. If a source
is VP9/AV1 (most 1440p/4K YouTube), the default **MP4 · H.264** output converts it on the Mac's
hardware encoder so it plays everywhere.

### Editing / Avid
**Video as** offers H.264 and two families of Avid codecs, all in .mov with 4:2:2 video at a constant
frame rate and 24-bit 48 kHz PCM audio:
- **DNxHD LB / SQ / HQ**: Avid's HD codec. Always 1920×1080 at a broadcast frame rate, using Avid's
  own bitrates (36 / 115 / 175 at 23.976, 45 / 145 / 220 at 29.97, and so on). Other sizes are scaled
  to fit, with black bars for vertical or square video.
- **DNxHR LB / SQ / HQ**: keeps the original resolution, for 4K or vertical projects.

Pick a **Frame rate** next to Quality to conform clips to your project. For audio, **WAV 48k/24-bit
(Avid)** is available. Turn on Options › Saving › *Plain ASCII file names* for the safest Avid media names.

### Folders
- **Save to** (bottom right of Search) switches the destination for new downloads. *Choose folder...*
  adds a folder to your favourites.
- Right-click a result › **Download to Folder...** for a one-off location.
- Options › Saving › *Sort into subfolders by* can file downloads by site, uploader, playlist or
  download date automatically.

### Spotify
Paste a Spotify song, album, playlist or artist link and each song shows up as its own row.
Spotify's audio is copy-protected, so NooNoo downloads the matching upload from YouTube and tags
the file with Spotify's title, artist and album. Now and then the match is a different version,
so check rows with an odd duration. Spotify podcasts aren't supported.

### Instagram, Facebook, X, private videos: logins and privacy
In **Options › Accounts**, choose the browser you're logged in with, tick the sites the login may be
used on, and optionally a separate browser profile. By default NooNoo tries every download logged
out first and only uses your login if the site refuses. Anything that used your login says so.
NooNoo never stores cookies and has no analytics or telemetry.

With Chrome, Brave, Edge, Opera or Vivaldi, macOS asks whether *security* (Apple's own Keychain
tool) may use "Chrome Safe Storage" (or similar). That item is the key the browser locks its cookies
with. It isn't your saved passwords. You type the password into macOS's window, and NooNoo never
sees it. **Allow** is the most private choice. **Always Allow** stops the question, but it also lets
any app that uses the `security` tool read that key.

### Updates
NooNoo checks for a newer version on launch and every few hours (Options › Behaviour; or
**NooNoo › Check for Updates…**). When one exists, a balloon pops up over the status bar, and
**Update Now** does the rest:
1. It downloads the build for that Mac (Apple Silicon or Intel) and checks its signature against the
   release key built into the app. An unsigned or wrongly signed build is refused, even from this repo.
2. It quits NooNoo, swaps in the new app (putting the old one back if anything goes wrong), and reopens it.
3. Downloads that were running carry on afterwards.

The check is one anonymous request for this repo's latest GitHub release (`FEED_REPO` in
`noonoo/updater.py`).

### Publishing a new version
Once, before your first signed release, create the signing key and commit its public half:
```bash
./make_update_key.sh
git add noonoo/update_key.pem && git commit -m "Add update signing key"
```
The private key stays in `~/.config/noonoo` behind a passphrase. Back up both somewhere safe, such as a
password manager: without them you can't sign updates, and everyone would have to reinstall by hand.

```bash
./release.sh 1.0.3
```
This bumps the version, builds and signs the Apple Silicon and Intel apps, tags and pushes, and publishes
the release on GitHub. Release notes come from the commit messages since
the last release; to write your own, pass a notes file: `./release.sh 1.0.3 notes.md`. The script refuses to run with uncommitted changes.

### Extra yt-dlp arguments
Options › Advanced accepts a fixed list of harmless options (`EXTRA_ARGS` in `noonoo/settings.py`).
Options that can run programs, load code or read and write arbitrary files, like `--exec`, are refused.

### When a site stops working
Sites change all the time. **Tools › Update Download Engine** pulls the latest yt-dlp.

## Layout

```
app/                NooNoo.app source: NooNoo.swift (window shell), Info.plist, icon.svg
build_app.sh        builds + installs NooNoo.app (--arch=arm64|x86_64|universal, --no-install, --zip)
release.sh          publishes a new version (see "Publishing a new version")
make_update_key.sh  creates the key that signs updates (run once)
tests/              security tests: python3 tests/test_security.py
noonoo/
  __main__.py         starts the engine (--serve for the app, or opens your browser)
  spotify.py          Spotify links -> matching YouTube uploads
  updater.py          update check, download + verify, install-and-relaunch helper
  server.py           localhost HTTP API (token + host checked), static UI, media streaming
  engine.py           download queue, yt-dlp process control, progress parsing, search/probe
  tools.py            fetches/updates yt-dlp, ffmpeg, ffprobe, deno
  verify.py           proves updates and tools came from their publishers
  settings.py         options + defaults (~/Library/Application Support/NooNoo/settings.json)
  web/                the Windows XP-style UI (HTML, CSS, JS, SVG icons, no build step)
```
