#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import mimetypes
from collections import deque
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
DISPLAY_ROOT = DATA_ROOT / "display"
# Cache directories earlier versions wrote and nothing reads any more.
RETIRED_CACHE_ROOTS = (DATA_ROOT / "preview", DATA_ROOT / "full")
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
    "stage": "idle",
    "done": 0,
    "total": 0,
    "message": "Idle",
    "result": None,
    "error": None,
    "pending": 0,
}
SCAN_LOCK = threading.Lock()
DEFAULT_WORKERS = min(32, os.cpu_count() or 4)
# Grid cards are 230-300px wide, so 600px still lands at 1:1 on a 2x display
# while halving what the thumbnail cache costs on disk.
THUMB_EDGE = 600
# Substitute renders exist only for engines that cannot decode the original
# at all (Chromium). WebKit sessions never create one. Bounded well below the
# 26MP source: a browser handed a full-resolution frame spends ~600ms decoding
# it and another ~100MB uploading the texture, which stalls the whole tab.
DISPLAY_EDGE = 2560
DISPLAY_CACHE_BUDGET = 200 * 1024 * 1024
# Photos per exiftool invocation. Larger batches amortise process startup
# further but delay the first cards reaching the grid, so the opening batch is
# deliberately small — the grid is populated a second in rather than four.
EXIFTOOL_BATCH = 100
EXIFTOOL_FIRST_BATCH = 20
EXIFTOOL_CONCURRENCY = 2
# Below this the embedded preview is too small to serve as a grid thumbnail
# and the original gets decoded instead.
EMBEDDED_MIN_EDGE = 640
# Scanned folders kept in the catalog; older ones are evicted with their caches.
LIBRARY_HISTORY = 5
FOCUS_ABS_FLOOR = 42.0
FOCUS_SOFT_CAP = 50.0
FOCUS_MAD_MULTIPLIER = 2.0


ANALYSIS_CACHE_DDL = """
    CREATE TABLE IF NOT EXISTS analysis_cache (
        path TEXT PRIMARY KEY,
        directory TEXT NOT NULL,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        focus_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL
    )
"""
ANALYSIS_CACHE_COLUMNS = {"path", "directory", "mtime", "size", "focus_json", "updated_at"}


def default_library() -> Path:
    return Path.home()


