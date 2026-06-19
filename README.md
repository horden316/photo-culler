# Photo Culler

Local-first photo culling MVP for HEIF/HIF + RAW workflows.

## Requirements

- macOS with `sips`
- Python 3 with Pillow installed

## Run

From the project folder:

```bash
python3 app/server.py --library /path/to/photos
```

Open:

```text
http://127.0.0.1:8765
```

## What It Does

- Scans a photo folder for display images (`.hif`, `.heif`, `.heic`, `.jpg`, `.jpeg`).
- Pairs each display image with matching RAW files by stem (`.raf`, `.arw`, `.cr2`, `.cr3`, `.nef`, `.dng`, `.rw2`, `.orf`).
- Detects orphan RAW files that do not have matching display images.
- Generates JPEG thumbnails with macOS `sips`.
- Reads common EXIF fields for the viewer.
- Stores culling state in SQLite.
- Lets you mark photos as keep / review / reject from a web UI.
- Moves rejected display files and paired RAW files into `_PHOTO_CULLER_REJECTED/`.
- Moves orphan RAW files into `_PHOTO_CULLER_ORPHAN_RAW/`.

The app moves files only after confirmation and never deletes files directly.
