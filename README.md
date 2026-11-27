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
  The panel at the bottom right sets **Quality**, **Video as**, **Audio as**, **Save to** and **File name** for new downloads.
- **Monitor tab**: live progress, speed and ETA, including conversion progress. Each download shows
  its format, its folder, and whether your browser login was used.
- **Columns**: right-click any list header to show or hide columns (including thumbnails), drag
  headers to reorder them, and drag their edges to resize. Options › Columns does the same.

### Best quality
NooNoo always downloads the highest resolution available, up to your Quality setting. If a source
is VP9/AV1 (most 1440p/4K YouTube), the default **MP4 · H.264** output converts it on the Mac's
hardware encoder so it plays everywhere.

### Editing / Avid
**Video as** offers H.264, H.265, ProRes (422 Proxy, LT, 422 and HQ, for Premiere, Resolve and Final Cut)
and two families of Avid codecs. The editing formats are all .mov with 4:2:2 video at a constant frame rate
and 24-bit 48 kHz PCM audio:
- **DNxHD LB / SQ / HQ**: Avid's HD codec. Always 1920×1080 at a broadcast frame rate, using Avid's
  own bitrates (36 / 115 / 175 at 23.976, 45 / 145 / 220 at 29.97, and so on). Other sizes are scaled
  to fit, with black bars for vertical or square video.
- **DNxHR LB / SQ / HQ**: keeps the original resolution, for 4K or vertical projects.

Pick a **Frame rate** next to Quality to conform clips to your project. For audio, **WAV 48k/24-bit
(Avid)** is available. Turn on Options › Saving › *Plain ASCII file names* for the safest Avid media names.

### Part of a video
Type where the part starts and ends in **Part** (bottom right of Search), then press **Download** or
**Download Audio**: `30.5`, `0:30`, `1:02:03` or timecode like `00:00:30:12`. Leave *to* empty for "to the
end". NooNoo downloads only that stretch. Right-click a result › **Download Part...** takes you to the same
boxes. Like File name, a part is used for one download, then the boxes empty.

With *Fine-trim after* ticked, NooNoo downloads a couple of seconds either side and the part waits in the
**Edit** tab (a balloon, a notification and a badge on the tab say so) for you to pick the exact first and last
frames. Untick it and the part is cut at the nearest frames to the typed times.

Some sites won't send just part of a video. NooNoo then downloads the whole thing and cuts the part out itself.

### Edit
Every finished download is listed in the **Edit** tab, along with parts waiting to be trimmed. **Add File...**
brings in any other video or sound file from your Mac. The first time a file is opened, NooNoo measures every
frame and makes a small copy to play. That takes a few seconds, or longer for long 4K videos. Exports are
always made from the original file, which is never changed or moved.

| Key | Does |
| --- | --- |
| Space | Play / pause |
| J · K · L | Play backwards · stop · play forwards. Press J or L again for 2×, 4×, 8× |
| Hold K, tap J / L | One frame back / forward |
| ← / → | One frame (Shift: 10). Sound: 10 ms |
| I / O (or E / R) | Mark In / Mark Out, with a still of each frame |
| Q / W (or Home / End) | Go to In / Out |
| G | Clear the marks (the whole clip) |
| \\ | Play from In to Out |
| - / = | Zoom the timeline out / in (or scroll on it) |
| Enter | Export |

Type a timecode into the box under the picture to jump there. The Out frame is included, like Avid's Mark Out.

The panel at the bottom right sets what **Export** makes:
- **Export as**: H.264 or H.265 (HEVC) MP4; ProRes 422 Proxy, LT, 422 or HQ; DNxHD or DNxHR; or sound only
  (MP3, M4A, Opus, FLAC, WAV).
- **Size**: the short side of the picture (1080p, 720p...), so vertical video stays vertical.
- **Frame rate**: conforms to a project rate (frames repeated or dropped, not retimed).
- **Fit into ... MB**: aims for a file size, for email, Discord or WhatsApp. NooNoo measures the video in a
  first pass, then shares the bits out in a second, and tries again a little smaller if it still comes out
  over. Under the marks it shows the bitrate that size buys, and suggests a smaller picture if it would look
  blocky. Works with H.264, H.265, MP3, M4A and Opus. The other formats have a fixed quality.
- **Save to** and **File name**: the name is filled in for you with the source's name, the timecode it covers
  such as `[00.01.00.00-00.01.09.24]`, and what changed, such as `(720p, 25 MB)`.

Exports run in **Monitor** like downloads, and the finished file appears in the Edit list.

### Folders
- **Save to** (bottom right of Search) is where new downloads go. Click **Browse...**, or type or paste
  a path into the box (`~/Movies`, Finder's *Copy as Pathname*, or a path copied from Terminal all work).
  A folder that doesn't exist yet is created when a download lands.
- Right-click a result › **Download to Folder...** for a one-off location.
- Options › Saving › *Sort into subfolders by* can file downloads by site, uploader, playlist or
  download date automatically.

### File names
Select a result and its title appears in **File name** (bottom right of Search), ready to edit or replace.
NooNoo adds the extension. Select several and type one name to number them: `Interview 01`, `Interview 02`.
Leave the box alone and Options › Saving › *File name pattern* decides. A name is used once, for that download.

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
the release on GitHub. The first time, it asks for the signing key's passphrase and offers to save it in your
Keychain (as "NooNoo update signing key"), so later releases don't ask. The key file stays encrypted. Anything
running as you can read a saved passphrase through macOS's `security` tool, just as it can your other saved
items. To go back to typing it: `security delete-generic-password -s "NooNoo update signing key"`. Release notes come from the commit messages since
the last release; to write your own, pass a notes file: `./release.sh 1.0.3 notes.md`. The script refuses to run with uncommitted changes.

### Extra yt-dlp arguments
Options › Advanced accepts a fixed list of harmless options (`EXTRA_ARGS` in `noonoo/settings.py`).
Options that can run programs, load code or read and write arbitrary files, like `--exec`, are refused.

### When a site stops working
Sites change all the time, and yt-dlp releases fixes often. NooNoo installs each new yt-dlp release
by itself (Options › Behaviour can turn this off), so you shouldn't need **Tools › Update Download Engine**.

YouTube sometimes turns a connection away for a while ("Sign in to confirm you're not a bot", or
HTTP 403). That isn't the engine being out of date: NooNoo tries again by itself, and if it still fails,
pressing Retry a few minutes later usually works. Lending NooNoo your browser login for YouTube
(Options › Accounts) makes it rarer.

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
  editor.py           the Edit tab's list, frame measurements and playing copies
  updater.py          update check, download + verify, install-and-relaunch helper
  server.py           localhost HTTP API (token + host checked), static UI, media streaming
  engine.py           download queue, yt-dlp process control, progress parsing, search/probe, exports
  tools.py            fetches/updates yt-dlp, ffmpeg, ffprobe, deno
  verify.py           proves updates and tools came from their publishers
  settings.py         options + defaults (~/Library/Application Support/NooNoo/settings.json)
  web/                the Windows XP-style UI (HTML, CSS, JS, SVG icons, no build step)
```