def connect() -> sqlite3.Connection:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    THUMB_ROOT.mkdir(parents=True, exist_ok=True)
    DISPLAY_ROOT.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        legacy_db = LEGACY_DATA_ROOT / "catalog.sqlite3"
        if legacy_db != DB_PATH and legacy_db.exists():
            shutil.copy2(legacy_db, DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    # The grid polls while a scan writes; WAL lets those reads through instead
    # of making them wait on the writer's lock.
    db.execute("PRAGMA journal_mode=WAL")
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
    # Focus scores survive rescans and folder switches: re-inserting the same
    # card should not pay for the analysis again.
    db.execute(ANALYSIS_CACHE_DDL)
    # CREATE TABLE IF NOT EXISTS leaves a table built by an older version
    # alone, so a changed column set only surfaces as a missing-column error
    # mid-scan. Rebuild it instead: this table is a pure cache, and losing it
    # costs one re-analysis and nothing the user marked.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(analysis_cache)")}
    if columns != ANALYSIS_CACHE_COLUMNS:
        db.execute("DROP TABLE analysis_cache")
        db.execute(ANALYSIS_CACHE_DDL)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            path TEXT PRIMARY KEY,
            last_used_at REAL NOT NULL
        )
        """
    )
    for statement in (
        "ALTER TABLE photos ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE photos ADD COLUMN analysis_state TEXT NOT NULL DEFAULT 'done'",
    ):
        try:
            db.execute(statement)
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_photos_directory ON photos(directory)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_analysis_cache_directory ON analysis_cache(directory)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_photo_marks_path ON photo_marks(path)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orphan_raws_stem ON orphan_raws(stem)")
    db.commit()
    apply_thumb_size()
    return db


def apply_thumb_size():
    """Drop thumbnails rendered at a different target size.

    Cached renders are keyed by mtime, so a change to THUMB_EDGE would
    otherwise never take effect on a library that has already been scanned.
    The marker lives outside the cache directories, which prune_cache sweeps.
    """
    marker = DATA_ROOT / "thumb-edge.txt"
    current = str(THUMB_EDGE)
    try:
        if marker.exists() and marker.read_text().strip() == current:
            return
        for path in THUMB_ROOT.glob("*.jpg"):
            try:
                path.unlink()
            except OSError:
                pass
        marker.write_text(current)
    except OSError:
        pass


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


def display_path_for(pid: str) -> Path:
    return DISPLAY_ROOT / f"{pid}.jpg"


def remember_library(db: sqlite3.Connection, library: Path) -> int:
    """Record this folder as most recently used and evict the oldest ones.

    Everything the app derives from a photo — thumbnail, score, metadata — is
    expensive enough that re-selecting a folder should never recompute it, so
    the catalog holds several folders at once and drops them by age rather
    than keeping only whatever was scanned last.
    """
    db.execute(
        """
        INSERT INTO libraries (path, last_used_at) VALUES (?, ?)
        ON CONFLICT(path) DO UPDATE SET last_used_at=excluded.last_used_at
        """,
        (str(library), time.time()),
    )
    db.execute(
        "DELETE FROM libraries WHERE path IN (SELECT path FROM libraries ORDER BY last_used_at DESC LIMIT -1 OFFSET ?)",
        (LIBRARY_HISTORY,),
    )
    # Anything whose folder is no longer remembered goes, which covers both the
    # folders just evicted and rows inherited from an older catalog that only
    # ever tracked one folder and so never registered it here.
    orphaned = 0
    for table in ("photos", "orphan_raws", "analysis_cache"):
        cursor = db.execute(f"DELETE FROM {table} WHERE directory NOT IN (SELECT path FROM libraries)")
        orphaned += cursor.rowcount
    db.commit()
    return orphaned


def prune_cache(db: sqlite3.Connection) -> int:
    """Drop cached renders that no longer belong to a remembered folder."""
    keep_ids = {row["id"] for row in db.execute("SELECT id FROM photos").fetchall()}
    removed = 0
    # Reclaim whatever older versions cached in tiers that no longer exist,
    # rather than leaving gigabytes sitting there unreferenced forever.
    for retired in RETIRED_CACHE_ROOTS:
        if not retired.is_dir():
            continue
        for path in retired.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        try:
            retired.rmdir()
        except OSError:
            pass
    for cache_root in (THUMB_ROOT, DISPLAY_ROOT):
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


def write_cached_jpeg(image, out: Path, max_edge: int | None) -> Path | None:
    """Resize an already display-oriented image and publish it atomically."""
    tmp = out.with_suffix(".tmp.jpg")
    try:
        tmp.unlink(missing_ok=True)
        if max_edge is not None:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image.convert("RGB").save(tmp, "JPEG", quality=85)
        tmp.replace(out)
        return out
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def cache_is_fresh(path: Path, out: Path) -> bool:
    try:
        return out.exists() and out.stat().st_mtime >= path.stat().st_mtime
    except OSError:
        return False


def trim_display_cache():
    """Keep the substitute render cache under budget, least-recent first."""
    try:
        entries = [(f.stat().st_atime, f.stat().st_size, f) for f in DISPLAY_ROOT.glob("*.jpg")]
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    if total <= DISPLAY_CACHE_BUDGET:
        return
    for _, size, path in sorted(entries):
        try:
            path.unlink()
            total -= size
        except OSError:
            continue
        if total <= DISPLAY_CACHE_BUDGET:
            return


def render_cached_jpeg(path: Path, out: Path, max_edge: int | None) -> Path | None:
    """Decode any supported image into a display-oriented cached JPEG."""
    if cache_is_fresh(path, out):
        return out
    if Image is None:
        return None
    try:
        with Image.open(path) as image:
            return write_cached_jpeg(ImageOps.exif_transpose(image), out, max_edge)
    except Exception:
        return None


def ensure_thumbnail(path: Path, pid: str) -> Path | None:
    return render_cached_jpeg(path, thumb_path_for(pid), THUMB_EDGE)


def ensure_display_render(path: Path, pid: str) -> Path | None:
    return render_cached_jpeg(path, display_path_for(pid), DISPLAY_EDGE)


FOCUS_TILE = 512
FOCUS_GRID = 3
FOCUS_REBLUR_TAPS = 9
FOCUS_EDGE_MIN_GRAD = 4.0
FOCUS_EDGE_COHERENCE = 2
FOCUS_MIN_EDGE_PIXELS = 300
FOCUS_POINT_WEIGHT = 0.8
FOCUS_COARSE_FACTOR = 3
FOCUS_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


# Comparator pairs of a 24-comparator median-of-9 sorting network. Sorting only
# far enough to place element 4 is what a 3x3 median needs, and whole-array
# min/max beats a partial sort of a stacked 9-plane array by ~12x.
MEDIAN9_NETWORK = (
    (0, 1), (3, 4), (6, 7), (1, 2), (4, 5), (7, 8), (0, 1), (3, 4), (6, 7),
    (0, 3), (3, 6), (0, 3), (1, 4), (4, 7), (1, 4), (2, 5), (5, 8), (2, 5),
    (1, 3), (5, 7), (2, 6), (4, 6), (2, 4), (2, 3),
)


def median_filter_3x3(arr):
    rows, cols = arr.shape
    padded = np.pad(arr, 1, mode="edge")
    planes = [padded[y : y + rows, x : x + cols] for y in range(3) for x in range(3)]
    for low, high in MEDIAN9_NETWORK:
        first, second = planes[low], planes[high]
        planes[low] = np.minimum(first, second)
        planes[high] = np.maximum(first, second)
    return planes[4]


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
    tile = np.asarray(tile, dtype=np.float32)
    padded = np.pad(tile, ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")
    acc = np.zeros((rows, cols), dtype=np.float32)
    for step in range(-half, half + 1):
        acc += padded[pad_y + dy * step : pad_y + dy * step + rows, pad_x + dx * step : pad_x + dx * step + cols]
    blurred = acc * np.float32(1.0 / FOCUS_REBLUR_TAPS)

    def central_gradient(arr):
        return np.abs(shifted_overlap(arr, dy, dx, 1) - shifted_overlap(arr, -dy, -dx, 1)) * np.float32(0.5)

    grad = central_gradient(tile)
    grad_blurred = central_gradient(blurred)
    mask = grad >= FOCUS_EDGE_MIN_GRAD
    mask_u8 = mask.astype(np.uint8)
    m_rows, m_cols = mask_u8.shape
    padded_mask = np.pad(mask_u8, 1)
    # 8-neighbour count as a separable 3x3 box sum minus the centre: six adds
    # over the whole tile instead of eight. Counts peak at 9, so uint8 holds.
    row_sums = padded_mask[:, 0:m_cols] + padded_mask[:, 1 : m_cols + 1] + padded_mask[:, 2 : m_cols + 2]
    neighbours = (row_sums[0:m_rows] + row_sums[1 : m_rows + 1] + row_sums[2 : m_rows + 2]) - mask_u8
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
    records = run_exiftool([*tags, str(path)], timeout=10)
    return records[0] if records else {}


def run_exiftool(arguments: list[str], timeout: int) -> list[dict]:
    """One exiftool run returning its JSON records.

    Arguments travel through a temporary -@ argfile rather than the command
    line: a hundred photo paths comfortably exceeds the 8191-character command
    line Windows allows, and exiftool would be truncated mid-list.
    """
    exiftool_cmd = find_exiftool()
    if not exiftool_cmd:
        return []
    argfile = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(arguments))
            argfile = handle.name
        proc = subprocess.run(
            # The argfile is UTF-8, but exiftool would otherwise decode the
            # paths inside it with the system code page — which mangles any
            # non-ASCII folder name on Windows.
            [*exiftool_cmd, "-charset", "FileName=UTF8", "-json", "-b", "-@", argfile],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=SUBPROCESS_FLAGS,
        )
    except Exception:
        return []
    finally:
        if argfile:
            try:
                os.unlink(argfile)
            except OSError:
                pass
    if not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def read_exiftool_batch(paths: list[Path], tags: list[str]) -> dict[str, dict]:
    """Tags for many photos from a single exiftool process, keyed by path.

    Spawning exiftool per photo costs ~69ms of process startup against ~15ms
    when a hundred photos share one run — on a card full of photos that
    difference alone is minutes.
    """
    records = run_exiftool([*tags, *(str(path) for path in paths)], timeout=30 + len(paths))
    by_path = {}
    for record in records:
        source = record.get("SourceFile")
        if source:
            # exiftool reports Windows paths with forward slashes; round-trip
            # through Path so the key matches what iterdir() handed us.
            by_path[str(Path(source))] = record
    return by_path


def decode_binary_tag(value) -> bytes | None:
    """Bytes behind exiftool's ``base64:`` encoding of a binary tag."""
    if not isinstance(value, str) or not value.startswith("base64:"):
        return None
    try:
        return base64.b64decode(value[len("base64:") :])
    except Exception:
        return None


