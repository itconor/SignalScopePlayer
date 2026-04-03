# SignalScope Player

Standalone desktop playback client for SignalScope logger recordings. Browse and play back archived audio without running a full SignalScope instance.

## Features

- Two connection modes: **Hub** (remote via API) or **Direct** (local/SMB recordings folder)
- 24-hour timeline with colour-coded 5-minute segment blocks (green = OK, amber = some silence, red = silent)
- Day bar overview with playback head and mark in/out indicators
- Metadata overlays: track, show, and mic-live bands
- Mark in/out with clip export (direct mode, requires ffmpeg)
- Auto-advances to next segment on playback completion
- Dark theme matching the SignalScope web UI
- Saves connection settings between sessions

## Requirements

- Python 3.10+
- PySide6
- ffmpeg (for export only)

## Install

```
pip install PySide6
```

## Run

```
python signalscope_player.py
```

A connection dialog opens with two tabs:

### Hub Mode

Connect to a SignalScope hub remotely. Enter:

- **Hub URL** — e.g. `https://hub.example.com`
- **API Token** — from SignalScope Settings > Mobile API

Uses the mobile API with Bearer token auth. Streams are loaded from the hub's merged catalog (all sites).

### Direct Mode

Open a recordings directory on a local or network drive. Click **Browse** and select the folder containing stream subdirectories (e.g. `S:\storage\logger_recordings` or `/media/storage/logger_recordings`).

The app reads `catalog.json` (written by the logger plugin) to discover streams. If no catalog exists, it falls back to listing subdirectories.

## Package as .exe

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "SignalScopePlayer" signalscope_player.py
```

The executable will be in `dist/SignalScopePlayer.exe`.

## How It Works

```
Hub mode:    App  -->  /api/mobile/logger/catalog   -->  stream list
                  -->  /api/mobile/logger/days       -->  date list
                  -->  /api/mobile/logger/segments   -->  segment grid
                  -->  /api/mobile/logger/stream_pcm -->  audio playback

Direct mode: App  -->  catalog.json                  -->  stream list
                  -->  {root}/{slug}/                -->  date directories
                  -->  {root}/{slug}/{date}/*.mp3    -->  segment files
                  -->  logger_index.db               -->  silence & metadata
```

## Settings

Connection details are saved to `~/.signalscope_player.json` and restored on next launch.

## Changelog

### 1.3.9
- Export: locate ffmpeg via `shutil.which` + common Homebrew/local paths so it works inside compiled bundles; show a clear install message if not found instead of a raw errno

### 1.3.8
- Silence ranges visualised on scrub bar as dark-red zones
- Red silence strip at the base of each segment block in the grid (proportional to silence %)
- Gap segments shown in dark blue (distinct from "no recording")
- Stereo streams marked `◈` in the stream list; STEREO badge shown in player bar
- `n_ch` read from `catalog.json` in direct mode for stereo detection

### 1.3.7
- SSL: global monkey-patch (`ssl._create_default_https_context`) fixes hub login in compiled macOS/Windows bundles where no system CA store is available

### 1.3.6
- Fix `_asset()` for PyInstaller `--onefile` packaging

### 1.3.5
- Generation counter, safe pending seek, hub relay fixes

### 1.3.4
- Fix `seek_s` dropped from `prepare_play` POST body

### 1.3.3
- Skip buttons and exact-time DayBar seeking

### 1.3.2
- Fix hub mode audio (serve OGG/MP3 directly)
