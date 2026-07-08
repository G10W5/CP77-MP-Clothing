"""
make_icon_sheet.py — Standalone icon sheet generator for CP77 clothing mods.

Combines per-color icon PNGs into a single sprite sheet (.xbm) with a
matching inkatlas coordinate map (.inkatlas.json).  Designed to be called
by cpmp.py's custom icon mode, but can also be run standalone.

Usage (standalone):
    python make_icon_sheet.py <input_dir> <item_name> <mod_base> <output_dir> <cli_path>

Usage (imported):
    from make_icon_sheet import make_icon_sheet
    atlas_data = make_icon_sheet(input_dir, item_name, mod_base, output_dir, cli_path)

Dependencies: Pillow (pip install Pillow)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow is required.  Install it with:  pip install Pillow", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ICON_SLOT_SIZE = 160          # pixels per icon slot (community standard)
MAX_SHEET_WIDTH = 2048        # wrap to next row beyond this width
_ICON_TEX_RE = re.compile(r'_icon_([a-z0-9_]+)$', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _header():
    from datetime import datetime
    return {
        "WolvenKitVersion": "8.18.1",
        "WKitJsonVersion": "0.0.9",
        "GameVersion": 2310,
        "ExportedDateTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "DataType": "CR2W",
        "ArchiveFileName": ""
    }


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def make_icon_sheet(
    input_dir: Path,
    item_name: str,
    mod_base: str,
    output_dir: Path,
    cli_path: Path | None = None,
) -> dict | None:
    """Combine per-color icon PNGs into a single sprite sheet + inkatlas.

    Parameters
    ----------
    input_dir : Path
        Directory containing ``{item_name}_icon_{color}.png`` files.
    item_name : str
        Item identifier (e.g. ``"flowery_dress"``).
    mod_base : str
        Mod base ID used in depot paths (e.g. ``"gothic_dress"``).
    output_dir : Path
        Where to write the ``.inkatlas.json`` and combined PNG.
    cli_path : Path, optional
        Path to ``WolvenKit.CLI.exe``.  If provided, the combined PNG is
        automatically imported to ``.xbm`` via the CLI.

    Returns
    -------
    dict or None
        The inkatlas JSON dict (ready to be written to ``.inkatlas.json``),
        or ``None`` if no icon PNGs were found.
    """

    # ------------------------------------------------------------------
    # 1. Discover per-color icon PNGs
    # ------------------------------------------------------------------
    icon_pngs: dict[str, Path] = {}
    for png in input_dir.rglob('*.png'):
        m = _ICON_TEX_RE.search(png.stem)
        if m:
            icon_pngs[m.group(1).lower()] = png
    for jpg in input_dir.rglob('*.jpg'):
        m = _ICON_TEX_RE.search(jpg.stem)
        if m:
            icon_pngs[m.group(1).lower()] = jpg
    for jpg in input_dir.rglob('*.jpeg'):
        m = _ICON_TEX_RE.search(jpg.stem)
        if m:
            icon_pngs[m.group(1).lower()] = jpg

    if not icon_pngs:
        return None

    # ------------------------------------------------------------------
    # 2. Load images, determine slot dimensions
    # ------------------------------------------------------------------
    images: dict[str, Image.Image] = {}
    slot_w, slot_h = ICON_SLOT_SIZE, ICON_SLOT_SIZE

    for color, png_path in sorted(icon_pngs.items()):
        try:
            img = Image.open(png_path).convert('RGBA')
        except Exception:
            continue
        # Resize to slot size if needed
        if img.size != (slot_w, slot_h):
            img = img.resize((slot_w, slot_h), Image.LANCZOS)
        images[color] = img

    if not images:
        return None

    # ------------------------------------------------------------------
    # 3. Arrange icons in a horizontal row (wrap at MAX_SHEET_WIDTH)
    # ------------------------------------------------------------------
    n_icons = len(images)
    cols = min(n_icons, MAX_SHEET_WIDTH // slot_w)
    if cols < 1:
        cols = 1
    rows = (n_icons + cols - 1) // cols

    sheet_w = cols * slot_w
    sheet_h = rows * slot_h
    sheet = Image.new('RGBA', (sheet_w, sheet_h), (0, 0, 0, 0))

    # Map color → position for UV calculation
    color_positions: dict[str, tuple[int, int]] = {}
    for idx, color in enumerate(sorted(images.keys())):
        col = idx % cols
        row = idx // cols
        x = col * slot_w
        y = row * slot_h
        sheet.paste(images[color], (x, y))
        color_positions[color] = (x, y)

    # ------------------------------------------------------------------
    # 4. Save combined PNG to output_dir (temporary, for CLI import)
    # ------------------------------------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    combined_png_path = output_dir / f"{item_name}_icons.png"
    sheet.save(str(combined_png_path))

    # ------------------------------------------------------------------
    # 5. Import to .xbm via WolvenKit CLI (if path provided)
    # ------------------------------------------------------------------
    if cli_path and cli_path.is_file():
        cmd = [str(cli_path), 'import', str(combined_png_path), '--outpath', str(output_dir)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            pass  # best-effort; caller can import manually

    # ------------------------------------------------------------------
    # 6. Build inkatlas JSON
    # ------------------------------------------------------------------
    parts = []
    for color in sorted(color_positions.keys()):
        x, y = color_positions[color]
        part = {
            "$type": "inkTextureAtlasMapper",
            "clippingRectInPixels": {
                "$type": "Rect",
                "bottom": y + slot_h,
                "left": x,
                "right": x + slot_w,
                "top": y
            },
            "clippingRectInUVCoords": {
                "$type": "RectF",
                "Bottom": (y + slot_h) / sheet_h,
                "Left": x / sheet_w,
                "Right": (x + slot_w) / sheet_w,
                "Top": y / sheet_h
            },
            "partName": {
                "$type": "CName",
                "$storage": "string",
                "$value": f"{item_name}_{color}"
            }
        }
        parts.append(part)

    slot = {
        "$type": "inkTextureSlot",
        "parts": parts,
        "slices": [],
        "texture": {
            "DepotPath": {
                "$type": "ResourcePath",
                "$storage": "string",
                "$value": f"{mod_base}\\{item_name}\\{item_name}_icons.xbm"
            },
            "Flags": "Soft"
        }
    }

    atlas = {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "inkTextureAtlas",
                "activeTexture": "StaticTexture",
                "cookingPlatform": "PLATFORM_PC",
                "dynamicTexture": {
                    "DepotPath": {
                        "$type": "ResourcePath",
                        "$storage": "uint64",
                        "$value": "0"
                    },
                    "Flags": "Soft"
                },
                "dynamicTextureSlot": {
                    "$type": "inkDynamicTextureSlot",
                    "parts": [],
                    "texture": {
                        "DepotPath": {
                            "$type": "ResourcePath",
                            "$storage": "uint64",
                            "$value": "0"
                        },
                        "Flags": "Soft"
                    }
                },
                "isSingleTextureMode": 1,
                "parts": [],
                "slices": [],
                "slots": {
                    "Elements": [slot]
                },
                "texture": {
                    "DepotPath": {
                        "$type": "ResourcePath",
                        "$storage": "uint64",
                        "$value": "0"
                    },
                    "Flags": "Soft"
                },
                "textureResolution": "UltraHD_3840_2160"
            },
            "EmbeddedFiles": []
        }
    }

    return atlas


# ---------------------------------------------------------------------------
# CLI entry point (standalone usage)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) < 5:
        print("Usage: python make_icon_sheet.py <input_dir> <item_name> <mod_base> <output_dir> [cli_path]")
        print("  input_dir  — folder containing {item}_icon_{color}.png files")
        print("  item_name  — item identifier (e.g. flowery_dress)")
        print("  mod_base   — mod base ID (e.g. gothic_dress)")
        print("  output_dir — where to write .inkatlas.json and combined .png")
        print("  cli_path   — (optional) path to WolvenKit.CLI.exe for auto .xbm import")
        sys.exit(1)

    in_dir = Path(sys.argv[1])
    item = sys.argv[2]
    base = sys.argv[3]
    out_dir = Path(sys.argv[4])
    cli = Path(sys.argv[5]) if len(sys.argv) > 5 else None

    result = make_icon_sheet(in_dir, item, base, out_dir, cli)
    if result is None:
        print(f"No icon images found in {in_dir}")
        sys.exit(1)

    atlas_path = out_dir / f"{item}_icons.inkatlas.json"
    with open(atlas_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f"✓ Wrote {atlas_path} ({len(result['Data']['RootChunk']['parts'])} slots)")