# Embedded images an original may carry, largest first.
EMBEDDED_IMAGE_TAGS = ("OtherImage", "PreviewImage")
# Orientation value -> the transpose that brings pixels upright, matching the
# table ImageOps.exif_transpose applies.
EXIF_ROTATIONS = {
    2: "FLIP_LEFT_RIGHT",
    3: "ROTATE_180",
    4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE",
    6: "ROTATE_270",
    7: "TRANSVERSE",
    8: "ROTATE_90",
}


def thumbnail_from_record(path: Path, pid: str, record: dict) -> Path | None:
    """Grid thumbnail built from the original's embedded preview.

    Fuji HIF files carry a 1920x1280 JPEG that decodes in ~30ms and needs well
    under a megabyte off the card, where decoding the 26MP original costs
    ~340ms and reads the whole 5MB file. Returns None when the file has no
    preview big enough to stand in, leaving the caller to decode the original.
    """
    thumb = thumb_path_for(pid)
    if cache_is_fresh(path, thumb):
        return thumb
    if Image is None:
        return None
    for tag in EMBEDDED_IMAGE_TAGS:
        blob = decode_binary_tag(record.get(tag))
        if blob is None:
            continue
        try:
            with Image.open(io.BytesIO(blob)) as image:
                if max(image.size) < EMBEDDED_MIN_EDGE:
                    continue
                # Embedded previews carry no orientation of their own, so the
                # parent file's has to be applied by hand or every portrait
                # frame lands on its side.
                rotation = EXIF_ROTATIONS.get(record.get("Orientation"))
                if rotation:
                    image = image.transpose(getattr(Image.Transpose, rotation))
                return write_cached_jpeg(image, thumb, THUMB_EDGE)
        except Exception:
            continue
    return None


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
    # "#" asks for the raw numeric value, which is what EXIF_ROTATIONS keys on.
    "-Orientation#",
]
# Adds the embedded previews to the same pass, base64'd into the JSON by -b, so
# a fast import needs one process per batch instead of one per photo per pass.
EXIFTOOL_IMPORT_TAGS = [*EXIFTOOL_TAGS, *(f"-{tag}" for tag in EMBEDDED_IMAGE_TAGS)]


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


