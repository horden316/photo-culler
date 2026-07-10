#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import mimetypes
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from _version import VERSION
except Exception:  # pragma: no cover
    VERSION = "dev"

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


DISPLAY_EXTS = {".hif", ".heif", ".heic", ".jpg", ".jpeg"}
RAW_EXTS = {".raf", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".rw2", ".orf"}
SKIP_DIRS = {"photo-culler", "_PHOTO_CULLER_REJECTED", "_PHOTO_CULLER_REVIEW", "_PHOTO_CULLER_ORPHAN_RAW"}
APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
WEB_ROOT = APP_ROOT / "web"
LEGACY_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
try:
    import platformdirs

    DATA_ROOT = Path(platformdirs.user_data_dir("PhotoCuller", appauthor=False))
except Exception:  # pragma: no cover
    DATA_ROOT = LEGACY_DATA_ROOT
THUMB_ROOT = DATA_ROOT / "thumbs"
FULL_ROOT = DATA_ROOT / "full"
PREVIEW_ROOT = DATA_ROOT / "preview"
DB_PATH = DATA_ROOT / "catalog.sqlite3"
MIME_TYPES = {
    ".hif": "image/heif",
    ".heif": "image/heif",
    ".heic": "image/heic",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
SCAN_JOB = {
    "running": False,
    "paused": False,
    "done": 0,
    "total": 0,
    "message": "Idle",
    "result": None,
    "error": None,
}
SCAN_LOCK = threading.Lock()
DEFAULT_WORKERS = 8
FOCUS_ABS_FLOOR = 42.0
FOCUS_SOFT_CAP = 50.0
FOCUS_MAD_MULTIPLIER = 2.0


def default_library() -> Path:
    return Path.home()


def connect() -> sqlite3.Connection:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    THUMB_ROOT.mkdir(parents=True, exist_ok=True)
    FULL_ROOT.mkdir(parents=True, exist_ok=True)
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        legacy_db = LEGACY_DATA_ROOT / "catalog.sqlite3"
        if legacy_db != DB_PATH and legacy_db.exists():
            shutil.copy2(legacy_db, DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            directory TEXT NOT NULL,
            stem TEXT NOT NULL,
            ext TEXT NOT NULL,
            raw_path TEXT,
            width INTEGER,
            height INTEGER,
            created_at TEXT,
            blur_score REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'unmarked',
            warnings TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS orphan_raws (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            directory TEXT NOT NULL,
            stem TEXT NOT NULL,
            ext TEXT NOT NULL,
            size_bytes INTEGER,
            created_at TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_marks (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    try:
        db.execute("ALTER TABLE photos ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    db.execute(
        """
        INSERT INTO photo_marks (id, path, status, updated_at)
        SELECT id, path, status, updated_at
        FROM photos
        WHERE status != 'unmarked'
        ON CONFLICT(id) DO UPDATE SET
            path=excluded.path,
            status=excluded.status,
            updated_at=excluded.updated_at
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_photos_stem ON photos(stem)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_photo_marks_path ON photo_marks(path)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orphan_raws_stem ON orphan_raws(stem)")
    db.commit()
    return db


def photo_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def json_response(handler: SimpleHTTPRequestHandler, payload, status=HTTPStatus.OK):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: SimpleHTTPRequestHandler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".") for part in path.parts)


def list_directory(path: Path | None = None) -> dict:
    current = (path or default_library()).expanduser().resolve()
    if not current.exists() or not current.is_dir():
        raise ValueError(f"Folder does not exist: {current}")
    directories = []
    for child in current.iterdir():
        if child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        try:
            if child.is_dir():
                directories.append({"name": child.name, "path": str(child.resolve())})
        except OSError:
            continue
    directories.sort(key=lambda item: item["name"].lower())
    shortcuts = [
        ("Home", Path.home()),
        ("Desktop", Path.home() / "Desktop"),
        ("Pictures", Path.home() / "Pictures"),
    ]
    return {
        "path": str(current),
        "parent": str(current.parent) if current.parent != current else None,
        "directories": directories,
        "shortcuts": [
            {"name": name, "path": str(folder.resolve())}
            for name, folder in shortcuts
            if folder.exists() and folder.is_dir()
        ],
    }


def scan_files(library: Path):
    display_files: list[Path] = []
    raw_by_key: dict[tuple[str, str], Path] = {}
    for path in library.iterdir():
        if path.name.startswith(".") or path.name in SKIP_DIRS:
            continue
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        key = (str(library), path.stem.lower())
        if ext in DISPLAY_EXTS:
            display_files.append(path)
        elif ext in RAW_EXTS:
            raw_by_key[key] = path
    return display_files, raw_by_key


def thumb_path_for(pid: str) -> Path:
    return THUMB_ROOT / f"{pid}.jpg"


def full_path_for(pid: str) -> Path:
    return FULL_ROOT / f"{pid}.jpg"


def preview_path_for(pid: str) -> Path:
    return PREVIEW_ROOT / f"{pid}.jpg"


def prune_cache(keep_ids: set[str]) -> int:
    removed = 0
    for cache_root in (THUMB_ROOT, FULL_ROOT, PREVIEW_ROOT):
        cache_root.mkdir(parents=True, exist_ok=True)
        for path in cache_root.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() != ".jpg" or path.stem not in keep_ids:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def render_cached_jpeg(path: Path, out: Path, max_edge: int | None) -> Path | None:
    """Decode any supported image into a display-oriented cached JPEG."""
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    if Image is None:
        return None
    tmp = out.with_suffix(".tmp.jpg")
    try:
        tmp.unlink(missing_ok=True)
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if max_edge is not None:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            image.convert("RGB").save(tmp, "JPEG", quality=85)
        tmp.replace(out)
        return out
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def ensure_thumbnail(path: Path, pid: str) -> Path | None:
    return render_cached_jpeg(path, thumb_path_for(pid), 900)


def ensure_smooth_preview(path: Path, pid: str) -> Path | None:
    return render_cached_jpeg(path, preview_path_for(pid), 2400)


def ensure_full_preview(path: Path, pid: str) -> Path | None:
    return render_cached_jpeg(path, full_path_for(pid), None)


FOCUS_TILE = 512
FOCUS_GRID = 3
FOCUS_REBLUR_TAPS = 9
FOCUS_EDGE_MIN_GRAD = 4.0
FOCUS_EDGE_COHERENCE = 2
FOCUS_MIN_EDGE_PIXELS = 300
FOCUS_POINT_WEIGHT = 0.8
FOCUS_COARSE_FACTOR = 3
FOCUS_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


def median_filter_3x3(arr):
    padded = np.pad(arr, 1, mode="edge")
    stacked = np.stack(
        [padded[y : y + arr.shape[0], x : x + arr.shape[1]] for y in range(3) for x in range(3)]
    )
    return np.median(stacked, axis=0)


def shifted_overlap(arr, dy: int, dx: int, steps: int):
    """View of arr shifted `steps` pixels along direction (dy, dx), aligned to origin."""
    rows, cols = arr.shape
    y0, x0 = max(0, dy * steps), max(0, dx * steps)
    y1, x1 = rows + min(0, dy * steps), cols + min(0, dx * steps)
    return arr[y0:y1, x0:x1]


def directional_blur_extent(tile, dy: int, dx: int) -> float | None:
    """Re-blur metric along one direction, measured at edge pixels only.

    Re-blurs the tile with a 1-D mean filter along (dy, dx) and averages the
    per-pixel gradient-loss ratio: crisp edges lose most of their gradient,
    already-soft edges barely change. The unweighted per-pixel average keeps a
    few oversharpened halo pixels from outvoting large soft areas.

    At native resolution most pixel differences are sensor noise, so pixels
    below the noise floor are excluded, as are isolated ones — real edges form
    contiguous lines while noise spikes stand alone. Returns blur extent 0..1
    (1 = fully blurred), or None with too few edge pixels to measure.
    """
    half = FOCUS_REBLUR_TAPS // 2
    rows, cols = tile.shape
    pad_y, pad_x = abs(dy) * half, abs(dx) * half
    padded = np.pad(tile, ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")
    acc = np.zeros((rows, cols), dtype=np.float64)
    for step in range(-half, half + 1):
        acc += padded[pad_y + dy * step : pad_y + dy * step + rows, pad_x + dx * step : pad_x + dx * step + cols]
    blurred = acc / FOCUS_REBLUR_TAPS

    def central_gradient(arr):
        return np.abs(shifted_overlap(arr, dy, dx, 1) - shifted_overlap(arr, -dy, -dx, 1)) / 2

    grad = central_gradient(tile)
    grad_blurred = central_gradient(blurred)
    mask = grad >= FOCUS_EDGE_MIN_GRAD
    mask_u8 = mask.astype(np.uint8)
    m_rows, m_cols = mask_u8.shape
    padded_mask = np.pad(mask_u8, 1)
    neighbours = np.zeros((m_rows, m_cols), dtype=np.int16)
    for ny in (-1, 0, 1):
        for nx in (-1, 0, 1):
            if ny or nx:
                neighbours += padded_mask[1 + ny : 1 + ny + m_rows, 1 + nx : 1 + nx + m_cols]
    mask &= neighbours >= FOCUS_EDGE_COHERENCE
    if int(mask.sum()) < FOCUS_MIN_EDGE_PIXELS:
        return None
    edge_grad = grad[mask]
    loss = np.clip(edge_grad - grad_blurred[mask], 0, None)
    return 1.0 - float((loss / edge_grad).mean())


def tile_sharpness(tile_u8) -> float | None:
    """Sharpness 0..1 for one tile; None when the tile lacks usable edges.

    Measures along four directions (axes + diagonals) and keeps the blurriest:
    motion smear leaves thin streaks that stay crisp across the motion axis but
    are smooth along it, so a single direction cannot expose it.
    """
    tile = median_filter_3x3(tile_u8.astype(np.float32))
    worst_blur = None
    for dy, dx in FOCUS_DIRECTIONS:
        blur_extent = directional_blur_extent(tile, dy, dx)
        if blur_extent is None:
            continue
        if worst_blur is None or blur_extent > worst_blur:
            worst_blur = blur_extent
    if worst_blur is None:
        return None
    return max(0.0, min(1.0, 1.0 - worst_blur))


def undo_exif_orientation(gray, orientation: int):
    """Map display-oriented pixels back to sensor orientation."""
    if orientation == 2:
        return np.fliplr(gray)
    if orientation == 3:
        return np.rot90(gray, 2)
    if orientation == 4:
        return np.flipud(gray)
    if orientation == 5:
        return gray.T
    if orientation == 6:
        return np.rot90(gray, 1)
    if orientation == 7:
        return np.rot90(gray, 2).T
    if orientation == 8:
        return np.rot90(gray, -1)
    return gray


def decode_full_gray(path: Path):
    """Full-resolution grayscale pixels in sensor orientation.

    FocusPixel AF coordinates are relative to the unrotated sensor frame, so
    rotation that pillow-heif applies on decode is undone here. Plain JPEGs
    are never auto-rotated by Pillow and need no correction.
    """
    try:
        with Image.open(path) as image:
            orientation = image.info.get("original_orientation") or 1
            gray = np.asarray(image.convert("L"), dtype=np.uint8)
        return np.ascontiguousarray(undo_exif_orientation(gray, orientation))
    except Exception:
        return None


def clamped_tile_origin(center_x: int, center_y: int, width: int, height: int) -> tuple[int, int]:
    x0 = min(max(0, center_x - FOCUS_TILE // 2), max(0, width - FOCUS_TILE))
    y0 = min(max(0, center_y - FOCUS_TILE // 2), max(0, height - FOCUS_TILE))
    return x0, y0


def coarse_tile_at(gray, center_x: int, center_y: int):
    """FOCUS_TILE-sized tile box-downsampled FOCUS_COARSE_FACTOR x around a point.

    The coarse scale sees past in-camera sharpening halos (1-2px, gone after
    downsampling) so wide motion smear that fools the native-scale metric
    still reads as blurred here.
    """
    factor = FOCUS_COARSE_FACTOR
    size = FOCUS_TILE * factor
    height, width = gray.shape
    x0 = min(max(0, center_x - size // 2), max(0, width - size))
    y0 = min(max(0, center_y - size // 2), max(0, height - size))
    region = gray[y0 : y0 + size, x0 : x0 + size].astype(np.float32)
    rows = region.shape[0] // factor * factor
    cols = region.shape[1] // factor * factor
    if rows < factor or cols < factor:
        return None
    return region[:rows, :cols].reshape(rows // factor, factor, cols // factor, factor).mean(axis=(1, 3))


def dual_scale_sharpness(gray, center_x: int, center_y: int, allow_coarse_only: bool) -> float | None:
    """Sharpness at native scale corrected downward by the coarse scale.

    Native-scale evidence is required for grid tiles: dark low-texture tiles
    that only resolve at the coarse scale would otherwise report inflated
    sharpness. The AF-point tile may fall back to coarse-only
    (allow_coarse_only): structure that exists only at the coarse scale right
    where the camera focused is itself evidence of blur.
    """
    height, width = gray.shape
    x0, y0 = clamped_tile_origin(center_x, center_y, width, height)
    fine = tile_sharpness(gray[y0 : y0 + FOCUS_TILE, x0 : x0 + FOCUS_TILE])
    coarse_input = coarse_tile_at(gray, center_x, center_y)
    coarse = tile_sharpness(coarse_input) if coarse_input is not None else None
    if fine is None:
        return coarse if allow_coarse_only else None
    return min(fine, coarse) if coarse is not None else fine


def analyze_focus(path: Path, focus_pixel: tuple[int, int] | None) -> dict | None:
    """Grid + AF-point sharpness measured at native and 3x-coarse scale.

    Returns {"score", "sharpMax", "sharpFocus"} (score 0-100) or None when the
    image cannot be analyzed.
    """
    if Image is None or np is None:
        return None
    gray = decode_full_gray(path)
    if gray is None:
        return None
    height, width = gray.shape
    if height < FOCUS_TILE or width < FOCUS_TILE:
        return None

    sharp_values: list[float] = []
    for row in range(FOCUS_GRID):
        for col in range(FOCUS_GRID):
            cx = int(width * (2 * col + 1) / (2 * FOCUS_GRID))
            cy = int(height * (2 * row + 1) / (2 * FOCUS_GRID))
            value = dual_scale_sharpness(gray, cx, cy, allow_coarse_only=False)
            if value is not None:
                sharp_values.append(value)

    sharp_focus = None
    if focus_pixel is not None:
        fx, fy = focus_pixel
        if 0 <= fx < width and 0 <= fy < height:
            sharp_focus = dual_scale_sharpness(gray, fx, fy, allow_coarse_only=True)

    if not sharp_values and sharp_focus is None:
        return None
    sharp_max = max(sharp_values) if sharp_values else sharp_focus
    if sharp_focus is not None:
        sharp_max = max(sharp_max, sharp_focus)
        combined = FOCUS_POINT_WEIGHT * sharp_focus + (1 - FOCUS_POINT_WEIGHT) * sharp_max
    else:
        combined = sharp_max
    return {
        "score": round(100.0 * combined, 2),
        "sharpMax": round(100.0 * sharp_max, 2),
        "sharpFocus": round(100.0 * sharp_focus, 2) if sharp_focus is not None else None,
    }


def as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        try:
            return float(value[0]) / float(value[1])
        except Exception:
            return None


def format_shutter(seconds: float | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    if seconds >= 1:
        value = f"{seconds:.1f}".rstrip("0").rstrip(".")
        return f"{value}s"
    denominator = round(1 / seconds)
    return f"1/{denominator}"


def format_ev(value: float | None) -> str | None:
    if value is None:
        return None
    if abs(value) < 0.005:
        return "0 EV"
    thirds = round(value * 3)
    if abs(value - thirds / 3) < 0.03:
        sign = "+" if thirds > 0 else "-"
        whole = abs(thirds) // 3
        remainder = abs(thirds) % 3
        if remainder == 0:
            return f"{sign}{whole} EV"
        if whole == 0:
            return f"{sign}{remainder}/3 EV"
        return f"{sign}{whole} {remainder}/3 EV"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} EV".replace(".00", "")


def compact_badge(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def exiftool_float(value: object) -> float | None:
    text = compact_badge(value)
    if not text:
        return None
    if "/" in text:
        try:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        except Exception:
            return None
    try:
        return float(text.split()[0])
    except Exception:
        return None


def compact_fuji_film_mode(value: object) -> str | None:
    text = compact_badge(value)
    if not text:
        return None
    if "(" in text and ")" in text:
        short_name = text.rsplit("(", 1)[1].split(")", 1)[0].strip()
        if short_name:
            return short_name
    if "/" in text:
        text = text.split("/", 1)[1].strip()
    return text or None


def fuji_brand_badges(tags: dict) -> list[str]:
    badges: list[str] = []
    dynamic_range = compact_badge(tags.get("DevelopmentDynamicRange") or tags.get("DynamicRange"))
    if dynamic_range and dynamic_range.lower() != "standard":
        badges.append(dynamic_range.upper() if dynamic_range.upper().startswith("DR") else f"DR{dynamic_range}")
    elif dynamic_range:
        badges.append("DR100")

    film = compact_fuji_film_mode(tags.get("FilmMode") or tags.get("FilmSimulation"))
    if film:
        badges.append(f"Film {film}")

    white_balance = compact_badge(tags.get("WhiteBalance"))
    if white_balance and white_balance.lower() not in {"auto", "unknown"}:
        badges.append(f"WB {white_balance}")

    color_chrome = compact_badge(tags.get("ColorChromeEffect"))
    if color_chrome and color_chrome.lower() not in {"off", "none"}:
        badges.append(f"Chrome {color_chrome}")

    chrome_blue = compact_badge(tags.get("ColorChromeFXBlue"))
    if chrome_blue and chrome_blue.lower() not in {"off", "none"}:
        badges.append(f"Blue {chrome_blue}")

    return badges


_EXIFTOOL_CMD: list[str] | None = None
_EXIFTOOL_RESOLVED = False


def find_exiftool() -> list[str] | None:
    """Command to invoke exiftool: the bundled copy first, then a system install."""
    global _EXIFTOOL_CMD, _EXIFTOOL_RESOLVED
    if _EXIFTOOL_RESOLVED:
        return _EXIFTOOL_CMD
    command = None
    if sys.platform == "win32":
        bundled = APP_ROOT / "exiftool" / "exiftool.exe"
        if bundled.exists():
            command = [str(bundled)]
    else:
        bundled = APP_ROOT / "exiftool" / "exiftool"
        if bundled.exists() and shutil.which("perl"):
            command = ["perl", str(bundled)]
    if command is None:
        system = shutil.which("exiftool")
        command = [system] if system else None
    _EXIFTOOL_CMD = command
    _EXIFTOOL_RESOLVED = True
    return command


# Keep console-less on Windows: without this every exiftool call flashes a
# console window in windowed (PyInstaller --noconsole) builds.
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def read_exiftool_tags(path: Path, tags: list[str]) -> dict:
    exiftool_cmd = find_exiftool()
    if not exiftool_cmd:
        return {}
    try:
        proc = subprocess.run(
            [*exiftool_cmd, "-json", *tags, str(path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=SUBPROCESS_FLAGS,
        )
    except Exception:
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    if not payload:
        return {}
    return payload[0]


EXIFTOOL_TAGS = [
    "-Make",
    "-DynamicRange",
    "-DynamicRangeSetting",
    "-DevelopmentDynamicRange",
    "-FilmMode",
    "-FilmSimulation",
    "-WhiteBalance",
    "-ColorChromeEffect",
    "-ColorChromeFXBlue",
    "-ISO",
    "-FNumber",
    "-ExposureTime",
    "-FocalLength",
    "-ExposureCompensation",
    "-DateTimeOriginal",
    "-CreateDate",
    "-FocusWarning",
    "-BlurWarning",
    "-ExposureWarning",
    "-FocusPixel",
]


def parse_focus_pixel(value) -> list[int] | None:
    text = compact_badge(value)
    if not text:
        return None
    parts = text.replace(",", " ").split()
    if len(parts) < 2:
        return None
    try:
        return [int(float(parts[0])), int(float(parts[1]))]
    except ValueError:
        return None


def camera_warning_flags(tags: dict) -> list[str]:
    flags: list[str] = []
    focus = compact_badge(tags.get("FocusWarning"))
    if focus and focus.lower() != "good":
        flags.append("camera_focus")
    blur = compact_badge(tags.get("BlurWarning"))
    if blur and blur.lower() not in {"none", "good"}:
        flags.append("camera_blur")
    exposure = compact_badge(tags.get("ExposureWarning"))
    if exposure and exposure.lower() != "good":
        flags.append("camera_exposure")
    return flags


def extract_exiftool_metadata(path: Path) -> dict:
    tags = read_exiftool_tags(path, EXIFTOOL_TAGS)
    if not tags:
        return {}
    iso = compact_badge(tags.get("ISO"))
    aperture = exiftool_float(tags.get("FNumber"))
    exposure_time = exiftool_float(tags.get("ExposureTime"))
    focal_length = exiftool_float(tags.get("FocalLength"))
    exposure_bias = exiftool_float(tags.get("ExposureCompensation"))
    metadata = {
        "iso": f"ISO {iso}" if iso is not None else None,
        "aperture": f"f/{aperture:.1f}".replace(".0", "") if aperture else None,
        "shutter": format_shutter(exposure_time),
        "focalLength": f"{focal_length:.0f}mm" if focal_length else None,
        "exposureCompensation": format_ev(exposure_bias),
        "capturedAt": tags.get("DateTimeOriginal") or tags.get("CreateDate"),
        "cameraWarnings": camera_warning_flags(tags),
        "focusPixel": parse_focus_pixel(tags.get("FocusPixel")),
    }
    make = compact_badge(tags.get("Make")) or ""
    if "fujifilm" in make.lower():
        metadata["brandBadges"] = fuji_brand_badges(tags)
    return metadata


def extract_exif(path: Path) -> dict:
    exiftool_metadata = extract_exiftool_metadata(path)
    brand_badges = exiftool_metadata.get("brandBadges") or []
    if Image is None:
        return exiftool_metadata
    ext = path.suffix.lower()
    exif = None
    try:
        if ext in {".jpg", ".jpeg"}:
            with Image.open(path) as image:
                exif = image.getexif()
        elif ext in {".hif", ".heif", ".heic"}:
            try:
                with Image.open(path) as image:
                    exif = image.getexif()
            except Exception:
                exif = None
            if not exif:
                data = path.read_bytes()
                tiff_index = data.find(b"II*\x00")
                if tiff_index == -1:
                    tiff_index = data.find(b"MM\x00*")
                if tiff_index == -1:
                    return exiftool_metadata
                exif = Image.Exif()
                exif.load(data[tiff_index:])
    except Exception:
        return exiftool_metadata
    if not exif:
        return exiftool_metadata
    try:
        exif_ifd = exif.get_ifd(34665)
    except Exception:
        exif_ifd = {}

    iso = exif_ifd.get(34855) or exif_ifd.get(8833)
    aperture = as_float(exif_ifd.get(33437))
    exposure_time = as_float(exif_ifd.get(33434))
    focal_length = as_float(exif_ifd.get(37386))
    exposure_bias = as_float(exif_ifd.get(37380))

    merged = {
        "iso": f"ISO {iso}" if iso is not None else None,
        "aperture": f"f/{aperture:.1f}".replace(".0", "") if aperture else None,
        "shutter": format_shutter(exposure_time),
        "focalLength": f"{focal_length:.0f}mm" if focal_length else None,
        "exposureCompensation": format_ev(exposure_bias),
        "capturedAt": exif_ifd.get(36867) or exif.get(306),
    }
    merged.update({key: value for key, value in exiftool_metadata.items() if value not in (None, "", [])})
    merged["brandBadges"] = brand_badges
    return merged


def warnings_for(raw_path: Path | None) -> list[str]:
    # focus_risk is assigned after the whole batch is scanned (relative threshold)
    warnings: list[str] = []
    if raw_path is None:
        warnings.append("no_raw_pair")
    return warnings


def apply_focus_risk_flags(db: sqlite3.Connection) -> float:
    """Flag photos whose score is a low outlier within the scanned batch."""
    rows = db.execute("SELECT id, blur_score, warnings FROM photos").fetchall()
    scores = [row["blur_score"] for row in rows if row["blur_score"] is not None]
    threshold = FOCUS_ABS_FLOOR
    if len(scores) >= 5:
        med = statistics.median(scores)
        sigma = 1.4826 * statistics.median([abs(s - med) for s in scores])
        threshold = max(FOCUS_ABS_FLOOR, min(med - FOCUS_MAD_MULTIPLIER * sigma, FOCUS_SOFT_CAP))
    for row in rows:
        warnings = [w for w in json.loads(row["warnings"] or "[]") if w != "focus_risk"]
        if row["blur_score"] is not None and row["blur_score"] < threshold:
            warnings.append("focus_risk")
        db.execute("UPDATE photos SET warnings = ? WHERE id = ?", (json.dumps(warnings), row["id"]))
    return threshold


def process_photo(path: Path, raw_path: Path | None) -> dict:
    pid = photo_id(path)
    thumb = ensure_thumbnail(path, pid)
    metadata = extract_exif(path)
    focus_pixel = metadata.get("focusPixel")
    focus = analyze_focus(path, tuple(focus_pixel) if focus_pixel else None)
    if focus is not None:
        metadata["sharpMax"] = focus["sharpMax"]
        metadata["sharpFocus"] = focus["sharpFocus"]
    width = None
    height = None
    if thumb and Image is not None:
        try:
            with Image.open(thumb) as image:
                width, height = image.size
        except Exception:
            pass
    created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    warnings = warnings_for(raw_path)
    return {
        "id": pid,
        "path": str(path),
        "directory": str(path.parent),
        "stem": path.stem,
        "ext": path.suffix.lower(),
        "raw_path": str(raw_path) if raw_path else None,
        "width": width,
        "height": height,
        "created_at": created_at,
        "blur_score": focus["score"] if focus is not None else None,
        "metadata_json": json.dumps(metadata),
        "warnings": json.dumps(warnings),
        "updated_at": time.time(),
    }


def update_scan_job(**updates):
    with SCAN_LOCK:
        SCAN_JOB.update(updates)


def wait_for_scan_resume():
    while True:
        with SCAN_LOCK:
            paused = SCAN_JOB["paused"]
            running = SCAN_JOB["running"]
        if not running or not paused:
            return
        time.sleep(0.2)


def scan_library(library: Path, workers: int = DEFAULT_WORKERS) -> dict:
    if not library.exists() or not library.is_dir():
        raise ValueError(f"Library folder does not exist: {library}")
    display_files, raw_by_key = scan_files(library)
    keep_cache_ids = {photo_id(path) for path in display_files}
    display_keys = {(str(path.parent), path.stem.lower()) for path in display_files}
    orphan_raws = [path for key, path in raw_by_key.items() if key not in display_keys]
    db = connect()
    db.execute("DELETE FROM photos WHERE path NOT LIKE ?", (f"{str(library)}{os.sep}%",))
    db.execute("DELETE FROM photos WHERE directory != ?", (str(library),))
    db.execute("DELETE FROM orphan_raws")
    db.commit()
    removed_cache = prune_cache(keep_cache_ids)
    started = time.time()
    update_scan_job(
        running=True,
        paused=False,
        done=0,
        total=len(display_files),
        message=f"Cleared {removed_cache} stale cache files" if removed_cache else "Generating thumbnails",
    )
    futures = {}
    worker_count = max(1, workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        paths = iter(display_files)

        def submit_next() -> bool:
            try:
                path = next(paths)
            except StopIteration:
                return False
            key = (str(path.parent), path.stem.lower())
            futures[executor.submit(process_photo, path, raw_by_key.get(key))] = path
            return True

        for _ in range(worker_count):
            if not submit_next():
                break

        index = 0
        while futures:
            done_futures, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done_futures:
                futures.pop(future, None)
                wait_for_scan_resume()
                index += 1
                item = future.result()
                item["status"] = saved_status(db, item["id"], item["path"])
                db.execute(
                    """
                    INSERT INTO photos (
                        id, path, directory, stem, ext, raw_path, width, height,
                        created_at, blur_score, metadata_json, status, warnings, updated_at
                    )
                    VALUES (
                        :id, :path, :directory, :stem, :ext, :raw_path, :width, :height,
                        :created_at, :blur_score, :metadata_json, :status, :warnings, :updated_at
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        path=excluded.path,
                        directory=excluded.directory,
                        stem=excluded.stem,
                        ext=excluded.ext,
                        raw_path=excluded.raw_path,
                        width=excluded.width,
                        height=excluded.height,
                        created_at=excluded.created_at,
                        blur_score=excluded.blur_score,
                        metadata_json=excluded.metadata_json,
                        status=excluded.status,
                        warnings=excluded.warnings,
                        updated_at=excluded.updated_at
                    """,
                    item,
                )
                if index % 25 == 0:
                    db.commit()
                    update_scan_job(done=index, message="Processing")
                wait_for_scan_resume()
                submit_next()
    db.commit()
    update_scan_job(done=len(display_files), message="Flagging focus risk")
    apply_focus_risk_flags(db)
    db.commit()
    for raw_path in orphan_raws:
        stat = raw_path.stat()
        db.execute(
            """
            INSERT INTO orphan_raws (
                id, path, directory, stem, ext, size_bytes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                directory=excluded.directory,
                stem=excluded.stem,
                ext=excluded.ext,
                size_bytes=excluded.size_bytes,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at
            """,
            (
                photo_id(raw_path),
                str(raw_path),
                str(raw_path.parent),
                raw_path.stem,
                raw_path.suffix.lower(),
                stat.st_size,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                time.time(),
            ),
        )
    db.commit()
    update_scan_job(done=len(display_files), message="Finalizing")
    total = db.execute("SELECT COUNT(*) AS count FROM photos").fetchone()["count"]
    paired = db.execute("SELECT COUNT(*) AS count FROM photos WHERE raw_path IS NOT NULL").fetchone()["count"]
    focus_risk = db.execute(
        "SELECT COUNT(*) AS count FROM photos WHERE warnings LIKE '%focus_risk%' OR warnings LIKE '%soft%'"
    ).fetchone()["count"]
    orphan_count = db.execute("SELECT COUNT(*) AS count FROM orphan_raws").fetchone()["count"]
    db.close()
    result = {
        "scanned": len(display_files),
        "total": total,
        "paired": paired,
        "focusRisk": focus_risk,
        "orphanRaws": orphan_count,
        "seconds": round(time.time() - started, 2),
    }
    update_scan_job(running=False, paused=False, result=result, message="Complete")
    return result


def start_scan(library: Path, workers: int = DEFAULT_WORKERS) -> dict:
    with SCAN_LOCK:
        if SCAN_JOB["running"]:
            return {"started": False, "job": dict(SCAN_JOB)}
        SCAN_JOB.update(
            {
                "running": True,
                "paused": False,
                "done": 0,
                "total": 0,
                "message": "Starting",
                "result": None,
                "error": None,
            }
        )

    def runner():
        try:
            scan_library(library, workers)
        except Exception as exc:
            update_scan_job(running=False, paused=False, error=str(exc), message="Failed")

    threading.Thread(target=runner, daemon=True).start()
    return {"started": True, "job": dict(SCAN_JOB)}


def set_scan_paused(paused: bool) -> dict:
    with SCAN_LOCK:
        if not SCAN_JOB["running"]:
            SCAN_JOB["paused"] = False
            return {"paused": False, "job": dict(SCAN_JOB)}
        SCAN_JOB["paused"] = paused
        SCAN_JOB["message"] = "Paused" if paused else "Processing"
        return {"paused": paused, "job": dict(SCAN_JOB)}


def saved_status(db: sqlite3.Connection, pid: str, path: str) -> str:
    row = db.execute(
        "SELECT status FROM photo_marks WHERE id = ? OR path = ? ORDER BY updated_at DESC LIMIT 1",
        (pid, path),
    ).fetchone()
    return row["status"] if row else "unmarked"


def row_to_photo(row: sqlite3.Row) -> dict:
    path = Path(row["path"])
    metadata = json.loads(row["metadata_json"] or "{}") if "metadata_json" in row.keys() else {}
    metadata.setdefault("brandBadges", [])
    return {
        "id": row["id"],
        "filename": path.name,
        "path": row["path"],
        "rawPath": row["raw_path"],
        "width": row["width"],
        "height": row["height"],
        "createdAt": row["created_at"],
        "focusScore": row["blur_score"],
        "blurScore": row["blur_score"],
        "metadata": metadata,
        "status": row["status"],
        "warnings": json.loads(row["warnings"] or "[]"),
        "thumbUrl": f"/thumbs/{row['id']}.jpg",
        "previewUrl": f"/api/photos/{row['id']}/preview",
        "fullUrl": f"/api/photos/{row['id']}/full",
        "originalUrl": f"/api/photos/{row['id']}/original",
    }


def photo_metadata(pid: str) -> dict:
    db = connect()
    row = db.execute("SELECT path, metadata_json FROM photos WHERE id = ?", (pid,)).fetchone()
    if row is None:
        db.close()
        raise ValueError("Photo not found")
    cached = json.loads(row["metadata_json"] or "{}")
    path = Path(row["path"])
    parsed = extract_exif(path) if path.exists() else {}
    metadata = dict(cached)
    if any(parsed.values()):
        metadata.update({key: value for key, value in parsed.items() if value is not None})
    metadata.setdefault("brandBadges", [])
    if any(parsed.values()):
        db.execute(
            "UPDATE photos SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata), time.time(), pid),
        )
        db.commit()
    db.close()
    return metadata


def full_preview(pid: str) -> Path:
    db = connect()
    row = db.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()
    db.close()
    if row is None:
        raise ValueError("Photo not found")
    path = Path(row["path"])
    if not path.exists():
        raise ValueError("Source photo not found")
    preview = ensure_full_preview(path, pid)
    if preview is None or not preview.exists():
        raise ValueError("Could not generate full preview")
    return preview


def smooth_preview(pid: str) -> Path:
    db = connect()
    row = db.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()
    db.close()
    if row is None:
        raise ValueError("Photo not found")
    path = Path(row["path"])
    if not path.exists():
        raise ValueError("Source photo not found")
    preview = ensure_smooth_preview(path, pid)
    if preview is None or not preview.exists():
        raise ValueError("Could not generate smooth preview")
    return preview


def original_photo(pid: str) -> tuple[Path, str]:
    db = connect()
    row = db.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()
    db.close()
    if row is None:
        raise ValueError("Photo not found")
    path = Path(row["path"])
    if not path.exists():
        raise ValueError("Source photo not found")
    content_type = MIME_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, content_type


def send_file(handler: SimpleHTTPRequestHandler, path: Path, content_type: str):
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.end_headers()
    with path.open("rb") as file:
        shutil.copyfileobj(file, handler.wfile)


def list_photos(query: dict) -> dict:
    status = query.get("status", [""])[0]
    warning = query.get("warning", [""])[0]
    search = query.get("search", [""])[0].strip().lower()
    limit = min(int(query.get("limit", ["300"])[0]), 1000)
    offset = int(query.get("offset", ["0"])[0])
    clauses = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if warning:
        if warning == "focus_risk":
            clauses.append("(warnings LIKE ? OR warnings LIKE ?)")
            params.extend(["%focus_risk%", "%soft%"])
        else:
            clauses.append("warnings LIKE ?")
            params.append(f"%{warning}%")
    if search:
        clauses.append("LOWER(stem) LIKE ?")
        params.append(f"%{search}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    db = connect()
    rows = db.execute(
        f"""
        SELECT * FROM photos
        {where}
        ORDER BY created_at IS NULL, created_at, filename
        LIMIT ? OFFSET ?
        """.replace("filename", "path"),
        (*params, limit, offset),
    ).fetchall()
    count = db.execute(f"SELECT COUNT(*) AS count FROM photos {where}", params).fetchone()["count"]
    stats_rows = db.execute("SELECT status, COUNT(*) AS count FROM photos GROUP BY status").fetchall()
    db.close()
    return {
        "photos": [row_to_photo(row) for row in rows],
        "count": count,
        "stats": {row["status"]: row["count"] for row in stats_rows},
    }


def row_to_orphan_raw(row: sqlite3.Row) -> dict:
    path = Path(row["path"])
    return {
        "id": row["id"],
        "filename": path.name,
        "path": row["path"],
        "sizeBytes": row["size_bytes"],
        "createdAt": row["created_at"],
    }


def list_orphan_raws(query: dict) -> dict:
    search = query.get("search", [""])[0].strip().lower()
    limit = min(int(query.get("limit", ["300"])[0]), 1000)
    offset = int(query.get("offset", ["0"])[0])
    clauses = []
    params: list[object] = []
    if search:
        clauses.append("LOWER(stem) LIKE ?")
        params.append(f"%{search}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    db = connect()
    rows = db.execute(
        f"""
        SELECT * FROM orphan_raws
        {where}
        ORDER BY created_at IS NULL, created_at, path
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    count = db.execute(f"SELECT COUNT(*) AS count FROM orphan_raws {where}", params).fetchone()["count"]
    db.close()
    return {
        "orphanRaws": [row_to_orphan_raw(row) for row in rows],
        "count": count,
    }


def mark_photo(pid: str, status: str) -> dict:
    if status not in {"unmarked", "keep", "review", "reject"}:
        raise ValueError("Invalid status")
    db = connect()
    row = db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
    if row is None:
        db.close()
        raise ValueError("Photo not found")
    updated_at = time.time()
    db.execute("UPDATE photos SET status = ?, updated_at = ? WHERE id = ?", (status, updated_at, pid))
    if status == "unmarked":
        db.execute("DELETE FROM photo_marks WHERE id = ? OR path = ?", (pid, row["path"]))
    else:
        db.execute(
            """
            INSERT INTO photo_marks (id, path, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (pid, row["path"], status, updated_at),
        )
    db.commit()
    row = db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
    db.close()
    return row_to_photo(row)


def unique_destination(path: Path, rejected_root: Path) -> Path:
    dest = rejected_root / path.name
    if not dest.exists():
        return dest
    counter = 1
    while True:
        candidate = rejected_root / f"{path.stem}-{counter}{path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def move_rejected(library: Path) -> dict:
    rejected_root = library / "_PHOTO_CULLER_REJECTED"
    rejected_root.mkdir(exist_ok=True)
    db = connect()
    rows = db.execute("SELECT * FROM photos WHERE status = 'reject'").fetchall()
    moved = []
    for row in rows:
        pid = row["id"]
        paths = [Path(row["path"])]
        if row["raw_path"]:
            paths.append(Path(row["raw_path"]))
        moved_paths = []
        for src in paths:
            if not src.exists() or not is_inside(src, library):
                continue
            dest = unique_destination(src, rejected_root)
            shutil.move(str(src), str(dest))
            moved_paths.append(str(dest))
        if moved_paths:
            db.execute("DELETE FROM photos WHERE id = ?", (pid,))
            db.execute("DELETE FROM photo_marks WHERE id = ? OR path = ?", (pid, row["path"]))
            moved.append({"id": pid, "destinations": moved_paths})
    db.commit()
    db.close()
    return {"moved": moved, "count": len(moved)}


def move_orphan_raws(library: Path) -> dict:
    orphan_root = library / "_PHOTO_CULLER_ORPHAN_RAW"
    orphan_root.mkdir(exist_ok=True)
    db = connect()
    rows = db.execute("SELECT * FROM orphan_raws").fetchall()
    moved = []
    for row in rows:
        src = Path(row["path"])
        if not src.exists() or not is_inside(src, library):
            continue
        dest = unique_destination(src, orphan_root)
        shutil.move(str(src), str(dest))
        db.execute("DELETE FROM orphan_raws WHERE id = ?", (row["id"],))
        moved.append({"id": row["id"], "destination": str(dest)})
    db.commit()
    db.close()
    return {"moved": moved, "count": len(moved)}


class Handler(SimpleHTTPRequestHandler):
    library: Path = default_library()
    library_selected: bool = False
    workers: int = DEFAULT_WORKERS

    def end_headers(self):
        path = urlparse(self.path).path
        if not path.startswith(("/thumbs/", "/full/", "/preview/", "/api/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def translate_path(self, path):
        parsed = urlparse(path)
        if parsed.path.startswith("/thumbs/"):
            name = Path(unquote(parsed.path.replace("/thumbs/", ""))).name
            return str(THUMB_ROOT / name)
        if parsed.path.startswith("/full/"):
            name = Path(unquote(parsed.path.replace("/full/", ""))).name
            return str(FULL_ROOT / name)
        if parsed.path.startswith("/preview/"):
            name = Path(unquote(parsed.path.replace("/preview/", ""))).name
            return str(PREVIEW_ROOT / name)
        if parsed.path == "/":
            return str(WEB_ROOT / "index.html")
        return str(WEB_ROOT / parsed.path.lstrip("/"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/photos":
            if not self.library_selected:
                json_response(self, {"photos": [], "count": 0, "stats": {}})
                return
            try:
                json_response(self, list_photos(parse_qs(parsed.query)))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/orphan-raws":
            if not self.library_selected:
                json_response(self, {"orphanRaws": [], "count": 0})
                return
            try:
                json_response(self, list_orphan_raws(parse_qs(parsed.query)))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/config":
            json_response(
                self,
                {
                    "library": str(self.library),
                    "librarySelected": self.library_selected,
                    "workers": self.workers,
                    "db": str(DB_PATH),
                    "version": VERSION,
                },
            )
            return
        if parsed.path == "/api/browse":
            try:
                query = parse_qs(parsed.query)
                path = query.get("path", [None])[0]
                json_response(self, list_directory(Path(path) if path else self.library))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/scan-status":
            with SCAN_LOCK:
                json_response(self, dict(SCAN_JOB))
            return
        if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/metadata"):
            try:
                pid = parsed.path.split("/")[3]
                json_response(self, photo_metadata(pid))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/full"):
            try:
                pid = parsed.path.split("/")[3]
                self.path = f"/full/{full_preview(pid).name}"
                super().do_GET()
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/preview"):
            try:
                pid = parsed.path.split("/")[3]
                self.path = f"/preview/{smooth_preview(pid).name}"
                super().do_GET()
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/original"):
            try:
                pid = parsed.path.split("/")[3]
                path, content_type = original_photo(pid)
                send_file(self, path, content_type)
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/scan":
                payload = read_json(self)
                library = Path(payload.get("library") or self.library).expanduser().resolve()
                self.__class__.library = library
                self.__class__.library_selected = True
                json_response(self, start_scan(library, self.workers))
                return
            if parsed.path == "/api/scan-control":
                payload = read_json(self)
                json_response(self, set_scan_paused(bool(payload.get("paused"))))
                return
            if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/mark"):
                pid = parsed.path.split("/")[3]
                payload = read_json(self)
                json_response(self, mark_photo(pid, payload.get("status", "unmarked")))
                return
            if parsed.path == "/api/move-rejected":
                if not self.library_selected:
                    json_response(self, {"error": "No library selected"}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, move_rejected(self.library))
                return
            if parsed.path == "/api/move-orphan-raws":
                if not self.library_selected:
                    json_response(self, {"error": "No library selected"}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, move_orphan_raws(self.library))
                return
            json_response(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", default=None, help="Photo library folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent photo processing workers")
    args = parser.parse_args(argv)

    Handler.library = Path(args.library).expanduser().resolve() if args.library else default_library().resolve()
    Handler.library_selected = args.library is not None
    Handler.workers = max(1, args.workers)
    connect().close()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Photo Culler running at http://{args.host}:{args.port}")
    print(f"Library: {Handler.library}")
    print(f"Workers: {Handler.workers}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
