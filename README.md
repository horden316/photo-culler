# Photo Culler

Local-first photo culling MVP for HEIF/HIF + RAW workflows.

## Requirements

- macOS with `sips`
- Python 3 with Pillow installed:
  - pip: `python3 -m pip install Pillow`
  - conda: `conda install pillow`
- Optional but recommended: NumPy for faster focus scoring:
  - pip: `python3 -m pip install numpy`
  - conda: `conda install numpy`
  - HIF/HEIF support depends on the Python environment and native image libraries. If your Pillow build cannot read HIF metadata, install `exiftool` below; Photo Culler will use it as a fallback.
- Optional: `exiftool` for camera-brand-specific metadata badges, such as Fujifilm DR and film simulation.
  - macOS: `brew install exiftool`
  - Linux: install `libimage-exiftool-perl` with your package manager, for example `sudo apt install libimage-exiftool-perl`
  - Windows: install ExifTool from <https://exiftool.org/> or with a package manager such as Chocolatey: `choco install exiftool`

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
- Estimates a 0-100 focus score from the sharpest local image regions.
- Reads common EXIF fields for the viewer.
- Shows camera-brand-specific badges when `exiftool` is installed.
- Stores culling state in SQLite.
- Lets you mark photos as keep / review / reject from a web UI.
- Moves rejected display files and paired RAW files into `_PHOTO_CULLER_REJECTED/`.
- Moves orphan RAW files into `_PHOTO_CULLER_ORPHAN_RAW/`.

The app moves files only after confirmation and never deletes files directly.