def extract_exiftool_metadata(path: Path, tags: dict | None = None) -> dict:
    """Display metadata from exiftool, reusing tags already read in a batch."""
    if tags is None:
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


def extract_exif(path: Path, tags: dict | None = None) -> dict:
    exiftool_metadata = extract_exiftool_metadata(path, tags)
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


def apply_focus_risk_flags(db: sqlite3.Connection, library: Path) -> float:
    """Flag photos whose score is a low outlier within the scanned batch."""
    rows = db.execute(
        "SELECT id, blur_score, warnings FROM photos WHERE directory = ?", (str(library),)
    ).fetchall()
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


def load_focus_cache(db: sqlite3.Connection, library: Path) -> dict[str, tuple[float, int, dict]]:
    """Previously computed focus results for this folder, keyed by path."""
    rows = db.execute(
        "SELECT path, mtime, size, focus_json FROM analysis_cache WHERE directory = ?",
        (str(library),),
    ).fetchall()
    return {row["path"]: (row["mtime"], row["size"], json.loads(row["focus_json"] or "{}")) for row in rows}


def cached_focus(focus_cache: dict, path: Path) -> dict | None:
    """Cached focus result, but only if the file has not changed since."""
    entry = focus_cache.get(str(path))
    if entry is None:
        return None
    mtime, size, focus = entry
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_mtime != mtime or stat.st_size != size:
        return None
    return focus


