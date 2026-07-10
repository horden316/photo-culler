# Photo Culler

Local-first photo culling app for HEIF/HIF + RAW workflows. Runs on macOS, Windows, and Linux.

## Download (recommended)

Grab the latest build for your platform from the [Releases](../../releases) page:

- **macOS**: `PhotoCuller-macos.zip` — unzip and drag `PhotoCuller.app` anywhere.
  The app is not notarized yet, so the first launch is blocked by Gatekeeper:
  right-click the app → **Open** → **Open** (needed only once).
- **Windows**: `PhotoCuller-windows.zip` — unzip and run `PhotoCuller\PhotoCuller.exe`.
  If SmartScreen warns, click **More info** → **Run anyway**.
  Requires the WebView2 runtime (preinstalled on Windows 10/11).
  If the app fails to start with a `Python.Runtime.dll` error, the extracted
  files are still marked as downloaded from the internet — right-click the zip
  → Properties → **Unblock** before extracting, or run
  `Get-ChildItem <app folder> -Recurse | Unblock-File` in PowerShell.
  (The app also clears this mark itself on startup, so this should be rare.)
- **Linux**: `PhotoCuller-linux.tar.gz` — extract and run `PhotoCuller/PhotoCuller`.
  Requires GTK and WebKitGTK, e.g. on Ubuntu/Debian:
  `sudo apt install gir1.2-webkit2-4.1`.

All builds bundle [ExifTool](https://exiftool.org/) for camera-specific metadata
(Fujifilm AF point, warnings, film simulation). Culling state is stored per-user
(macOS: `~/Library/Application Support/PhotoCuller`), so updating the app never
loses your marks — just replace it with the new version.

## Run from source (development)

Requirements: Python 3.11+ and the dependencies in `requirements.txt`:

```bash
python3 -m pip install -r requirements.txt
```

`exiftool` on the PATH is optional in dev mode (brand badges and AF-point
scoring degrade gracefully without it): `brew install exiftool` /
`sudo apt install libimage-exiftool-perl` / `choco install exiftool`.

Browser mode:

```bash
python3 app/server.py --library /path/to/photos
# open http://127.0.0.1:8765
```

Desktop window mode:

```bash
python3 app/desktop.py
```

## Building a release

Local build (current platform only):

```bash
python3 -m pip install pyinstaller
pyinstaller photo-culler.spec --noconfirm
# output in dist/ (dist/PhotoCuller.app on macOS)
```

Official releases are built by CI: push a tag like `v0.2.0` and
`.github/workflows/release.yml` builds and attaches macOS/Windows/Linux
artifacts to a GitHub Release. The workflow downloads a pinned, checksummed
ExifTool into `vendor/exiftool/` at build time; a local build without that
directory falls back to the system `exiftool` at runtime.

## What It Does

- Scans a photo folder for display images (`.hif`, `.heif`, `.heic`, `.jpg`, `.jpeg`).
- Pairs each display image with matching RAW files by stem (`.raf`, `.arw`, `.cr2`, `.cr3`, `.nef`, `.dng`, `.rw2`, `.orf`).
- Detects orphan RAW files that do not have matching display images.
- Generates JPEG thumbnails with Pillow (`pillow-heif` decodes HEIF/HIF on all platforms).
- Estimates a 0-100 focus score at native resolution: samples a 3x3 grid of
  tiles plus the camera's AF-point tile (when EXIF provides it, e.g. Fujifilm
  `FocusPixel`), measures gradient loss under re-blur along four directions,
  and combines the AF-point and sharpest-tile scores. Photos whose score is a
  low outlier within the scanned batch are flagged `focus risk`.
- Reads camera warning tags (Fujifilm focus / blur / exposure warnings) and
  shows them as badges alongside the algorithmic score.
- Reads common EXIF fields for the viewer.
- Shows camera-brand-specific badges via the bundled `exiftool`.
- Stores culling state in SQLite in the per-user data directory.
- Lets you mark photos as keep / review / reject from a web UI.
- Moves rejected display files and paired RAW files into `_PHOTO_CULLER_REJECTED/`.
- Moves orphan RAW files into `_PHOTO_CULLER_ORPHAN_RAW/`.

The app moves files only after confirmation and never deletes files directly.
