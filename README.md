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

### When a site stops working
Sites change all the time. **Tools › Update Download Engine** pulls the latest yt-dlp.

## Layout

```
app/                NooNoo.app source: NooNoo.swift (window shell), Info.plist, icon.svg
build_app.sh        builds + installs NooNoo.app
noonoo/
  __main__.py         starts the engine (--serve for the app, or opens your browser)
  spotify.py          Spotify links -> matching YouTube uploads
  server.py           localhost HTTP API (token + host checked), static UI, media streaming
  engine.py           download queue, yt-dlp process control, progress parsing, search/probe
  tools.py            fetches/updates yt-dlp, ffmpeg, ffprobe, deno
  settings.py         options + defaults (~/Library/Application Support/NooNoo/settings.json)
  web/                the Windows XP-style UI (HTML, CSS, JS, SVG icons, no build step)
```