def store_focus(db: sqlite3.Connection, path: Path, focus: dict | None):
    try:
        stat = path.stat()
    except OSError:
        return
    db.execute(
        """
        INSERT INTO analysis_cache (path, directory, mtime, size, focus_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            directory=excluded.directory,
            mtime=excluded.mtime,
            size=excluded.size,
            focus_json=excluded.focus_json,
            updated_at=excluded.updated_at
        """,
        (
            str(path),
            str(path.parent),
            stat.st_mtime,
            stat.st_size,
            json.dumps(focus or {}),
            time.time(),
        ),
    )


def apply_focus(metadata: dict, focus: dict | None) -> float | None:
    """Fold an analysis result into a photo's metadata, returning its score."""
    if not focus:
        return None
    metadata["sharpMax"] = focus.get("sharpMax")
    metadata["sharpFocus"] = focus.get("sharpFocus")
    return focus.get("score")


def import_photo(path: Path, raw_path: Path | None, record: dict, focus: dict | None) -> dict:
    """Row for one photo without decoding the original.

    The thumbnail comes from the embedded preview and the metadata from tags
    the batch already read, so this touches well under a megabyte per photo.
    Focus analysis is deferred unless a previous scan already produced it.
    """
    pid = photo_id(path)
    thumb = thumbnail_from_record(path, pid, record) or ensure_thumbnail(path, pid)
    metadata = extract_exif(path, record)
    blur_score = apply_focus(metadata, focus)
    width = None
    height = None
    if thumb and Image is not None:
        try:
            with Image.open(thumb) as image:
                width, height = image.size
        except Exception:
            pass
    created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
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
        "blur_score": blur_score,
        "metadata_json": json.dumps(metadata),
        "warnings": json.dumps(warnings_for(raw_path)),
        "analysis_state": "done" if focus is not None else "pending",
        "updated_at": time.time(),
    }


UPSERT_PHOTO = """
    INSERT INTO photos (
        id, path, directory, stem, ext, raw_path, width, height,
        created_at, blur_score, metadata_json, status, warnings, analysis_state, updated_at
    )
    VALUES (
        :id, :path, :directory, :stem, :ext, :raw_path, :width, :height,
        :created_at, :blur_score, :metadata_json, :status, :warnings, :analysis_state, :updated_at
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
        analysis_state=excluded.analysis_state,
        updated_at=excluded.updated_at
"""


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


def scan_cancelled() -> bool:
    with SCAN_LOCK:
        return not SCAN_JOB["running"]


def pending_count(db: sqlite3.Connection, library: Path) -> int:
    return db.execute(
        "SELECT COUNT(*) AS count FROM photos WHERE directory = ? AND analysis_state = 'pending'",
        (str(library),),
    ).fetchone()["count"]


def import_photos(db: sqlite3.Connection, library: Path, display_files: list[Path], raw_by_key: dict, workers: int) -> int:
    """Stage one: thumbnails and metadata, committed batch by batch.

    Focus analysis is left to stage two so the grid starts filling within a
    second or two of picking a folder rather than after the whole card has
    been analysed.
    """
    focus_cache = load_focus_cache(db, library)
    head, tail = display_files[:EXIFTOOL_FIRST_BATCH], display_files[EXIFTOOL_FIRST_BATCH:]
    batches = ([head] if head else []) + [tail[index : index + EXIFTOOL_BATCH] for index in range(0, len(tail), EXIFTOOL_BATCH)]
    imported = 0
    update_scan_job(stage="import", done=0, total=len(display_files), message="Importing")
    with ThreadPoolExecutor(max_workers=EXIFTOOL_CONCURRENCY) as tag_pool, ThreadPoolExecutor(
        max_workers=max(1, workers)
    ) as render_pool:
        queue: deque = deque()
        next_batch = 0

        def fill_queue():
            # Bounded look-ahead: a batch in flight holds ~50MB of base64
            # previews, so reading the whole card ahead would not fit.
            nonlocal next_batch
            while len(queue) < EXIFTOOL_CONCURRENCY + 1 and next_batch < len(batches):
                batch = batches[next_batch]
                queue.append((batch, tag_pool.submit(read_exiftool_batch, batch, EXIFTOOL_IMPORT_TAGS)))
                next_batch += 1

        fill_queue()
        while queue:
            wait_for_scan_resume()
            if scan_cancelled():
                break
            batch, future = queue.popleft()
            records = future.result()
            fill_queue()

            def build(path: Path, records=records) -> dict | None:
                # records is bound now, not at call time: the next batch
                # rebinds it while these tasks may still be in flight.
                key = (str(path.parent), path.stem.lower())
                try:
                    return import_photo(
                        path,
                        raw_by_key.get(key),
                        records.get(str(path), {}),
                        cached_focus(focus_cache, path),
                    )
                except Exception:
                    # A card pulled mid-scan should cost one photo, not the run.
                    return None

            # Render the whole batch before opening the write transaction, so
            # the UI's polling reads are never blocked behind decode work.
            items = [item for item in render_pool.map(build, batch) if item is not None]
            for item in items:
                item["status"] = saved_status(db, item["id"], item["path"])
                db.execute(UPSERT_PHOTO, item)
                imported += 1
            db.commit()
            update_scan_job(done=imported, message="Importing")
    return imported


def analysis_workers(workers: int) -> int:
    """Concurrency for focus analysis, which saturates below the core count.

    Each photo in flight holds a 26MP grayscale frame plus tile-sized
    temporaries, so the stage is bound by memory bandwidth rather than cores
    and extra threads stop paying off. Interleaved A/B runs on an 8-core
    machine put half the cores ~8% ahead (median 502 vs 547 ms/photo); a
    process pool was tried too and landed within noise of threads, so the
    GIL is not what limits this. The import stage is subprocess- and I/O-bound
    and keeps the full worker count.
    """
    return max(1, min(workers, (os.cpu_count() or 4) // 2))


def analyze_photo(pid: str, path: Path, metadata: dict) -> tuple[str, Path, dict, dict | None]:
    focus_pixel = metadata.get("focusPixel")
    focus = analyze_focus(path, tuple(focus_pixel) if focus_pixel else None)
    return pid, path, metadata, focus


def analyze_photos(db: sqlite3.Connection, library: Path, workers: int) -> int:
    """Stage two: focus scores for photos the import left pending."""
    rows = db.execute(
        """
        SELECT id, path, metadata_json FROM photos
        WHERE directory = ? AND analysis_state = 'pending'
        ORDER BY path
        """,
        (str(library),),
    ).fetchall()
    if not rows:
        return 0
    update_scan_job(stage="analyze", done=0, total=len(rows), message="Analysing focus")
    analyzed = 0
    futures = {}
    worker_count = analysis_workers(workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        remaining = iter(rows)

        def submit_next() -> bool:
            row = next(remaining, None)
            if row is None:
                return False
            path = Path(row["path"])
            metadata = json.loads(row["metadata_json"] or "{}")
            futures[executor.submit(analyze_photo, row["id"], path, metadata)] = row["id"]
            return True

        for _ in range(worker_count):
            if not submit_next():
                break

        while futures:
            done_futures, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done_futures:
                futures.pop(future, None)
                wait_for_scan_resume()
                pid, path, metadata, focus = future.result()
                blur_score = apply_focus(metadata, focus)
                db.execute(
                    """
                    UPDATE photos
                    SET blur_score = ?, metadata_json = ?, analysis_state = 'done', updated_at = ?
                    WHERE id = ?
                    """,
                    (blur_score, json.dumps(metadata), time.time(), pid),
                )
                store_focus(db, path, focus)
                analyzed += 1
                if analyzed % 25 == 0:
                    db.commit()
                    update_scan_job(done=analyzed)
                if scan_cancelled():
                    break
                wait_for_scan_resume()
                submit_next()
            if scan_cancelled():
                break
    db.commit()
    return analyzed


def record_orphan_raws(db: sqlite3.Connection, library: Path, orphan_raws: list[Path]):
    db.execute("DELETE FROM orphan_raws WHERE directory = ?", (str(library),))
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


def drop_missing_photos(db: sqlite3.Connection, library: Path, display_files: list[Path]):
    """Forget photos this folder no longer holds, keeping other folders intact."""
    present = {photo_id(path) for path in display_files}
    stale = [
        (row["id"],)
        for row in db.execute("SELECT id FROM photos WHERE directory = ?", (str(library),)).fetchall()
        if row["id"] not in present
    ]
    if stale:
        db.executemany("DELETE FROM photos WHERE id = ?", stale)
    db.commit()


def scan_summary(db: sqlite3.Connection, library: Path, scanned: int, started: float) -> dict:
    directory = str(library)
    total = db.execute("SELECT COUNT(*) AS count FROM photos WHERE directory = ?", (directory,)).fetchone()["count"]
    paired = db.execute(
        "SELECT COUNT(*) AS count FROM photos WHERE directory = ? AND raw_path IS NOT NULL", (directory,)
    ).fetchone()["count"]
    focus_risk = db.execute(
        """
        SELECT COUNT(*) AS count FROM photos
        WHERE directory = ? AND (warnings LIKE '%focus_risk%' OR warnings LIKE '%soft%')
        """,
        (directory,),
    ).fetchone()["count"]
    orphan_count = db.execute(
        "SELECT COUNT(*) AS count FROM orphan_raws WHERE directory = ?", (directory,)
    ).fetchone()["count"]
    return {
        "scanned": scanned,
        "total": total,
        "paired": paired,
        "focusRisk": focus_risk,
        "orphanRaws": orphan_count,
        "pending": pending_count(db, library),
        "seconds": round(time.time() - started, 2),
    }


def scan_library(library: Path, workers: int = DEFAULT_WORKERS, analyze: bool = True) -> dict:
    if not library.exists() or not library.is_dir():
        raise ValueError(f"Library folder does not exist: {library}")
    display_files, raw_by_key = scan_files(library)
    display_keys = {(str(path.parent), path.stem.lower()) for path in display_files}
    orphan_raws = [path for key, path in raw_by_key.items() if key not in display_keys]
    started = time.time()
    db = connect()
    remember_library(db, library)
    drop_missing_photos(db, library, display_files)
    update_scan_job(running=True, paused=False, stage="import", done=0, total=len(display_files))

    import_photos(db, library, display_files, raw_by_key, workers)
    # Only prune once the folder's own rows exist, or this scan's thumbnails
    # would be swept away as orphans and rendered a second time.
    prune_cache(db)
    record_orphan_raws(db, library, orphan_raws)

    if analyze and not scan_cancelled():
        analyze_photos(db, library, workers)
    # Runs even for a browse-only scan: the import rebuilds every warnings
    # list, so cached scores from an earlier analysis need their risk flags
    # put back.
    update_scan_job(message="Flagging focus risk")
    apply_focus_risk_flags(db, library)
    db.commit()

    update_scan_job(message="Finalizing")
    result = scan_summary(db, library, len(display_files), started)
    db.close()
    update_scan_job(running=False, paused=False, stage="idle", result=result, message="Complete", pending=result["pending"])
    return result


def analyze_library(library: Path, workers: int = DEFAULT_WORKERS) -> dict:
    """Run focus analysis over whatever a browse-only import left pending."""
    started = time.time()
    db = connect()
    update_scan_job(running=True, paused=False, stage="analyze", done=0, total=0)
    analyze_photos(db, library, workers)
    update_scan_job(message="Flagging focus risk")
    apply_focus_risk_flags(db, library)
    db.commit()
    result = scan_summary(db, library, 0, started)
    db.close()
    update_scan_job(running=False, paused=False, stage="idle", result=result, message="Complete", pending=result["pending"])
    return result


def start_job(work) -> dict:
    with SCAN_LOCK:
        if SCAN_JOB["running"]:
            return {"started": False, "job": dict(SCAN_JOB)}
        SCAN_JOB.update(
            {
                "running": True,
                "paused": False,
                "stage": "starting",
                "done": 0,
                "total": 0,
                "message": "Starting",
                "result": None,
                "error": None,
            }
        )

    def runner():
        try:
            work()
        except Exception as exc:
            update_scan_job(running=False, paused=False, stage="idle", error=str(exc), message="Failed")

    threading.Thread(target=runner, daemon=True).start()
    return {"started": True, "job": dict(SCAN_JOB)}


def start_scan(library: Path, workers: int = DEFAULT_WORKERS, analyze: bool = True) -> dict:
    return start_job(lambda: scan_library(library, workers, analyze))


def start_analysis(library: Path, workers: int = DEFAULT_WORKERS) -> dict:
    return start_job(lambda: analyze_library(library, workers))


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
        "displayUrl": f"/api/photos/{row['id']}/display",
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
    # The import already read every one of these tags in a batched exiftool
    # pass, so re-reading per photo would spend ~70ms of process startup on
    # each step through the viewer. Only rows left by an older catalog, which
    # never stored metadata, still need the per-file read.
    if any(cached.get(key) for key in ("iso", "shutter", "aperture", "capturedAt")):
        cached.setdefault("brandBadges", [])
        db.close()
        return cached
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


def display_render(pid: str) -> Path:
    db = connect()
    row = db.execute("SELECT path FROM photos WHERE id = ?", (pid,)).fetchone()
    db.close()
    if row is None:
        raise ValueError("Photo not found")
    path = Path(row["path"])
    if not path.exists():
        raise ValueError("Source photo not found")
    render = ensure_display_render(path, pid)
    if render is None or not render.exists():
        raise ValueError("Could not generate display render")
    trim_display_cache()
    return render


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
    try:
        with path.open("rb") as file:
            shutil.copyfileobj(file, handler.wfile)
    except (BrokenPipeError, ConnectionResetError):
        # The viewer abandons an original the moment it decides it cannot
        # decode it. Nothing left to say on a socket the client has closed.
        pass


def list_photos(query: dict, library: Path) -> dict:
    status = query.get("status", [""])[0]
    warning = query.get("warning", [""])[0]
    search = query.get("search", [""])[0].strip().lower()
    limit = min(int(query.get("limit", ["300"])[0]), 1000)
    offset = int(query.get("offset", ["0"])[0])
    # The catalog now spans several remembered folders, so every query has to
    # narrow itself to the one on screen.
    clauses = ["directory = ?"]
    params: list[object] = [str(library)]
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
    stats_rows = db.execute(
        "SELECT status, COUNT(*) AS count FROM photos WHERE directory = ? GROUP BY status",
        (str(library),),
    ).fetchall()
    pending = pending_count(db, library)
    db.close()
    return {
        "photos": [row_to_photo(row) for row in rows],
        "count": count,
        "pending": pending,
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


def list_orphan_raws(query: dict, library: Path) -> dict:
    search = query.get("search", [""])[0].strip().lower()
    limit = min(int(query.get("limit", ["300"])[0]), 1000)
    offset = int(query.get("offset", ["0"])[0])
    clauses = ["directory = ?"]
    params: list[object] = [str(library)]
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
    rows = db.execute(
        "SELECT * FROM photos WHERE status = 'reject' AND directory = ?", (str(library),)
    ).fetchall()
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
    rows = db.execute("SELECT * FROM orphan_raws WHERE directory = ?", (str(library),)).fetchall()
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
        if not path.startswith(("/thumbs/", "/display/", "/api/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def translate_path(self, path):
        parsed = urlparse(path)
        if parsed.path.startswith("/thumbs/"):
            name = Path(unquote(parsed.path.replace("/thumbs/", ""))).name
            return str(THUMB_ROOT / name)
        if parsed.path.startswith("/display/"):
            name = Path(unquote(parsed.path.replace("/display/", ""))).name
            return str(DISPLAY_ROOT / name)
        if parsed.path == "/":
            return str(WEB_ROOT / "index.html")
        return str(WEB_ROOT / parsed.path.lstrip("/"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/photos":
            if not self.library_selected:
                json_response(self, {"photos": [], "count": 0, "pending": 0, "stats": {}})
                return
            try:
                json_response(self, list_photos(parse_qs(parsed.query), self.library))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/orphan-raws":
            if not self.library_selected:
                json_response(self, {"orphanRaws": [], "count": 0})
                return
            try:
                json_response(self, list_orphan_raws(parse_qs(parsed.query), self.library))
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
        if parsed.path.startswith("/api/photos/") and parsed.path.endswith("/display"):
            try:
                pid = parsed.path.split("/")[3]
                self.path = f"/display/{display_render(pid).name}"
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
                analyze = bool(payload.get("analyze", True))
                json_response(self, start_scan(library, self.workers, analyze))
                return
            if parsed.path == "/api/analyze":
                if not self.library_selected:
                    json_response(self, {"error": "No library selected"}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, start_analysis(self.library, self.workers))
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
