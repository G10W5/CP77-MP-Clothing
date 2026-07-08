#!/usr/bin/env python3
"""
CP77 Modding Pipeline Master (CPMP) v4.7
=========================================
Two-Entity Multi-Slot Pipeline Compiler for Cyberpunk 2077 modding.
Automates the complete file loop between folder-structured Blender GLB exports
and a fully initialized, double-clickable WolvenKit project folder.

Implements:
- REDModding Wiki Two-Entity architecture (.ent / .app / .mesh mapping).
- Full Equipment-EX Dynamic Slot Override Injection.
- Interactive Scrollable Item Manager (Visual checklists, slot drop-downs).
- Automated Mesh Material and Texture Mapping Engine.
- Procedural in-game coordinate tiled Inventory Icon (.inkatlas) generator.
"""


import copy
import json
import queue
import re
import shutil
import struct
import subprocess
import sys
import textwrap
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

from cr2w_mesh import import_glb_to_mesh, get_glb_material_names
from make_icon_sheet import make_icon_sheet

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ═══════════════════════════════════════════════════════════════════════════════
# DESIGN TOKENS & SYSTEM COLOURS
# ═══════════════════════════════════════════════════════════════════════════════

C = {
    'bg_root':       '#08080e',
    'bg_dark':       '#0c0c16',
    'bg_panel':      '#121220',
    'bg_panel_alt':  '#16162a',
    'bg_input':      '#1a1a30',
    'bg_hover':      '#222240',
    'bg_console':    '#0a0a10',
    'border':        '#28284a',
    'border_light':  '#35356a',
    'text':          '#e2e2f0',
    'text_dim':      '#7a7a9e',
    'text_label':    '#b0b0d0',
    'text_heading':  '#ffffff',
    'cyan':          '#00d4ff',
    'purple':        '#9b5fff',
    'green':         '#00ff88',
    'orange':        '#ff9f1a',
    'red':           '#ff3860',
    'yellow':        '#ffe066',
    'blue':          '#4d8cff',
    'btn_primary':   '#0d3860',
    'btn_run':       '#143a1e',
    'btn_compile':   '#3d2a08',
    'btn_scan':      '#1a1a50',
}

FONT = 'Segoe UI'
FONT_MONO = 'Cascadia Code'
VERSION = '4.7.1'

# Filename patterns used to auto-classify raw texture PNGs dropped in an
# item's Blender export folder, so the pipeline can rename/route them into
# the @dynamic material convention automatically instead of requiring the
# modder to hand-place pre-converted .xbm files.
#   {anything}_color_{variant}.png  -> diffuse map for that color variant
#   {anything}_n.png / _normal.png  -> normal map
#   {anything}_r.png / _roughness.png -> roughness map
#   {anything}_m.png / _metal(lic|ness).png -> metalness map
#   {anything}_icon_{color}.png     -> per-color inventory icon (handled
#                                      separately from material maps)
_COLOR_TEX_RE = re.compile(r'_color_([a-z0-9_]+)$', re.IGNORECASE)
_NORMAL_TEX_RE = re.compile(r'_(?:n|normal)$', re.IGNORECASE)
_ROUGH_TEX_RE = re.compile(r'_(?:r|roughness)$', re.IGNORECASE)
_METAL_TEX_RE = re.compile(r'_(?:m|metal(?:lic|ness)?)$', re.IGNORECASE)
_OPACITY_TEX_RE = re.compile(r'_(?:o|opacity)$', re.IGNORECASE)
_ICON_TEX_RE = re.compile(r'_icon_([a-z0-9_]+)$', re.IGNORECASE)

# Supported image file extensions (WolvenKit CLI handles all of these natively)
IMAGE_EXTS = ('*.png', '*.jpg', '*.jpeg')


def _find_images(directory: Path) -> list[Path]:
    """Find all supported image files in a directory (recursive)."""
    results = []
    for ext in IMAGE_EXTS:
        results.extend(directory.rglob(ext))
    return sorted(results)


def _png_dimensions(path: Path):
    """Read a PNG's (width, height) straight out of its IHDR chunk without
    needing Pillow or any other dependency. Returns None if the file isn't
    a readable PNG."""
    try:
        with open(path, 'rb') as f:
            header = f.read(24)
        if len(header) < 24 or header[:8] != b'\x89PNG\r\n\x1a\n':
            return None
        width, height = struct.unpack('>II', header[16:24])
        return width, height
    except Exception:
        return None


# Body suffixes for ArchiveXL {body} dynamic substitution.
# Full catalog of body suffixes CPMP knows how to recognize/generate.
# Longer/more-specific suffixes are listed first so filename matching in
# sync_glbs() (which checks endswith in order) doesn't let a short suffix
# like '_rb' shadow a longer one like '_ebbrb'.
BODY_SUFFIX_CATALOG = [
    ('_ebbprb',  'ebbprb'),   # modded — Hyst EBBP + Realistic Butt
    ('_ebbrb',   'ebbrb'),    # modded — Hyst EBB + Realistic Butt
    ('_ebbp',    'ebbp'),     # modded — Hyst EBBP
    ('_angel',   'angel'),    # modded — Hyst Angel
    ('_ebb',     'ebb'),      # modded — Hyst EBB
    ('_eve',     'eve'),      # modded — Hyst EVE
    ('_rb',      'rb'),       # modded — Hyst Realistic Butt
    ('_wb',      'wb'),       # vanilla — Woman Average (default Female V)
    ('_base',    'wb'),       # Blender export convention → maps to vanilla wb
    ('_mb',      'mb'),       # vanilla — Man Average (default Male V)
    # Future male body mods (Adonis, Atlas, Gymfiend) can be added here
    # if the tool gains enough popularity to warrant it.
]

BODY_TYPE_TOOLTIPS = {
    'ebbprb': 'Hyst EBBP + Realistic Butt — Enhanced Big Breasts Push Up with Realistic Butt shape',
    'ebbrb':  'Hyst EBB + Realistic Butt — Enhanced Big Breasts with Realistic Butt shape',
    'ebbp':   'Hyst EBBP — Enhanced Big Breasts Push Up (cleavage-focused bust shape)',
    'angel':  'Hyst Angel — Distinct body redesign by LxRHyst (2024)',
    'ebb':    'Hyst EBB — Enhanced Big Breasts (larger breasts with improved cleavage)',
    'eve':    'Hyst EVE — Stellar Blade-inspired body by LxRHyst (2026)',
    'rb':     'Hyst RB — Realistic Butt (enhanced lower body only, no breast change)',
    'wb':     'Vanilla Woman Average — Default Female V body (player_female_average)',
    'mb':     'Vanilla Man Average — Default Male V body (player_man_average)',
}


class ToolTip:
    """Simple hover tooltip for Tkinter widgets."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind('<Enter>', self.show)
        widget.bind('<Leave>', self.hide)

    def show(self, event=None):
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, bg=C['bg_input'], fg=C['text_label'],
                         font=(FONT, 7), relief='solid', borderwidth=1, padx=6, pady=3)
        label.pack()

    def hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

# Which body types actually get generated by default. Previously this was a
# hardcoded 13-entry list baked straight into the pipeline, which meant every
# item got meshes for every body type whether you wanted them or not. Now
# it's just the default value of the "Body Types" field in the UI, so it's
# editable per-project instead of requiring a code change.
DEFAULT_BODY_TOKENS = "ebbprb, ebbrb, angel, eve, rb"

def resolve_body_suffixes(raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Turn a comma-separated string of body tokens (e.g. from the UI's Body
    Types field) into the [(old_suffix, new_suffix), ...] pairs the pipeline
    actually iterates over, preserving BODY_SUFFIX_CATALOG's priority order
    (longer/more-specific suffixes first) regardless of the order the user
    typed them in. Returns (resolved_pairs, unknown_tokens) so the caller can
    warn about any typos instead of silently dropping them."""
    wanted = {t.strip().lower() for t in raw.split(',') if t.strip()}
    resolved = [pair for pair in BODY_SUFFIX_CATALOG if pair[1] in wanted]
    unknown = sorted(wanted - {pair[1] for pair in BODY_SUFFIX_CATALOG})
    return resolved, unknown


def extract_mat_settings(w: dict) -> tuple[list[str], dict]:
    """Extract two_sided_materials list and material_settings dict from
    an item_widgets entry's mat_settings_vars. Returns (two_sided_list, mat_settings_dict)."""
    mat_vars = w.get('mat_settings_vars', {})
    two_sided = []
    mat_settings = {}
    for mat_name, vs in mat_vars.items():
        transparent = vs['transparent_var'].get()
        ts = vs['two_sided_var'].get()
        mat_settings[mat_name] = {'transparent': transparent, 'two_sided': ts}
        if ts:
            two_sided.append(mat_name)
    return two_sided, mat_settings

# Maps an item's slot to its template_meshes/ subfolder name. Single source of
# truth used by both the mesh-build step and the texture-copy step so they can
# never disagree about which donor template a given item should use.
SLOT_DIR_MAP = {
    'GenericLegClothing': 'Legs',
    'GenericInnerChestClothing': 'Inner Torso',
    'GenericOuterChestClothing': 'Outer Torso',
    'GenericFootClothing': 'Feet',
    'GenericHeadClothing': 'Head',
    'GenericFaceClothing': 'Face',
    'Outfit': 'Special Accessories',
}

# Standard CDPR Vanilla Base Slots
BASE_SLOTS = [
    ("GenericInnerChestClothing", "Inner Torso / Shirt"),
    ("GenericOuterChestClothing", "Outer Torso / Jacket"),
    ("GenericLegClothing", "Legs / Pants / Skirts"),
    ("GenericFootClothing", "Feet / Shoes / Boots"),
    ("GenericHeadClothing", "Head / Hats / Helmets"),
    ("GenericFaceClothing", "Face / Glasses / Masks"),
    ("Outfit", "Special / Underwear / Bodysuits"),
]

# Comprehensive Equipment-EX Custom Slots
EQUIPMENT_EX_SLOTS = [
    ("None", "Disable Equipment-EX Slot (Vanilla Only)"),
    ("OutfitSlots.Head", "Head (Hats, Hijabs, Helmets)"),
    ("OutfitSlots.Balaclava", "Balaclava"),
    ("OutfitSlots.Mask", "Face Mask"),
    ("OutfitSlots.Glasses", "Glasses / Visors"),
    ("OutfitSlots.Eyes", "Eyes (Lenses)"),
    ("OutfitSlots.EyeLeft", "Left Eye Accessory"),
    ("OutfitSlots.EyeRight", "Right Eye Accessory"),
    ("OutfitSlots.Wreath", "Wreaths"),
    ("OutfitSlots.EarLeft", "Left Ear (Earrings)"),
    ("OutfitSlots.EarRight", "Right Ear (Earrings)"),
    ("OutfitSlots.Neckwear", "Neckwear (Scarves, Collars)"),
    ("OutfitSlots.NecklaceTight", "Chokers"),
    ("OutfitSlots.NecklaceShort", "Short Necklaces"),
    ("OutfitSlots.NecklaceLong", "Long Necklaces"),
    ("OutfitSlots.TorsoUnder", "Torso Underwear (Bras, Tight Tops)"),
    ("OutfitSlots.TorsoInner", "Torso Inner (T-Shirts, Tight Dresses)"),
    ("OutfitSlots.TorsoMiddle", "Torso Middle (Loose Shirts, Blazers)"),
    ("OutfitSlots.TorsoOuter", "Torso Outer (Jackets, Coats)"),
    ("OutfitSlots.TorsoAux", "Torso Aux (Vests, Harnesses)"),
    ("OutfitSlots.Back", "Back (Backpacks, Swords)"),
    ("OutfitSlots.ShoulderLeft", "Left Shoulder"),
    ("OutfitSlots.ShoulderRight", "Right Shoulder"),
    ("OutfitSlots.ElbowLeft", "Left Elbow"),
    ("OutfitSlots.ElbowRight", "Right Elbow"),
    ("OutfitSlots.WristLeft", "Left Wrist (Watches, Bands)"),
    ("OutfitSlots.WristRight", "Right Wrist (Watches, Bands)"),
    ("OutfitSlots.Hands", "Both Hands (Gloves)"),
    ("OutfitSlots.HandLeft", "Left Hand (Glove)"),
    ("OutfitSlots.HandRight", "Right Hand (Glove)"),
    ("OutfitSlots.FingersLeft", "Left Rings"),
    ("OutfitSlots.FingersRight", "Right Rings"),
    ("OutfitSlots.FingernailsLeft", "Left Nails"),
    ("OutfitSlots.FingernailsRight", "Right Nails"),
    ("OutfitSlots.Waist", "Waist (Belts)"),
    ("OutfitSlots.LegsInner", "Legs Inner (Tights, Leggings)"),
    ("OutfitSlots.LegsMiddle", "Legs Middle (Tight Pants, Shorts)"),
    ("OutfitSlots.LegsOuter", "Legs Outer (Loose Pants, Skirts)"),
    ("OutfitSlots.ThighLeft", "Left Thigh"),
    ("OutfitSlots.ThighRight", "Right Thigh"),
    ("OutfitSlots.KneeLeft", "Left Knee"),
    ("OutfitSlots.KneeRight", "Right Knee"),
    ("OutfitSlots.AnkleLeft", "Left Ankle"),
    ("OutfitSlots.AnkleRight", "Right Ankle"),
    ("OutfitSlots.Feet", "Feet (Footwear)"),
    ("OutfitSlots.ToesLeft", "Left Toe Rings"),
    ("OutfitSlots.ToesRight", "Right Toe Rings"),
    ("OutfitSlots.ToenailsLeft", "Left Toenails"),
    ("OutfitSlots.ToenailsRight", "Right Toenails"),
    ("OutfitSlots.BodyUnder", "Body Underwear (Netrunner Suits)"),
    ("OutfitSlots.BodyInner", "Body Inner"),
    ("OutfitSlots.BodyMiddle", "Body Middle (Jumpsuits, Tracksuits)"),
    ("OutfitSlots.BodyOuter", "Body Outer"),
    ("OutfitSlots.HandPropLeft", "Photo Mode Left Prop"),
    ("OutfitSlots.HandPropRight", "Photo Mode Right Prop"),
]

# Item rarity tiers (maps directly to CDPR's TweakXL `quality:` field)
QUALITY_TIERS = [
    ("Quality.Common",    "Common"),
    ("Quality.Uncommon",  "Uncommon"),
    ("Quality.Rare",      "Rare"),
    ("Quality.Epic",      "Epic"),
    ("Quality.Legendary", "Legendary"),
]

# Icon handling modes per item.
#   vanilla - no custom icon block is written, so the item silently inherits
#             whatever icon its $base slot preset already has (a safe,
#             always-valid placeholder). Nothing to break.
#   custom  - a per-item .inkatlas + icon YAML block is generated, pointing
#             at the modder's own {name}_icons.xbm sliced by color.
ICON_MODES = [
    ("custom",  "Custom (I have icon textures ready)"),
    ("vanilla", "Vanilla Fallback (safe placeholder, no textures needed)"),
]

# Regex scanner slot matching presets
SLOT_KEYWORDS = [
    (['jacket', 'coat', 'outer', 'cardigan', 'vest', 'cape', 'shawl', 'bolero'],
                                                'GenericOuterChestClothing', 'OutfitSlots.TorsoOuter'),
    (['top', 'shirt', 'bodice', 'bustier', 'tank', 'crop', 'blouse', 'camisole'],
                                                'GenericInnerChestClothing', 'OutfitSlots.TorsoInner'),
    (['corset'],                                 'GenericInnerChestClothing', 'OutfitSlots.TorsoInner'),
    (['pants', 'bottom', 'bottoms', 'legs', 'jeans', 'trousers', 'shorts', 'leggings',
      'skirt', 'stockings', 'tights', 'fishnet', 'panties', 'underwear_legs'],
                                                'GenericLegClothing',        'OutfitSlots.LegsMiddle'),
    (['shoes', 'boots', 'heels', 'feet', 'sneakers', 'sandals', 'footwear', 'slippers'],
                                                'GenericFootClothing',       'OutfitSlots.Feet'),
    (['hat', 'helmet', 'cap', 'head', 'headband', 'tiara', 'crown'],
                                                'GenericHeadClothing',       'OutfitSlots.Head'),
    (['glasses', 'mask', 'face', 'goggles', 'eyepatch'],
                                                'GenericFaceClothing',       'OutfitSlots.Mask'),
    (['dress', 'bodysuit', 'outfit', 'suit', 'onesie'],
                                                'Outfit',                    'OutfitSlots.BodyMiddle'),
]


def _header():
    return {
        "WolvenKitVersion": "8.18.1",
        "WKitJsonVersion": "0.0.9",
        "GameVersion": 2310,
        "ExportedDateTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "DataType": "CR2W"
    }

def default_root_ent():
    return {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "entEntityTemplate",
                "appearances": [],
                "entity": {
                    "Data": {
                        "$type": "entEntity",
                        "name": "root_entity",
                        "components": []
                    }
                }
            }
        }
    }

def default_appearance():
    return {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "appearanceAppearanceResource",
                "appearances": []
            }
        }
    }

def default_mesh_ent():
    return {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "entEntityTemplate",
                "appearances": [
                    {
                        "$type": "entEntityTemplateAppearance",
                        "name": "default"
                    }
                ],
                "entity": {
                    "Data": {
                        "$type": "entEntity",
                        "name": "mesh_entity",
                        "components": [
                            {
                                "$type": "entGarmentSkinnedMeshComponent",
                                "name": "t1_custom_clothing",
                                "mesh": {
                                    "DepotPath": ""
                                },
                                "chunkMask": 18446744073709551615
                            }
                        ]
                    }
                }
            }
        }
    }

def default_inkatlas():
    return {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "inkTextureAtlas",
                "activeTexture": {
                    "DepotPath": ""
                },
                "cookingPlatform": "PLATFORM_PC",
                "dynamicTexture": "None",
                "inkDynamicTextureSlot": "None",
                "isSingleTextureMode": True,
                "parts": [],
                "slots": []
            }
        }
    }

def default_mesh_json():
    return {
        "Header": _header(),
        "Data": {
            "Version": 195,
            "BuildVersion": 0,
            "RootChunk": {
                "$type": "CMesh",
                "materialEntries": [],
                "localMaterialBuffer": {
                    "materials": []
                },
                "appearances": [
                    {
                        "name": "default",
                        "chunkMaterials": ["default_material"]
                    }
                ]
            }
        }
    }


class ConfigManager:
    FIELDS = [
        'blender_dir', 'output_dir', 'mod_base_name', 'mod_author',
        'display_prefix', 'colors', 'body_types', 'wkit_cli', 'auto_compile', 'geometry',
    ]
    DEFAULTS = {
        'blender_dir': '', 'output_dir': '', 'mod_base_name': 'gothic_set',
        'mod_author': 'V_Designer', 'display_prefix': 'Midnight Gothic',
        'colors': 'black, red, purple', 'body_types': DEFAULT_BODY_TOKENS,
        'wkit_cli': '', 'auto_compile': False,
        'geometry': '1400x900',
    }

    def __init__(self):
        self._path = Path(__file__).resolve().parent / 'cpmp_config.json'
        self.d = dict(self.DEFAULTS)
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                with open(self._path, 'r', encoding='utf-8') as f:
                    self.d.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            with open(self._path, 'w', encoding='utf-8') as f:
                json.dump(self.d, f, indent=2)
        except Exception:
            pass

    def get(self, k):   return self.d.get(k, self.DEFAULTS.get(k, ''))
    def set(self, k, v): self.d[k] = v


class PipelineWorker:
    """
    All asset copying, JSON assembly modifications, translation mappings,
    and CLI orchestration are carried out here. Fully decoupled from tkinter.
    """

    def __init__(self, log_fn, progress_fn=None):
        self.log = log_fn
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        # BUG 12: optional callback(step:int, total:int) so the caller (the
        # tkinter GUI) can drive a real determinate progress bar instead of
        # just an indeterminate spinner with no sense of how far along a
        # long CLI compile actually is.
        self.progress_fn = progress_fn
        import sys
        if getattr(sys, 'frozen', False):
            self.script_dir = Path(sys.executable).parent
        else:
            self.script_dir = Path(__file__).resolve().parent

    def _check_cancel(self):
        """Check if cancellation was requested. Raises to abort pipeline."""
        if self._cancel_event.is_set():
            raise InterruptedError("Pipeline cancelled by user.")

    def _report_progress(self, step: int, total: int):
        if self.progress_fn:
            try:
                self.progress_fn(step, total)
            except Exception:
                pass

    @staticmethod
    def map_slot_presets(folder_name: str) -> tuple[str, str]:
        low = folder_name.lower()
        for keywords, base_s, eq_s in SLOT_KEYWORDS:
            for kw in keywords:
                if kw in low:
                    return base_s, eq_s
        return "GenericInnerChestClothing", "OutfitSlots.TorsoInner"

    @staticmethod
    def parse_colors(raw: str) -> list[str]:
        return [c.strip().lower() for c in raw.split(',') if c.strip()]

    def _ensure_dir(self, path: Path):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            self.log(f"  📂  Created: {path}", 'ok')

    def _load_template(self, filename: str, default_fn):
        path = self.script_dir / filename
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self.log(f"  ⚠  Error reading template '{filename}': {e}", 'warn')
        return default_fn()


    def scan_export_dir(self, blender_dir: str) -> list[dict]:
        root = Path(blender_dir)
        if not root.is_dir():
            self.log(f"  ✗  Directory not found: {blender_dir}", 'error')
            return []

        items = []
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            name = sub.name.lower().replace(' ', '_').replace('-', '_')
            # BUG 6 fix: recurse into nested subfolders (e.g. "Casual_pants_blend/inner/")
            # instead of only globbing the immediate directory, while still using the
            # *top-level* folder name as the item identity so parent context isn't lost.
            glbs = sorted(sub.rglob('*.glb'))
            if not glbs:
                continue
            base_s, eq_s = self.map_slot_presets(name)
            items.append({
                'name': name,
                'path': sub,
                'slot': base_s,
                'eq_slot': eq_s,
                'glbs': glbs,
                'display_name': name.replace('_', ' ').title(),
                'description': '',
                'quality': QUALITY_TIERS[0][0],
                'icon_mode': ICON_MODES[0][0],
                'two_sided_materials': [],
                'has_foot_variants': False,
            })
            self.log(f"  ▸ Scanned: '{name}' ({len(glbs)} mesh files)", 'info')

        # Fallback to flat scanning if root folder contains loose files directly
        if not items:
            root_glbs = sorted(root.glob('*.glb'))
            if root_glbs:
                name = root.name.lower().replace(' ', '_').replace('-', '_')
                base_s, eq_s = self.map_slot_presets(name)
                items.append({
                    'name': name,
                    'path': root,
                    'slot': base_s,
                    'eq_slot': eq_s,
                    'glbs': root_glbs,
                    'display_name': name.replace('_', ' ').title(),
                    'description': '',
                    'quality': QUALITY_TIERS[0][0],
                    'icon_mode': ICON_MODES[0][0],
                    'two_sided_materials': [],
                    'has_foot_variants': False,
                })
                self.log(f"  ▸ Scanned (Flat fallback): '{name}' ({len(root_glbs)} mesh files)", 'info')
        return items


    def init_project(self, output_dir: str, mod_base: str, author: str, items: list[dict]) -> Path:
        wkit = Path(output_dir) / mod_base
        self.log("", 'info')
        self.log("━━━  INITIALIZING WOLVENKIT PROJECT  ━━━", 'header')

        # Generate core filesystems
        dirs = [
            wkit / 'source' / 'resources' / 'archive' / 'pc' / 'mod',
            wkit / 'source' / 'resources' / 'r6' / 'tweaks' / mod_base,
            wkit / 'source' / 'archive' / mod_base / 'localization',
            wkit / 'source' / 'archive' / mod_base / 'textures'
        ]
        for d in dirs:
            self._ensure_dir(d)

        # Generate per-item subdirectory structures
        for it in items:
            if not it.get('enabled', True):
                continue
            item_dir = wkit / 'source' / 'archive' / mod_base / it['name']
            self._ensure_dir(item_dir)
            self._ensure_dir(item_dir / 'meshes')
            self._ensure_dir(item_dir / 'textures')

        # Create double-clickable metadata project file (.cpmodproj)
        proj_file = wkit / f"{mod_base}.cpmodproj"
        xml = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="utf-8"?>
            <CP77Mod xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                     xmlns:xsd="http://www.w3.org/2001/XMLSchema">
              <Name>{mod_base}</Name>
              <Author>{author}</Author>
              <Version>1.0.0</Version>
              <Description>Auto-generated by CP77 Modding Pipeline Master v{VERSION}</Description>
            </CP77Mod>
        """)
        with open(proj_file, 'w', encoding='utf-8') as f:
            f.write(xml)
        self.log(f"  📋  Created WolvenKit Workspace: {proj_file.name}", 'ok')
        return wkit


    def sync_glbs(self, items: list[dict], wkit: Path, mod_base: str, body_suffixes: list = None):
        self.log("", 'info')
        self.log("━━━  SYNCING BLENDER GLB GEOMETRY  ━━━", 'header')
        body_suffixes = body_suffixes if body_suffixes is not None else BODY_SUFFIX_CATALOG
        for it in items:
            if not it.get('enabled', True):
                continue
            dest = wkit / 'source' / 'raw' / mod_base / it['name'] / 'meshes'
            self._ensure_dir(dest)
            item_name = it['name']

            for glb in it['glbs']:
                stem = glb.stem
                new_stem = None

                for old_suf, new_suf in body_suffixes:
                    if stem.lower().endswith(old_suf):
                        new_stem = f"{item_name}_{new_suf}"
                        break

                if not new_stem:
                    for _, active_suf in body_suffixes:
                        alias = f"{item_name}_{active_suf}"
                        dst = dest / f"{alias}.glb"
                        shutil.copy2(str(glb), str(dst))
                    self.log(f"  🔄 {glb.name} ➔ (copied for all body types)", 'info')
                    continue

                dst = dest / f"{new_stem}.glb"
                shutil.copy2(str(glb), str(dst))
                self.log(f"  🔄 {glb.name} ➔ {new_stem}.glb", 'info')


    def sync_textures(self, items: list[dict], wkit: Path, mod_base: str, colors: list[str]):
        """Scans each item's source export folder for material maps
        (diffuse/normal/roughness/metalness/opacity), renames them to match
        the @dynamic naming convention the mesh templates expect, and stages
        them for CLI import into .xbm in run_wolvenkit_cli().

        Multi-material support: texture files are grouped by their filename
        prefix (e.g. ruffle_color_*, corset_color_*). Each prefix becomes a
        separate @dynamic material in the mesh. The prefix is preserved in
        the destination filename so the mesh converter can resolve per-material
        texture paths."""
        self.log("", 'info')
        self.log("━━━  SYNCING MATERIAL TEXTURES  ━━━", 'header')
        typed_colors = set(c.lower() for c in colors)

        for it in items:
            if not it.get('enabled', True):
                continue
            name = it['name']
            src_dir = Path(it['path'])
            raw_tex_dir = wkit / 'source' / 'raw' / mod_base / name / 'textures'
            found_variants = set()
            matched_any = False
            material_prefixes = set()

            # ── PASS 1: collect all image metadata without copying ──
            # Two-pass approach ensures we know ALL material prefixes before
            # deciding which is the "main" one (alphabetically first → no prefix).
            color_images = []
            non_color_images = []
            opacity_prefixes = set()

            for png in _find_images(src_dir):
                stem = png.stem
                if _ICON_TEX_RE.search(stem):
                    continue

                m = _COLOR_TEX_RE.search(stem)
                if m:
                    variant = m.group(1).lower()
                    mat_prefix = stem[:m.start()]
                    if not mat_prefix:
                        mat_prefix = name
                    material_prefixes.add(mat_prefix)
                    color_images.append((png, variant, mat_prefix))
                    found_variants.add(variant)
                    matched_any = True
                    continue

                mat_prefix = name
                for mp in material_prefixes:
                    if stem.startswith(mp):
                        mat_prefix = mp
                        break

                for pattern, suffix, label in (
                    (_NORMAL_TEX_RE, 'n', 'normal'),
                    (_ROUGH_TEX_RE, 'r', 'roughness'),
                    (_METAL_TEX_RE, 'm', 'metalness'),
                    (_OPACITY_TEX_RE, 'opacity', 'opacity'),
                ):
                    if pattern.search(stem):
                        non_color_images.append((png, suffix, mat_prefix))
                        if suffix == 'opacity':
                            opacity_prefixes.add(mat_prefix)
                        matched_any = True
                        break
                else:
                    self.log(
                        f"  ⚠  '{png.name}' doesn't match a recognized _color_<variant>/_n/_r/_m/_opacity "
                        f"naming pattern for item '{name}' — skipped (won't be imported).", 'warn'
                    )

            # ── Determine main material (alphabetically first prefix → no suffix) ──
            sorted_prefixes = sorted(material_prefixes, key=str.lower)
            main_prefix = sorted_prefixes[0] if len(sorted_prefixes) > 1 else None

            # ── PASS 2: copy files with correct naming ──
            self._ensure_dir(raw_tex_dir)

            # Build a lookup of mat_prefix → opacity PNG for alpha merge
            opacity_by_prefix = {}
            for png, suffix, mat_prefix in non_color_images:
                if suffix == 'opacity':
                    opacity_by_prefix[mat_prefix] = png

            for png, variant, mat_prefix in color_images:
                use_prefix = main_prefix is not None and mat_prefix != main_prefix
                if use_prefix:
                    dst = raw_tex_dir / f"{name}_{mat_prefix}_color_{variant}{png.suffix}"
                else:
                    dst = raw_tex_dir / f"{name}_color_{variant}{png.suffix}"

                opacity_png = opacity_by_prefix.get(mat_prefix)
                if opacity_png and _HAS_PIL:
                    try:
                        color_img = Image.open(str(png)).convert('RGBA')
                        opacity_img = Image.open(str(opacity_png)).convert('L')
                        if color_img.size != opacity_img.size:
                            opacity_img = opacity_img.resize(color_img.size, Image.LANCZOS)
                        color_img.putalpha(opacity_img)
                        color_img.save(str(dst))
                        self.log(f"  🎨 {png.name} + {opacity_png.name} (alpha merged) ➔ {dst.name}", 'info')
                    except Exception as e:
                        self.log(f"  ⚠  Alpha merge failed for {png.name}: {e} — copying color without alpha", 'warn')
                        shutil.copy2(str(png), str(dst))
                else:
                    shutil.copy2(str(png), str(dst))
                    self.log(f"  🎨 {png.name} ➔ {dst.name}", 'info')

            for png, suffix, mat_prefix in non_color_images:
                if suffix == 'opacity':
                    self.log(f"  🎨 {png.name} → merged into color alpha (skipped standalone copy)", 'info')
                    continue
                use_prefix = main_prefix is not None and mat_prefix != main_prefix
                if use_prefix:
                    dst = raw_tex_dir / f"{name}_{mat_prefix}_{suffix}{png.suffix}"
                else:
                    dst = raw_tex_dir / f"{name}_{suffix}{png.suffix}"
                shutil.copy2(str(png), str(dst))
                self.log(f"  🎨 {png.name} ➔ {dst.name}", 'info')

            if not matched_any:
                self.log(
                    f"  ⚠  No material textures found for '{name}'. It will fall back to "
                    f"whatever is already in the template mesh (likely looks wrong in-game).",
                    'warn'
                )

            it['material_prefixes'] = sorted(material_prefixes) if material_prefixes else [name]
            it['opacity_prefixes'] = sorted(opacity_prefixes)

            # This is the real, data-backed version of the BUG 5 color-mismatch
            # check — comparing the color variants actually found on disk against
            # what was typed into the Color Variants field, instead of just a
            # generic reminder with nothing to check it against.
            missing_pngs = typed_colors - found_variants
            extra_pngs = found_variants - typed_colors
            if missing_pngs:
                self.log(
                    f"  ✗  '{name}': no texture PNG found for color(s) {', '.join(sorted(missing_pngs))} "
                    f"— these will show broken/missing textures in-game.", 'error'
                )
            if extra_pngs:
                self.log(
                    f"  ⚠  '{name}': found texture PNG(s) for {', '.join(sorted(extra_pngs))} "
                    f"but they aren't listed in Color Variants, so no item will reference them.", 'warn'
                )
            it['found_variants'] = sorted(found_variants)


    def generate_configs(self, wkit: Path, mod_base: str, items: list[dict],
                         colors: list[str], display_prefix: str):
        self.log("", 'info')
        self.log("━━━  COMPILING MOD CONFIGURATIONS  ━━━", 'header')

        # BUG 5: the color variant names typed into the "Color Variants" field
        # become the literal !{color} appearance suffix in appearanceName, which
        # must exactly match an appearance actually baked into the .mesh file
        # (e.g. black, red, light_denim, classic_denim, brown). A typo/mismatch
        # here (e.g. "purple" when the mesh only has "light_denim") silently
        # fails texture resolution in-game with no compile-time error, so we
        # can't validate it automatically without inspecting the binary mesh —
        # just surface a loud reminder so the modder double-checks it themselves.
        self.log("  ⚠  Reminder: each color below must exactly match a mesh appearance", 'warn')
        self.log(f"     name baked into the .mesh file(s): {', '.join(colors)}", 'warn')
        self.log("     Mismatched names (e.g. 'purple' when the mesh has 'light_denim')", 'warn')
        self.log("     will silently fail texture resolution in-game.", 'warn')

        # 1. Generate Combined TweakXL YAML (with Equipment-EX placementSlots array)
        yaml_lines = [f"# TweakXL configs — compiled by CPMP v{VERSION}", ""]
        for it in items:
            if not it.get('enabled', True):
                continue
            name = it['name']
            slot = it['slot']
            eq_slot = it['eq_slot']
            quality = it.get('quality', QUALITY_TIERS[0][0])
            icon_mode = it.get('icon_mode', ICON_MODES[0][0])
            entity_name = f"{mod_base}_{name}_root"

            yaml_lines.append(f"# ── {name.replace('_', ' ').upper()} ──")
            item_colors = [c for c in colors if c in it.get('found_variants', colors)]
            if item_colors != colors:
                self.log(
                    f"  ℹ  '{name}': {len(item_colors)}/{len(colors)} color(s) have textures "
                    f"({', '.join(item_colors)}) — skipped: "
                    f"{', '.join(c for c in colors if c not in item_colors)}", 'info'
                )
            is_foot_state = bool(it.get('foot_states', []))
            for color in item_colors:
                yaml_lines.append(f"Items.{mod_base}_{name}_{color}:")
                yaml_lines.append(f"  $base: Items.{slot}")

                # Dynamic injection of Equipment-EX layout append blocks
                if eq_slot and eq_slot != "None":
                    yaml_lines.append(f"  placementSlots:")
                    yaml_lines.append(f"    - !append {eq_slot}")

                if is_foot_state:
                    # Foot-state pattern: color-specific entity + trailing-underscore appearance name
                    # + LegsState appearanceSuffix so the game auto-matches flat/lifted/heels.
                    color_entity = f"{mod_base}_{name}_{color}"
                    yaml_lines.append(f"  entityName: {color_entity}")
                    yaml_lines.append(f"  appearanceName: {color_entity}_")
                    yaml_lines.append(f"  appearanceSuffixes:")
                    yaml_lines.append(f"   - !append itemsFactoryAppearanceSuffix.LegsState")
                else:
                    yaml_lines.append(f"  entityName: {entity_name}")
                    yaml_lines.append(f"  appearanceName: {entity_name}!{color}")
                yaml_lines.append(f"  displayName: LocKey#{mod_base}_{name}_loc_name_{color}")
                yaml_lines.append(f"  localizedDescription: LocKey#{mod_base}_{name}_loc_desc")
                yaml_lines.append(f"  quality: {quality}")

                if icon_mode == 'custom':
                    yaml_lines.append(f"  icon:")
                    yaml_lines.append(f"    atlasResourcePath: {mod_base}\\{name}\\{name}_icons.inkatlas")
                    yaml_lines.append(f"    atlasPartName: {name}_{color}")
                # icon_mode == 'vanilla': deliberately omit the icon: block so
                # the item inherits whatever icon its $base slot already has.

                # FEATURE B: auto-generated spawn command comment, ready to
                # copy-paste straight into the CET console in-game.
                yaml_lines.append(f'  # Spawn: Game.AddToInventory("Items.{mod_base}_{name}_{color}", 1)')
                yaml_lines.append("")

        yaml_out = wkit / 'source' / 'resources' / 'r6' / 'tweaks' / mod_base / f"{mod_base}.yaml"
        with open(yaml_out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(yaml_lines))
        self.log(f"  ✓  Written combined TweakXL YAML: {yaml_out.name}", 'ok')

        # 2. Generate Combined CSV Factory (CR2W binary C2dArray format)
        csv_rows = []
        for it in items:
            if not it.get('enabled', True):
                continue
            name = it['name']
            is_foot_state = bool(it.get('foot_states', []))
            if is_foot_state:
                # Foot-state items: one CSV row per (item, color) -> color-specific entity
                for color in [c for c in colors if c in it.get('found_variants', colors)]:
                    entity = f"{mod_base}_{name}_{color}"
                    path = f"{mod_base}\\{name}\\{mod_base}_{name}_{color}.ent"
                    csv_rows.append([entity, path, "true"])
            else:
                entity = f"{mod_base}_{name}_root"
                path = f"{mod_base}\\{name}\\{mod_base}_{name}_root.ent"
                csv_rows.append([entity, path, "true"])

        csv_json_data = {
            "Header": {
                "WolvenKitVersion": "8.18.1",
                "WKitJsonVersion": "0.0.9",
                "GameVersion": 2310,
                "ExportedDateTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "DataType": "CR2W",
                "ArchiveFileName": ""
            },
            "Data": {
                "Version": 195,
                "BuildVersion": 0,
                "RootChunk": {
                    "$type": "C2dArray",
                    "compiledData": csv_rows,
                    "compiledHeaders": ["name", "path", "preload"],
                    "cookingPlatform": "PLATFORM_PC",
                    "data": [],
                    "headers": []
                },
                "EmbeddedFiles": []
            }
        }

        csv_json_out = wkit / 'source' / 'archive' / mod_base / f"{mod_base}.csv.json"
        with open(csv_json_out, 'w', encoding='utf-8') as f:
            json.dump(csv_json_data, f, indent=2)
        self.log(f"  ✓  Written factory CSV source: {csv_json_out.name}", 'ok')
        # BUG 9 fix: this used to try an inline CLI compile here via a
        # self.wkit_cli attribute that PipelineWorker never actually set
        # (it only ever takes log_fn), so this block silently crashed with
        # an AttributeError every time it ran. Binary CR2W compilation for
        # this file is already handled correctly in run_wolvenkit_cli()
        # (pipeline step 5), so the redundant/broken inline attempt has
        # been removed — this method now only ever writes the JSON source.

        # 3. Generate ArchiveXL configuration (.xl)
        xl_content = textwrap.dedent(f"""\
            factories:
              - {mod_base}\\{mod_base}.csv
            localization:
              onscreens:
                en-us:
                  - {mod_base}\\localization\\en-us.json
        """)
        xl_out = wkit / 'source' / 'resources' / 'archive' / 'pc' / 'mod' / f"{mod_base}.archive.xl"
        with open(xl_out, 'w', encoding='utf-8') as f:
            f.write(xl_content)
        self.log(f"  ✓  Written ArchiveXL: {xl_out.name}", 'ok')

        # 4. Generate combined localization dictionary — each item gets its
        #    own display name (per color) AND its own description, using the
        #    text the modder entered in the Item Manager card.
        loc_entries = []
        for it in items:
            if not it.get('enabled', True):
                continue
            name = it['name']
            item_display = it.get('display_name') or name.replace('_', ' ').title()
            item_colors = [c for c in colors if c in it.get('found_variants', colors)]
            for color in item_colors:
                # Avoid duplicating the prefix in the display name. If the
                # prefix already contains the item display name (e.g. prefix
                # "Denim Pants" contains "Pants"), skip prepending it.
                if display_prefix and not display_prefix.lower().__contains__(item_display.lower()):
                    display_name = f"{display_prefix} {item_display} - {color.title()}"
                else:
                    display_name = f"{display_prefix} - {color.title()}" if display_prefix else f"{item_display} - {color.title()}"
                loc_entries.append({
                    # Verified against working denim_pants localization: the
                    # CLI's polymorphic deserializer requires a "$type" on each
                    # entry object to resolve localizationPersistenceOnScreenEntry.
                    "$type": "localizationPersistenceOnScreenEntry",
                    "femaleVariant": display_name,
                    "maleVariant": "",
                    "primaryKey": "0",
                    "secondaryKey": f"{mod_base}_{name}_loc_name_{color}"
                })
            loc_entries.append({
                "$type": "localizationPersistenceOnScreenEntry",
                "femaleVariant": it.get('description') or "No description provided.",
                "maleVariant": "",
                "primaryKey": "0",
                "secondaryKey": f"{mod_base}_{name}_loc_desc"
            })

        loc_json_data = {
            "Header": {
                "WolvenKitVersion": "8.18.1",
                "WKitJsonVersion": "0.0.9",
                "GameVersion": 2310,
                "ExportedDateTime": "2026-07-01T00:00:00.0000000Z",
                "DataType": "CR2W",
                "ArchiveFileName": "en-us.json"
            },
            "Data": {
                "Version": 195,
                "BuildVersion": 0,
                "RootChunk": {
                    "$type": "JsonResource",
                    "cookingPlatform": "PLATFORM_PC",
                    "root": {
                        "HandleId": "0",
                        "Data": {
                            "$type": "localizationPersistenceOnScreenEntries",
                            "entries": loc_entries
                        }
                    }
                },
                "EmbeddedFiles": []
            }
        }

        loc_dir = wkit / 'source' / 'archive' / mod_base / 'localization'
        loc_dir.mkdir(parents=True, exist_ok=True)
        # BUG (found via live test log): localization's compiled CR2W resource
        # type (JsonResource) keeps ".json" as its own real extension in-game —
        # unlike the CSV factory (.csv) or .ent/.app files, where the compiled
        # extension differs from the ".json" source wrapper. Writing the source
        # as plain "en-us.json" meant `cr2w -d` stripping the ".json" wrapper
        # collided with the file's own name and produced nothing at all (the
        # exact "reported success but never produced" failure from the log).
        # Fix: use WolvenKit's own double-extension editing convention —
        # "en-us.json.json" as the editable source, so stripping one ".json"
        # correctly lands on "en-us.json" as the real compiled binary, which is
        # exactly what the .archive.xl localization reference expects.
        loc_json_out = loc_dir / 'en-us.json.json'
        with open(loc_json_out, 'w', encoding='utf-8') as f:
            json.dump(loc_json_data, f, indent=2, ensure_ascii=False)
        self.log(f"  ✓  Written localization source: {loc_json_out.name}", 'ok')
        # BUG 9 fix: same removed broken self.wkit_cli inline-compile attempt
        # as above. run_wolvenkit_cli() (pipeline step 5) already compiles
        # every localization JSON file it finds under this directory.


    def compile_json_templates(self, output_dir: str, mod_base: str, items: list[dict],
                               colors: list[str], body_suffixes: list = None):
        self.log("", 'info')
        self.log("━━━  EXECUTING ACTIVE MATERIAL COMPILATION  ━━━", 'header')
        body_suffixes = body_suffixes if body_suffixes is not None else BODY_SUFFIX_CATALOG

        # Load master asset skeletons
        tpl_root = self._load_template('template_root.ent.json', default_root_ent)
        tpl_app = self._load_template('template_appearance.app.json', default_appearance)
        tpl_mesh_ent = self._load_template('template_mesh.ent.json', default_mesh_ent)
        tpl_mesh = self._load_template('template.mesh.json', default_mesh_json)
        tpl_atlas = self._load_template('template_icons.inkatlas.json', default_inkatlas)

        # BUG 4 resolved: an inject_textures() helper used to live here for patching
        # material instances with explicit BaseColor/Normal/Roughness DepotPaths, but
        # it was never called. The mesh templates use CP2077's @dynamic material
        # convention instead — textures are resolved purely by naming convention
        # ({mesh_name}_color_{variant}.xbm, {mesh_name}_n.xbm, {mesh_name}_r.xbm,
        # {mesh_name}_m.xbm) off of the *{variant} meshAppearance set below, so no
        # explicit material patching is needed. The dead function has been removed.

        for it in items:
            self._check_cancel()
            if not it.get('enabled', True):
                continue
            name = it['name']
            entity_name = f"{mod_base}_{name}_root"
            out_dir = Path(output_dir) / mod_base / 'source' / 'archive' / mod_base / name

            # 1. Assemble Root Entity
            root = copy.deepcopy(tpl_root)
            root_apps = root['Data']['RootChunk'].setdefault('appearances', [])
            root_apps.clear()
            root_apps.append({
                "$type": "entTemplateAppearance",
                "appearanceName": {"$type": "CName", "$storage": "string", "$value": "None"},
                "appearanceResource": {
                    "DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"{mod_base}\\{name}\\{mod_base}_{name}_appearance.app"},
                    "Flags": "Soft"
                },
                "name": {"$type": "CName", "$storage": "string", "$value": entity_name}
            })
            root['Data']['RootChunk']['defaultAppearance'] = {"$type": "CName", "$storage": "string", "$value": "None"}
            
            # Inject dynamic appearance hooks
            try:
                v_tags = root['Data']['RootChunk']['visualTagsSchema']['Data']['visualTags']['tags']
                if not any(t.get('$value') == 'DynamicAppearance' for t in v_tags):
                    v_tags.append({"$type": "CName", "$storage": "string", "$value": "DynamicAppearance"})
            except Exception:
                pass

            with open(out_dir / f"{mod_base}_{name}_root.ent.json", 'w') as f:
                json.dump(root, f, indent=2)

            # 2. Assemble Appearance Resources
            app = copy.deepcopy(tpl_app)
            # Strip DismWoundConfig Configs (double-underscore CNames crash CLI parser)
            try:
                app['Data']['RootChunk']['DismWoundConfig']['Configs'] = []
            except Exception:
                pass
            app_apps = app['Data']['RootChunk'].setdefault('appearances', [])
            app_apps.clear()
            new_def = {
                "$type": "appearanceAppearanceDefinition",
                "censorFlags": 0,
                "components": [],
                "cookedDataPathOverride": {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"}, "Flags": "Soft"},
                "forcedLodDistance": 0,
                "hitRepresentationOverrides": [],
                "inheritedVisualTags": {"$type": "redTagList", "tags": []},
                "looseDependencies": [],
                "name": {"$type": "CName", "$storage": "string", "$value": entity_name},
                "partsMasks": [],
                "partsOverrides": [],
                "partsValues": [
                    {
                        "$type": "appearanceAppearancePart",
                        "resource": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"{mod_base}\\{name}\\{mod_base}_{name}_mesh_entity.ent"}, "Flags": "Soft"}
                    }
                ],
                "proxyMesh": {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"}, "Flags": "Soft"},
                "parentAppearance": {"$type": "CName", "$storage": "string", "$value": "None"},
                "visualTags": {"$type": "redTagList", "tags": []}
            }
            # Toggleable Feet (ArchiveXL): only two real tags exist — HighHeels
            # and FlatShoes. "Lifted" is the neutral/default state and needs no
            # tag at all, so it's intentionally not mapped to anything here.
            # Only applies to Feet-slot items ($base must be a foot item).
            if it.get('slot') == 'GenericFootClothing':
                foot_states = it.get('foot_states', [])
                if 'heel' in foot_states:
                    new_def["visualTags"]["tags"].append({"$type": "CName", "$storage": "string", "$value": "HighHeels"})
                if 'flat' in foot_states:
                    new_def["visualTags"]["tags"].append({"$type": "CName", "$storage": "string", "$value": "FlatShoes"})
                if 'heel' in foot_states and 'flat' in foot_states:
                    self.log(f"  ⚠  '{name}': both Flat and Heel checked — an item can only be "
                              f"one at a time in-game. Both tags will be written, but the game's "
                              f"behavior with conflicting tags is undefined. Pick one.", 'warn')
            app_apps.append({"HandleId": "1", "Data": new_def})
            with open(out_dir / f"{mod_base}_{name}_appearance.app.json", 'w') as f:
                json.dump(app, f, indent=2)

            # 3. Assemble Mesh Entity
            mesh_ent = copy.deepcopy(tpl_mesh_ent)
            mesh_depot = f"*{mod_base}\\{name}\\meshes\\{name}_{{body}}.mesh"
            # Use *{variant} for dynamic appearance resolution from YAML !color suffix.
            # The @dynamic material in the .mesh resolves textures by convention:
            #   {mesh_name}_color_{appearance}.xbm, {mesh_name}_n.xbm, etc.
            mesh_appearance = "*{variant}"
            for comp in mesh_ent['Data']['RootChunk'].get('components', []):
                if comp.get('$type') in ('entGarmentSkinnedMeshComponent', 'entSkinnedMeshComponent'):
                    comp['mesh']['DepotPath'] = {"$type": "ResourcePath", "$storage": "string", "$value": mesh_depot}
                    comp['meshAppearance'] = {"$type": "CName", "$storage": "string", "$value": mesh_appearance}
                    comp['name'] = {"$type": "CName", "$storage": "string", "$value": f"{name}_mesh"}
            for chunk in mesh_ent['Data']['RootChunk'].get('compiledData', {}).get('Data', {}).get('Chunks', []):
                # BUG 14: compiledData is NOT regenerated by `cr2w -d` — it's
                # preserved byte-for-byte from whatever was in the source JSON.
                # We resolve this with option (b) from the hand-off doc: patch
                # the same mesh DepotPath/meshAppearance/name fields here as in
                # `components` above, rather than clearing compiledData outright
                # (which risks dropping binary fields WolvenKit's CLI silently
                # depends on that aren't visible in this JSON view).
                if chunk.get('$type') in ('entGarmentSkinnedMeshComponent', 'entSkinnedMeshComponent'):
                    chunk['mesh']['DepotPath'] = {"$type": "ResourcePath", "$storage": "string", "$value": mesh_depot}
                    chunk['meshAppearance'] = {"$type": "CName", "$storage": "string", "$value": mesh_appearance}
                    chunk['name'] = {"$type": "CName", "$storage": "string", "$value": f"{name}_mesh"}
            with open(out_dir / f"{mod_base}_{name}_mesh_entity.ent.json", 'w') as f:
                json.dump(mesh_ent, f, indent=2)

            # 4. Copy template mesh binaries for each body suffix
            #    .mesh.json can't be compiled by WolvenKit CLI 8.18.1,
            #    so we copy the binary .mesh template and rename per body type.
            #    Per-body templates (template_mesh_{body}.mesh) are preferred
            #    over the generic template_mesh.mesh fallback.
            src_slot = it.get('slot', '')
            mesh_dir = out_dir / 'meshes'
            mesh_dir.mkdir(parents=True, exist_ok=True)
            slot_dir = self.script_dir / 'template_meshes' / src_slot
            fallback_mesh = None
            if slot_dir.is_dir():
                candidates = list(slot_dir.glob('template_mesh.mesh'))
                if candidates:
                    fallback_mesh = candidates[0]
            if fallback_mesh is None:
                for sub in (self.script_dir / 'template_meshes').iterdir():
                    if sub.is_dir():
                        candidates = list(sub.glob('template_mesh.mesh'))
                        if candidates:
                            fallback_mesh = candidates[0]
                            break
            for _, body_suffix in body_suffixes:
                dst = mesh_dir / f"{name}_{body_suffix}.mesh"
                if not dst.exists():
                    # Prefer per-body template (e.g. template_mesh_rb.mesh)
                    per_body = slot_dir / f"template_mesh_{body_suffix}.mesh" if slot_dir.is_dir() else None
                    if per_body and per_body.is_file():
                        shutil.copy2(per_body, dst)
                    elif fallback_mesh:
                        shutil.copy2(fallback_mesh, dst)
                    else:
                        print(f'  WARNING: No template mesh found for slot "{src_slot}" body "{body_suffix}"')

            # 5. Generate Icons coordinate map (.inkatlas) — only for items
            #    the modder explicitly marked as "Custom" in the Item Manager.
            #    Items left on "Vanilla Fallback" skip this and simply
            #    inherit their $base slot's existing icon (no risk of a
            #    dangling reference to textures that don't exist yet).
            if it.get('icon_mode') == 'custom':
                src_dir = Path(it['path'])
                prebuilt_xbm = list(src_dir.glob('*.xbm'))
                prebuilt_atlas = list(src_dir.glob('*.inkatlas'))
                icon_pngs = {}
                for png in _find_images(src_dir):
                    m = _ICON_TEX_RE.search(png.stem)
                    if m:
                        icon_pngs[m.group(1).lower()] = png

                if prebuilt_xbm or prebuilt_atlas:
                    # Priority 1: modder already has a hand-made .xbm sprite
                    # sheet + matching .inkatlas — just copy them in as-is.
                    for icon_file in prebuilt_xbm + prebuilt_atlas:
                        dst = out_dir / icon_file.name
                        shutil.copy2(str(icon_file), str(dst))
                        self.log(f"  🖼  Copied pre-made icon asset: {icon_file.name}", 'ok')
                    if not prebuilt_atlas:
                        self.log(
                            f"  ⚠  '{name}': found .xbm icon(s) but no .inkatlas coordinate "
                            f"map alongside them — you'll need to supply one manually.", 'warn'
                        )

                elif icon_pngs:
                    # Priority 2 (auto): combine all per-color PNGs into a
                    # single sprite sheet (.xbm) with UV-mapped inkatlas slots.
                    # Uses make_icon_sheet.py for Pillow-based compositing.
                    raw_icons_dir = Path(output_dir) / mod_base / 'source' / 'raw' / mod_base / name / 'icons'
                    self._ensure_dir(raw_icons_dir)
                    for color, png in icon_pngs.items():
                        if color not in colors:
                            self.log(
                                f"  ⚠  Icon file '{png.name}' targets color '{color}' which isn't "
                                f"in Color Variants — it will be included but nothing references it.",
                                'warn'
                            )
                    atlas_data = make_icon_sheet(
                        src_dir, name, mod_base, raw_icons_dir
                    )
                    if atlas_data:
                        with open(out_dir / f"{name}_icons.inkatlas.json", 'w') as f:
                            json.dump(atlas_data, f, indent=2)
                        self.log(f"  ✓  Auto-built icon sheet for '{name}' from {len(icon_pngs)} color(s) (single .xbm).", 'ok')
                    else:
                        self.log(f"  ⚠  Icon sheet generation failed for '{name}'.", 'warn')

                else:
                    self.log(
                        f"  ⚠  Icon mode is 'custom' for '{name}' but no icon assets were found. "
                        f"Drop either a pre-made {name}_icons.xbm + .inkatlas, or per-color PNGs "
                        f"named '{name}_icon_<color>.png' (e.g. '{name}_icon_black.png'), into "
                        f"its source export folder.", 'warn'
                    )

            self.log(f"  ✓  Compiled assemblies for item: '{name}'", 'ok')


    def run_wolvenkit_cli(self, cli_path: str, wkit: Path, mod_base: str, items: list[dict], colors: list[str] = None, body_suffixes: list = None):
        self.log("", 'info')
        self.log("━━━  RUNNING WOLVENKIT CLI BINARY COMPILER  ━━━", 'header')
        cli = Path(cli_path)
        if not cli.is_file():
            self.log(f"  ✗  CLI binary not found: {cli_path}", 'error')
            return False

        success, errors = 0, 0

        # 1. Compile modified JSON metadata structures to native binaries (.cr2w)
        #    2a optimization: use folder-level 'cr2w -d <dir>' instead of per-file
        #    calls to eliminate redundant .NET runtime startups (~6s each).
        for it in items:
            self._check_cancel()
            if not it.get('enabled', True):
                continue
            item_dir = wkit / 'source' / 'archive' / mod_base / it['name']
            json_files = sorted(item_dir.rglob('*.json'))
            if not json_files:
                continue
            # Pre-collect expected outputs so we can verify after batch compile
            expected = {}
            for jf in json_files:
                expected[jf.with_suffix('')] = jf
            self.log(f"  ▶ Batch packing {len(json_files)} JSON file(s) for: {it['name']}", 'info')
            cmd = [str(cli), 'cr2w', '-d', str(item_dir)]
            if self._execute_sub(cmd):
                for bin_out, src in expected.items():
                    if bin_out.exists():
                        success += 1
                    else:
                        self.log(
                            f"  ✗  Compile reported success but '{bin_out.name}' was never "
                            f"produced — check the CLI's cr2w verb/version.", 'error'
                        )
                        self._log_last_cli_output()
                        errors += 1
            else:
                errors += len(json_files)

        # 1b. Compile factory CSV JSON to CR2W binary
        # BUG 3 fix: `cr2w -d {mod_base}.csv.json` writes its compiled binary
        # output to {mod_base}.csv (suffix stripped) — that .csv file is what
        # ArchiveXL's `factories:` list actually expects on disk. The old code
        # inverted this: it copied the binary .csv *onto* the .csv.json path
        # and deleted the real .csv, leaving a binary blob wearing a .json
        # extension and no usable .csv file at all. Fix: keep the compiled
        # .csv binary as-is and just clean up the now-redundant .json source.
        csv_json = wkit / 'source' / 'archive' / mod_base / f"{mod_base}.csv.json"
        if csv_json.exists():
            cmd = [str(cli), 'cr2w', '-d', str(csv_json)]
            self.log(f"  ▶ Packing factory CSV: {csv_json.name}", 'info')
            if self._execute_sub(cmd):
                bin_out = csv_json.with_suffix('')  # -> {mod_base}.csv
                if bin_out.exists():
                    csv_json.unlink(missing_ok=True)
                    self.log(f"  ✓  Compiled factory CSV: {bin_out.name} ({bin_out.stat().st_size} bytes)", 'ok')
                    success += 1
                else:
                    self.log(f"  ✗  CSV compile reported success but {bin_out.name} was not found", 'error')
                    self._log_last_cli_output()
                    errors += 1
            else:
                errors += 1

        # 1c. Compile localization JSON to CR2W binary
        #     2a optimization: use folder-level 'cr2w -d <dir>' for batch compile.
        #     Source files are written as "en-us.json.json" so stripping one
        #     ".json" via with_suffix('') correctly lands on "en-us.json".
        loc_dir = wkit / 'source' / 'archive' / mod_base / 'localization'
        if loc_dir.is_dir():
            loc_jsons = sorted(loc_dir.rglob('*.json.json'))
            if loc_jsons:
                expected = {jf.with_suffix(''): jf for jf in loc_jsons}
                self.log(f"  ▶ Batch packing {len(loc_jsons)} localization file(s)", 'info')
                cmd = [str(cli), 'cr2w', '-d', str(loc_dir)]
                if self._execute_sub(cmd):
                    for bin_out, src in expected.items():
                        if bin_out.exists():
                            src.unlink(missing_ok=True)
                            self.log(f"  ✓  Compiled localization: {bin_out.name} ({bin_out.stat().st_size} bytes)", 'ok')
                            success += 1
                        else:
                            self.log(
                                f"  ✗  Localization compile reported success but "
                                f"'{bin_out.name}' was never produced — {src.name} is "
                                f"still plain JSON, not a compiled CR2W resource.", 'error'
                            )
                            self._log_last_cli_output()
                            errors += 1
                else:
                    errors += len(loc_jsons)

        # 2. Import Blender GLBs directly on top of newly initialized materials
        # 2a. Convert raw GLB meshes to CR2W .mesh files
        #     WolvenKit CLI 'import' does NOT support GLB→mesh ("Direct mesh
        #     importing is not implemented").  We use our custom converter
        #     cr2w_mesh.py which: serializes the template .mesh to JSON,
        #     replaces vertex/index data from the GLB, updates appearances
        #     and bounding boxes, then serializes back to binary via cr2w -d.
        # Template meshes live next to cpmp.py, not next to the output project.
        # Resolved per-item below via SLOT_DIR_MAP (was previously hardcoded to
        # 'Legs' for every item regardless of slot — that was the bug causing
        # non-Legs garments, like shirts, to be built against the pants donor
        # mesh's materials/UVs).
        template_root = Path(__file__).parent / 'template_meshes'
        # Collect the active body suffixes so we only convert matching GLBs
        if not body_suffixes:
            body_suffixes = BODY_SUFFIX_CATALOG
        active_suffixes = {tok for _, tok in body_suffixes}
        for it in items:
            self._check_cancel()
            if not it.get('enabled', True):
                continue
            name = it['name']
            raw_dir = wkit / 'source' / 'raw' / mod_base / name / 'meshes'
            arch_dir = wkit / 'source' / 'archive' / mod_base / name / 'meshes'
            if not raw_dir.is_dir():
                continue
            glb_files = list(raw_dir.glob('*.glb'))
            if not glb_files:
                continue
            item_slot = it.get('slot', '') or self.map_slot_presets(name)[0]
            tmpl_subdir = SLOT_DIR_MAP.get(item_slot, item_slot)
            template_dir = template_root / tmpl_subdir
            if not template_dir.is_dir():
                self.log(
                    f"  ✗  No template_meshes folder for slot '{item_slot}' "
                    f"(expected: {template_dir}) — skipping mesh build for '{name}'. "
                    f"You need a real donor template for this slot in WolvenKit, "
                    f"not a copy of another slot's template.", 'error'
                )
                errors += len(glb_files)
                continue
            self.log(f"  ▶ Converting GLB meshes for: {name} (template: {tmpl_subdir})", 'info')
            # --- GLB classification: detect foot states + body types ---
            # Hyst convention: {name}_{foot_state}_{body}.glb
            #   foot_state = flat | high | def (or omitted for default)
            #   body = ebb | angel | eve | rb | wb | base_body | etc.
            FOOT_STATE_MARKERS = {'flat', 'lifted', 'heel'}
            has_foot = it.get('has_foot_variants', False)
            single_glb = None
            per_body_glbs = {}
            per_foot_glbs = {}  # {(foot_state, body_suffix): glb_file}

            for glb_file in glb_files:
                stem = glb_file.stem
                parts = stem.split('_', 1)
                rest = parts[1] if len(parts) > 1 else 'base_body'

                if has_foot and '_' in rest:
                    first_token = rest.split('_')[0]
                    if first_token in FOOT_STATE_MARKERS:
                        foot_state = first_token
                        body_part = rest[len(first_token)+1:] or 'base_body'
                        if body_part in active_suffixes:
                            per_foot_glbs[(foot_state, body_part)] = glb_file
                            self.log(f"  GLB: {glb_file.name} → foot={foot_state}, body={body_part}", 'info')
                            continue

                if rest == 'base_body':
                    single_glb = glb_file
                elif rest in active_suffixes:
                    per_body_glbs[rest] = glb_file

            def _convert_one(glb_file, out_name):
                """Run the GLB→mesh converter for a single file. Returns True/False."""
                template_mesh = template_dir / f'template_mesh_{out_name.split("_", 1)[-1] if "_" in out_name else "body"}.mesh'
                if not template_mesh.exists():
                    template_mesh = template_dir / 'template_mesh.mesh'
                if not template_mesh.exists():
                    self.log(f"  ✗  No template mesh found in {template_dir}", 'error')
                    return False
                out_mesh = arch_dir / f'{out_name}.mesh'
                tex_prefix = f"{mod_base}\\{name}\\textures\\{name}"
                opacity_mats = set(it.get('opacity_prefixes', []))
                two_sided = set(it.get('two_sided_materials', []))
                mat_settings = it.get('material_settings', {})
                try:
                    return import_glb_to_mesh(
                        glb_path=str(glb_file),
                        template_mesh_path=str(template_mesh),
                        output_mesh_path=str(out_mesh),
                        cli_path=str(cli),
                        colors=[c for c in (colors or []) if c in it.get('found_variants', colors or [])],
                        texture_depot_prefix=tex_prefix,
                        logger=self.log,
                        has_opacity=opacity_mats if opacity_mats else None,
                        two_sided_materials=two_sided if two_sided else None,
                        material_settings=mat_settings if mat_settings else None,
                    )
                except Exception as e:
                    self.log(f"  ✗  GLB conversion failed for {glb_file.name}: {e}", 'error')
                    return False

            needs_body = it.get('needs_body_variants', True)
            if needs_body and single_glb and not per_body_glbs:
                # --- 1c: single GLB, needs body variants → convert once, copy rest
                first_suffix = next(iter(active_suffixes), None)
                if first_suffix:
                    first_stem = f"{name}_{first_suffix}"
                    ok = _convert_one(single_glb, first_stem)
                    if ok:
                        success += 1
                        for suf in active_suffixes:
                            if suf == first_suffix:
                                continue
                            src = arch_dir / f'{first_stem}.mesh'
                            dst = arch_dir / f'{name}_{suf}.mesh'
                            if src.exists():
                                shutil.copy2(str(src), str(dst))
                                self.log(f"  ✔  Copied {first_stem}.mesh → {name}_{suf}.mesh (single-GLB dedup)", 'info')
                                success += 1
                            else:
                                self.log(f"  ⚠  Source mesh {src} missing for copy", 'warn')
                    else:
                        errors += 1
            elif not needs_body:
                # --- 4a: no body variants → convert once, copy to all active body types.
                #     The mesh entity template uses {body} placeholders, so every active
                #     body type needs a .mesh file even if they're all identical copies.
                first_suffix = next(iter(active_suffixes), None)
                if first_suffix:
                    stem_name = f"{name}_{first_suffix}"
                    ok = _convert_one(single_glb or next(iter(per_body_glbs.values())), stem_name)
                    if ok:
                        success += 1
                        for suf in active_suffixes:
                            if suf == first_suffix:
                                continue
                            src = arch_dir / f'{stem_name}.mesh'
                            dst = arch_dir / f'{name}_{suf}.mesh'
                            if src.exists():
                                shutil.copy2(str(src), str(dst))
                                self.log(f"  ✔  Copied {stem_name}.mesh → {name}_{suf}.mesh (single-body opt-out)", 'info')
                                success += 1
                    else:
                        errors += 1
                self.log(f"  ⏭  Single conversion for '{name}' (body-type opt-out: all body types get identical mesh)", 'info')
            else:
                # per-body GLBs or single GLB without opt-in: convert each as before
                for glb_file in glb_files:
                    stem = glb_file.stem
                    parts = stem.split('_', 1)
                    body_suffix = parts[1] if len(parts) > 1 else 'base_body'
                    if body_suffix not in active_suffixes:
                        self.log(f"  ⏭  Skipping {glb_file.name} (body type '{body_suffix}' not active)", 'info')
                        continue
                    if _convert_one(glb_file, stem):
                        success += 1
                    else:
                        errors += 1

            # --- Foot-state variants for Leg slot ---
            # Convert each (foot_state, body) GLB into a separate mesh.
            # ArchiveXL swaps them at runtime via {feet} in depot paths.
            if per_foot_glbs:
                self.log(f"  ▶ Converting {len(per_foot_glbs)} foot-state variant(s)...", 'info')
                for (foot_state, body_suffix), glb_file in per_foot_glbs.items():
                    out_name = f"{name}_{foot_state}_{body_suffix}"
                    if _convert_one(glb_file, out_name):
                        success += 1
                        self.log(f"  ✔  {glb_file.name} → {out_name}.mesh (foot={foot_state})", 'info')
                    else:
                        errors += 1

        # 2b. Import raw material texture PNGs (color/normal/roughness/metalness)
        #     staged by sync_textures() into .xbm, landing at {mod_base}\{name}\textures\
        #     to match the @dynamic material naming convention.
        # 2c. Import combined icon sprite sheets in parallel (2c optimization).
        #     All texture+icon imports for independent items are submitted to a
        #     thread pool and executed concurrently.
        import_jobs = []
        for it in items:
            if not it.get('enabled', True):
                continue
            name = it['name']
            # Material textures
            raw_tex = wkit / 'source' / 'raw' / mod_base / name / 'textures'
            arch_tex = wkit / 'source' / 'archive' / mod_base / name / 'textures'
            if raw_tex.is_dir() and any(_find_images(raw_tex)):
                arch_tex.mkdir(parents=True, exist_ok=True)
                import_jobs.append(('textures', name, [str(cli), 'import', str(raw_tex), '--outpath', str(arch_tex)]))
            # Icon sprite sheets
            raw_icon = wkit / 'source' / 'raw' / mod_base / name / 'icons'
            arch_icon = wkit / 'source' / 'archive' / mod_base / name
            if raw_icon.is_dir() and any(_find_images(raw_icon)):
                import_jobs.append(('icons', name, [str(cli), 'import', str(raw_icon), '--outpath', str(arch_icon)]))

        if import_jobs:
            self.log(f"  ▶ Importing {len(import_jobs)} texture/icon asset(s) in parallel...", 'info')
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(self._execute_sub, cmd, timeout=600): (kind, name) for kind, name, cmd in import_jobs}
                for fut in as_completed(futures):
                    kind, name = futures[fut]
                    try:
                        if fut.result():
                            success += 1
                        else:
                            errors += 1
                    except Exception as e:
                        self.log(f"  ✗  {kind} import crashed for '{name}': {e}", 'error')
                        errors += 1

        self.log("", 'info')
        self.log(f"  ═══  CLI Sync Complete: {success} succeeded, {errors} errors  ═══", 'ok' if errors == 0 else 'warn')
        return errors == 0

    def _execute_sub(self, cmd: list, timeout: int = 300) -> bool:
        self._last_result = None
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            with self._lock:
                self._last_result = res
            if res.returncode not in (0, 3):
                if res.stderr.strip():
                    self.log(f"     Error output: {res.stderr.strip()}", 'error')
                return False
            return True
        except Exception as e:
            self.log(f"  ✗ Subprocess execution failure: {e}", 'error')
            return False

    def _log_last_cli_output(self):
        """Called after a verification failure (exit code looked fine but the
        expected output file never appeared) to surface whatever the CLI
        actually printed, since that's otherwise thrown away on the 'success'
        path."""
        res = getattr(self, '_last_result', None)
        if res is None:
            return
        if res.stdout and res.stdout.strip():
            self.log(f"     CLI stdout: {res.stdout.strip()[:400]}", 'warn')
        if res.stderr and res.stderr.strip():
            self.log(f"     CLI stderr: {res.stderr.strip()[:400]}", 'warn')

    def orchestrate_pipeline(self, blender_dir, output_dir, mod_base, author,
                             display_prefix, colors_raw, cli_path, auto_compile,
                             active_items, body_suffixes_raw: str = DEFAULT_BODY_TOKENS):
        self.log("═══════════════════════════════════════════════════════", 'header')
        self.log(f"  CP77 MODDING PIPELINE MASTER v{VERSION}", 'header')
        self.log(f"  Execution Thread Initiated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 'info')
        self.log("═══════════════════════════════════════════════════════", 'header')

        colors = self.parse_colors(colors_raw)
        if not colors:
            self.log("  ✗  Error: At least one color variant is required!", 'error')
            return False

        body_suffixes, unknown_tokens = resolve_body_suffixes(body_suffixes_raw)
        if unknown_tokens:
            self.log(f"  ⚠  Unrecognized body type token(s), ignored: {', '.join(unknown_tokens)}", 'warn')
        if not body_suffixes:
            self.log("  ✗  Error: no valid body types selected — nothing to generate meshes for!", 'error')
            return False
        self.log(f"  ▸ Generating body types: {', '.join(t for _, t in body_suffixes)}", 'info')

        mod_name = mod_base
        mod_base = mod_base.lower()
        TOTAL_STEPS = 6

        # Step 1: Initialize project file directories
        self._check_cancel()
        self._report_progress(0, TOTAL_STEPS)
        wkit = self.init_project(output_dir, mod_base, author, active_items)

        # Step 2: Synchronize raw 3D meshes
        self._check_cancel()
        self._report_progress(1, TOTAL_STEPS)
        self.sync_glbs(active_items, wkit, mod_base, body_suffixes)

        # Step 3: Synchronize raw material textures
        self._check_cancel()
        self._report_progress(2, TOTAL_STEPS)
        self.sync_textures(active_items, wkit, mod_base, colors)

        # Step 4: Compile YAML and CSV configurations
        self._check_cancel()
        self._report_progress(3, TOTAL_STEPS)
        self.generate_configs(wkit, mod_base, active_items, colors, display_prefix)

        # Step 5: Run material array assemblies
        self._check_cancel()
        self._report_progress(4, TOTAL_STEPS)
        self.compile_json_templates(str(wkit.parent), mod_base, active_items, colors, body_suffixes)

        # Step 6: Execute binaries build
        self._check_cancel()
        self._report_progress(5, TOTAL_STEPS)
        ran_cli = False
        if auto_compile and cli_path:
            ran_cli = self.run_wolvenkit_cli(cli_path, wkit, mod_base, active_items, colors, body_suffixes)
        else:
            self.log("", 'info')
            self.log("━━━  ⚠  BINARY CONVERSION SKIPPED  ━━━", 'warn')
            self.log("  Every asset was written as .json (e.g. *.mesh.json, *.ent.json).", 'warn')
            self.log("  These are NOT usable in-game yet and won't open in WolvenKit's", 'warn')
            self.log("  Asset Browser or Mesh Viewer until converted to real binaries.", 'warn')
            self.log("  Fix: set 'WolvenKit CLI Executable File' + check 'Build game", 'warn')
            self.log("  binary files on compilation', then re-run — or just click", 'warn')
            self.log("  '🔨 Manual WolvenKit CLI Sync' now to convert what's already here.", 'warn')

        self.log("", 'info')
        self.log("═══════════════════════════════════════════════════════", 'header')
        self.log("  ✓  PIPELINE BUILD FINISHED", 'ok')
        self.log(f"  Target Workspace: {Path(output_dir) / mod_base}", 'ok')
        if not ran_cli:
            self.log("  ⚠  Binaries not yet built — see warning above.", 'warn')
        self.log("═══════════════════════════════════════════════════════", 'header')
        self._report_progress(TOTAL_STEPS, TOTAL_STEPS)
        return True


class CPMPApp:
    def __init__(self):
        self.cfg = ConfigManager()
        self.log_queue = queue.Queue()
        self._running = False
        self.scanned_items = []

        # Persistent log file: every console line is now also appended to a
        # timestamped file on disk (not just kept in the scrollback), so a
        # failed run can be inspected/shared after the fact.
        try:
            script_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
            log_dir = script_dir / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file_path = log_dir / f"cpmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            self._log_file = open(self._log_file_path, 'a', encoding='utf-8')
        except Exception:
            self._log_file = None
            self._log_file_path = None

        self.root = tk.Tk()
        self.root.title(f"CP77 Modding Pipeline Master v{VERSION} - Equipment-EX Engine")
        self.root.configure(bg=C['bg_root'])
        self.root.geometry(self.cfg.get('geometry'))
        self.root.minsize(1350, 850)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self._build_ui()
        self._restore_fields()
        self._poll_log()
        if self._log_file_path:
            self._log(f"Logging this session to: {self._log_file_path}", 'info')

    def _build_ui(self):
        # Top Header Bar
        header = tk.Frame(self.root, bg=C['bg_dark'], height=56)
        header.pack(fill='x', padx=0, pady=0)
        header.pack_propagate(False)

        tk.Label(
            header, text="⬡  CP77 MODDING PIPELINE MASTER",
            bg=C['bg_dark'], fg=C['cyan'], font=(FONT, 14, 'bold')
        ).pack(side='left', padx=16, pady=12)

        tk.Label(
            header, text=f"v{VERSION}  ·  Equipment-EX active compiler module",
            bg=C['bg_dark'], fg=C['text_dim'], font=(FONT, 9)
        ).pack(side='left', padx=4, pady=14)

        self._status_label = tk.Label(
            header, text="● READY", bg=C['bg_dark'], fg=C['green'], font=(FONT, 9, 'bold')
        )
        self._status_label.pack(side='right', padx=16, pady=14)

        tk.Frame(self.root, bg=C['cyan'], height=1).pack(fill='x')

        # Main Three-Column Split Layout
        body = tk.Frame(self.root, bg=C['bg_root'])
        body.pack(fill='both', expand=True, padx=8, pady=8)
        body.columnconfigure(0, weight=1, minsize=380) # Configuration Panel
        body.columnconfigure(1, weight=2, minsize=480) # Dynamic Equipment-EX Item Manager
        body.columnconfigure(2, weight=1, minsize=380) # Live console log
        body.rowconfigure(0, weight=1)

        # Column 1: Config
        col1 = tk.Frame(body, bg=C['bg_root'])
        col1.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        self._build_left_configs(col1)

        # Column 2: Equipment-EX Item Manager (Scrollable Frame)
        col2 = tk.Frame(body, bg=C['bg_root'])
        col2.grid(row=0, column=1, sticky='nsew', padx=(4, 4))
        self._build_middle_manager(col2)

        # Column 3: Console
        col3 = tk.Frame(body, bg=C['bg_root'])
        col3.grid(row=0, column=2, sticky='nsew', padx=(4, 0))
        self._build_right_console(col3)


    def _panel(self, parent, title, emoji=''):
        outer = tk.Frame(parent, bg=C['border'], bd=0, highlightthickness=0)
        outer.pack(fill='x', pady=(0, 8))
        inner = tk.Frame(outer, bg=C['bg_panel'], bd=0)
        inner.pack(fill='both', padx=1, pady=1)

        t_bar = tk.Frame(inner, bg=C['bg_panel_alt'], height=30)
        t_bar.pack(fill='x')
        t_bar.pack_propagate(False)
        tk.Label(
            t_bar, text=f" {emoji}  {title}",
            bg=C['bg_panel_alt'], fg=C['text_heading'],
            font=(FONT, 9, 'bold'), anchor='w'
        ).pack(side='left', padx=8, pady=4)

        content = tk.Frame(inner, bg=C['bg_panel'])
        content.pack(fill='both', padx=10, pady=(6, 10))
        return content

    def _labeled_entry(self, parent, label, row, browse=False, browse_type='dir'):
        tk.Label(parent, text=label, bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8), anchor='w').grid(row=row, column=0, sticky='w', pady=(3, 1))
        entry = tk.Entry(
            parent, bg=C['bg_input'], fg=C['text'], insertbackground=C['cyan'], relief='flat', font=(FONT, 9),
            highlightthickness=1, highlightbackground=C['border'], highlightcolor=C['cyan']
        )
        if browse:
            entry.grid(row=row + 1, column=0, sticky='ew', pady=(0, 2), padx=(0, 4))
            btn = tk.Button(
                parent, text="Browse", bg=C['btn_primary'], fg=C['text'], font=(FONT, 8), relief='flat', cursor='hand2', width=7,
                activebackground=C['bg_hover'], command=lambda: self._browse(entry, browse_type)
            )
            btn.grid(row=row + 1, column=1, sticky='e', pady=(0, 2))
        else:
            entry.grid(row=row + 1, column=0, columnspan=2, sticky='ew', pady=(0, 2))
        return entry

    def _labeled_body_type_checks(self, parent, label, row):
        """Grid of checkboxes (one per BODY_SUFFIX_CATALOG token) replacing the
        old comma-separated text entry. Returns {token: BooleanVar}."""
        tk.Label(parent, text=label, bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8), anchor='w').grid(row=row, column=0, columnspan=2, sticky='w', pady=(3, 1))
        grid = tk.Frame(parent, bg=C['bg_panel'])
        grid.grid(row=row + 1, column=0, columnspan=2, sticky='ew', pady=(0, 2))
        default_tokens = {t.strip().lower() for t in DEFAULT_BODY_TOKENS.split(',') if t.strip()}
        body_type_vars = {}
        for i, (_, token) in enumerate(BODY_SUFFIX_CATALOG):
            var = tk.BooleanVar(value=token in default_tokens)
            cb = tk.Checkbutton(
                grid, text=token, variable=var, bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 8)
            )
            cb.grid(row=i // 4, column=i % 4, sticky='w', padx=(0, 8))
            if token in BODY_TYPE_TOOLTIPS:
                ToolTip(cb, BODY_TYPE_TOOLTIPS[token])
            body_type_vars[token] = var
        return body_type_vars

    def _browse(self, entry, btype):
        path = filedialog.askdirectory(title="Select Folder") if btype == 'dir' else filedialog.askopenfilename(title="Select WolvenKit CLI", filetypes=[("Executable", "*.exe")])
        if path:
            entry.delete(0, tk.END)
            entry.insert(0, path)

    def _build_left_configs(self, parent):
        # Core Parameters Panel
        c_panel = self._panel(parent, "MOD COMPILER OPTIONS", "📁")
        c_panel.columnconfigure(0, weight=1)
        self.e_blender = self._labeled_entry(c_panel, "Blender Exports Directory", 0, browse=True)
        self.e_output = self._labeled_entry(c_panel, "WolvenKit Projects Path", 2, browse=True)
        self.e_mod_base = self._labeled_entry(c_panel, "Mod Base ID (e.g. gothic_dress)", 4)
        self.e_author = self._labeled_entry(c_panel, "Author / Maker Name", 6)
        self.e_display = self._labeled_entry(c_panel, "Inventory Catalog Prefix", 8)
        self.e_colors = self._labeled_entry(c_panel, "Color Variants (comma-separated)", 10)
        self.e_body_types = self._labeled_body_type_checks(c_panel, "Body Types", 12)

        tk.Label(
            c_panel, text="ℹ  Per-item display name, description, rarity and icon\n   handling are set in the Item Manager (middle column) below.",
            bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic'), justify='left', anchor='w'
        ).grid(row=14, column=0, columnspan=2, sticky='w', pady=(6, 0))

        # WolvenKit CLI Integration Panel
        cli_panel = self._panel(parent, "WOLVENKIT CLI TOOLS", "🔧")
        cli_panel.columnconfigure(0, weight=1)
        self.e_cli = self._labeled_entry(cli_panel, "WolvenKit CLI Executable File", 0, browse=True, browse_type='file')

        self.auto_compile_var = tk.BooleanVar(value=self.cfg.get('auto_compile'))
        tk.Checkbutton(
            cli_panel, text="Build game binary files on compilation",
            variable=self.auto_compile_var, bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
            activebackground=C['bg_panel'], font=(FONT, 8)
        ).grid(row=2, column=0, columnspan=2, sticky='w', pady=(6, 0))
        tk.Label(
            cli_panel,
            text="ℹ  Without this, everything is written as .json only —\n   won't open in WolvenKit or work in-game until converted.",
            bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic'), justify='left', anchor='w'
        ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))

        # Compile Panel Execution
        act_panel = self._panel(parent, "PIPELINE ORCHESTRATION", "⚡")
        prog_f = tk.Frame(act_panel, bg=C['bg_panel'])
        prog_f.pack(fill='x', pady=(0, 6))
        self._progress = ttk.Progressbar(prog_f, mode='determinate', maximum=6, length=200)
        self._progress.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self._cancel_var = tk.BooleanVar(value=False)
        self.btn_cancel = tk.Button(
            prog_f, text="✖ Cancel", bg=C.get('btn_warn', '#8b4513'), fg=C['text'],
            font=(FONT, 8, 'bold'), relief='flat', cursor='hand2', state='disabled',
            command=self._on_cancel, width=10
        )
        self.btn_cancel.pack(side='right')

        btn_f = tk.Frame(act_panel, bg=C['bg_panel'])
        btn_f.pack(fill='x')

        self.btn_scan = self._action_btn(btn_f, "🔍  1. Preview Scan Exports", C['btn_scan'], self._on_scan)
        self.btn_preview = self._action_btn(btn_f, "👁  1b. Dry-Run Preview", C.get('btn_compile', '#3d2a08'), self._on_preview)
        self.btn_run = self._action_btn(btn_f, "▶  2. Process && Compile Project", C['btn_run'], self._on_run)
        self.btn_compile = self._action_btn(btn_f, "🔨  Manual WolvenKit CLI Sync", C['btn_compile'], self._on_compile)

    def _action_btn(self, parent, text, color, cmd):
        btn = tk.Button(
            parent, text=text, bg=color, fg=C['text'], font=(FONT, 9, 'bold'), relief='flat', cursor='hand2', command=cmd, height=2
        )
        btn.pack(fill='x', pady=3)
        btn.bind('<Enter>', lambda e: btn.config(bg=C['bg_hover']))
        btn.bind('<Leave>', lambda e: btn.config(bg=color))
        return btn


    def _build_middle_manager(self, parent):
        outer = tk.Frame(parent, bg=C['border'])
        outer.pack(fill='both', expand=True)

        inner = tk.Frame(outer, bg=C['bg_panel'])
        inner.pack(fill='both', expand=True, padx=1, pady=1)

        # Tab Title Header
        t_bar = tk.Frame(inner, bg=C['bg_panel_alt'], height=30)
        t_bar.pack(fill='x')
        t_bar.pack_propagate(False)
        tk.Label(
            t_bar, text=" 👕  DYNAMIC EQUIPMENT-EX ITEM MANAGER",
            bg=C['bg_panel_alt'], fg=C['text_heading'], font=(FONT, 9, 'bold'), anchor='w'
        ).pack(side='left', padx=8, pady=4)

        # Scrollable Canvas
        self.canvas = tk.Canvas(inner, bg=C['bg_console'], bd=0, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(inner, orient="vertical", command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg=C['bg_console'])

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.bind('<Configure>', lambda e: (
            self.canvas.itemconfig(self.canvas_window, width=e.width),
            self._update_scrollbar_visibility()
        ))

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        # Scroll with mouse wheel — scoped to only fire while the pointer is
        # actually over this panel. The previous bind_all() applied globally
        # to the whole app, so scrolling anywhere (including over Comboboxes,
        # which have their own default wheel behavior of changing the
        # selected value) would also drag this canvas's view, producing the
        # "elements scroll on their own" glitch. Binding/unbinding on
        # Enter/Leave of the containing panel keeps the scroll behavior
        # local to this panel only.
        def _on_mousewheel(event):
            bbox = self.canvas.bbox("all")
            if bbox and (bbox[3] - bbox[1]) > self.canvas.winfo_height():
                self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        def _bind_mousewheel(event):
            self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mousewheel(event):
            self.canvas.unbind_all("<MouseWheel>")
        inner.bind('<Enter>', _bind_mousewheel)
        inner.bind('<Leave>', _unbind_mousewheel)

        self.canvas.pack(side="left", fill="both", expand=True)

        self._show_middle_placeholder()

    def _show_middle_placeholder(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        placeholder = tk.Label(
            self.scroll_frame,
            text="No exported Blender subfolders loaded.\n\nType in your configurations on the left,\nthen click '1. Preview Scan Exports' to populate\nyour Equipment-EX slots mapping panel here.",
            bg=C['bg_console'], fg=C['text_dim'], font=(FONT, 10, 'italic'), justify='center'
        )
        placeholder.pack(pady=150, fill='both', expand=True)
        self._update_scrollbar_visibility()

    def _update_scrollbar_visibility(self):
        """Show or hide the scrollbar depending on whether content
        overflows the canvas viewport."""
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all")
        if bbox is None:
            self.scrollbar.pack_forget()
            return
        content_height = bbox[3] - bbox[1]
        viewport_height = self.canvas.winfo_height()
        if content_height > viewport_height:
            if not self.scrollbar.winfo_ismapped():
                self.scrollbar.pack(side="right", fill="y")
        else:
            self.scrollbar.pack_forget()

    def _populate_item_manager(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        if not self.scanned_items:
            self._show_middle_placeholder()
            return

        tk.Label(
            self.scroll_frame,
            text="Verify your scanned clothing folders and override assignments below:",
            bg=C['bg_console'], fg=C['cyan'], font=(FONT, 8, 'bold'), anchor='w'
        ).pack(padx=10, pady=(8, 12), fill='x')

        self.item_widgets = []

        # Create interactive option cards for each subdirectory item
        for idx, it in enumerate(self.scanned_items):
            card = tk.Frame(self.scroll_frame, bg=C['bg_panel'], bd=1, highlightbackground=C['border'], highlightcolor=C['cyan'], highlightthickness=1)
            card.pack(fill='x', padx=10, pady=(0, 10))

            card_header = tk.Frame(card, bg=C['bg_panel_alt'], height=28)
            card_header.pack(fill='x')
            card_header.pack_propagate(False)

            # Active Toggle Checkbox
            enabled_var = tk.BooleanVar(value=it.get('enabled', True))
            chk = tk.Checkbutton(
                card_header, text=f"Active Folder: '{it['name']}'", variable=enabled_var,
                bg=C['bg_panel_alt'], fg=C['text_heading'], selectcolor=C['bg_input'],
                activebackground=C['bg_panel_alt'], activeforeground=C['text_heading'], font=(FONT, 9, 'bold')
            )
            chk.pack(side='left', padx=6)

            # 3c: Per-item compile button — runs pipeline on just this one item
            item_compile_btn = tk.Button(
                card_header, text="Compile This Item", bg=C.get('btn_compile', '#3d2a08'), fg=C['text'],
                font=(FONT, 7, 'bold'), relief='flat', cursor='hand2', padx=8, pady=1,
                command=lambda n=it['name']: self._on_compile_single(n)
            )
            item_compile_btn.pack(side='right', padx=6, pady=2)

            body_frame = tk.Frame(card, bg=C['bg_panel'], padx=8, pady=8)
            body_frame.pack(fill='x')
            body_frame.columnconfigure(1, weight=1)

            # Dropdown for Base Equip Slots
            tk.Label(body_frame, text="Vanilla Base Category:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=0, column=0, sticky='w', pady=3)
            base_var = tk.StringVar(value=it['slot'])
            base_combo = ttk.Combobox(body_frame, textvariable=base_var, values=[b[0] for b in BASE_SLOTS], state="readonly", font=(FONT, 8))
            base_combo.grid(row=0, column=1, sticky='ew', padx=(6, 0), pady=3)

            # Foot state checkboxes (Flat / Lifted / Heel) — for Feet slot
            # always shown; for Leg slot, only shown when "has foot state
            # variants" is checked (tights/stockings that change with heels).
            foot_flat_var = tk.BooleanVar(value='flat' in it.get('foot_states', []))
            foot_lifted_var = tk.BooleanVar(value='lifted' in it.get('foot_states', []))
            foot_heel_var = tk.BooleanVar(value='heel' in it.get('foot_states', []))
            foot_state_frame = tk.Frame(body_frame, bg=C['bg_panel'])
            tk.Label(foot_state_frame, text="Foot State:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).pack(side='left', padx=(0, 6))
            for text, var in (("Flat", foot_flat_var), ("Lifted", foot_lifted_var), ("Heel", foot_heel_var)):
                tk.Checkbutton(
                    foot_state_frame, text=text, variable=var, bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                    activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 8)
                ).pack(side='left', padx=(0, 8))
            tk.Label(
                foot_state_frame, text="(Lifted = default, no tag needed)",
                bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic')
            ).pack(side='left', padx=(4, 0))

            # For Leg slot: "has foot state variants" master toggle
            has_foot_variants = tk.BooleanVar(value=it.get('has_foot_variants', False))
            foot_var_cb = tk.Checkbutton(
                body_frame, text="Has foot state variants", variable=has_foot_variants,
                bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 8)
            )
            foot_var_cb.grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 3))
            ToolTip(foot_var_cb, "Check if this leg item has separate GLBs per foot state (e.g. tights with flat/high heel variants).")

            def _toggle_foot_state_row(event=None, bv=base_var, frame=foot_state_frame, fvar=has_foot_variants, fcb=foot_var_cb):
                is_feet = bv.get() == 'GenericFootClothing'
                is_leg = bv.get() == 'GenericLegClothing'
                if is_feet:
                    fcb.grid_remove()
                    frame.grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 3))
                elif is_leg:
                    fcb.grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 3))
                    if fvar.get():
                        frame.grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 3))
                    else:
                        frame.grid_remove()
                else:
                    fcb.grid_remove()
                    frame.grid_remove()
            base_combo.bind('<<ComboboxSelected>>', _toggle_foot_state_row)
            has_foot_variants.trace_add('write', lambda *_: _toggle_foot_state_row())
            _toggle_foot_state_row()

            # Dropdown for Equipment-EX Custom Slots
            tk.Label(body_frame, text="Equipment-EX Outfit Slot:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=2, column=0, sticky='w', pady=3)
            eq_var = tk.StringVar(value=it['eq_slot'])
            eq_combo = ttk.Combobox(body_frame, textvariable=eq_var, values=[e[0] for e in EQUIPMENT_EX_SLOTS], state="readonly", font=(FONT, 8))
            eq_combo.grid(row=2, column=1, sticky='ew', padx=(6, 0), pady=3)

            # Body-type variants checkbox — auto-checked for torso/legs slots
            # where body shape matters, unchecked for head/face/feet/accessories.
            BODY_VARIANT_SLOTS = {'GenericInnerChestClothing', 'GenericOuterChestClothing', 'GenericLegClothing'}
            needs_body_variants = tk.BooleanVar(value=it['slot'] in BODY_VARIANT_SLOTS)
            body_var_cb = tk.Checkbutton(
                body_frame, text="Needs body-type variants", variable=needs_body_variants,
                bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 8)
            )
            body_var_cb.grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 3))
            ToolTip(body_var_cb, "Auto-checked for Torso/Legs. Uncheck for accessories that don't need per-body meshes (faster compile).")

            def _toggle_body_var(event=None, bv=base_var, var=needs_body_variants):
                var.set(bv.get() in BODY_VARIANT_SLOTS)
            base_combo.bind('<<ComboboxSelected>>', _toggle_body_var)

            # Separator so the "in-game text" fields read as their own group
            tk.Frame(body_frame, bg=C['border'], height=1).grid(row=4, column=0, columnspan=2, sticky='ew', pady=(8, 6))

            # Display Name (what players see in the inventory)
            tk.Label(body_frame, text="Display Name *:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=5, column=0, sticky='w', pady=3)
            display_name_var = tk.StringVar(value=it.get('display_name', it['name'].replace('_', ' ').title()))
            display_entry = tk.Entry(
                body_frame, textvariable=display_name_var, bg=C['bg_input'], fg=C['text'],
                insertbackground=C['cyan'], relief='flat', font=(FONT, 8),
                highlightthickness=1, highlightbackground=C['border'], highlightcolor=C['cyan']
            )
            display_entry.grid(row=5, column=1, sticky='ew', padx=(6, 0), pady=3)

            # Description (what players read in the inventory tooltip)
            tk.Label(body_frame, text="Description *:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=6, column=0, sticky='w', pady=3)
            description_var = tk.StringVar(value=it.get('description', ''))
            description_entry = tk.Entry(
                body_frame, textvariable=description_var, bg=C['bg_input'], fg=C['text'],
                insertbackground=C['cyan'], relief='flat', font=(FONT, 8),
                highlightthickness=1, highlightbackground=C['border'], highlightcolor=C['cyan']
            )
            description_entry.grid(row=6, column=1, sticky='ew', padx=(6, 0), pady=3)

            # Rarity / Quality Tier
            tk.Label(body_frame, text="Rarity:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=7, column=0, sticky='w', pady=3)
            quality_labels = [label for _, label in QUALITY_TIERS]
            quality_lookup = {label: token for token, label in QUALITY_TIERS}
            quality_reverse = {token: label for token, label in QUALITY_TIERS}
            quality_var = tk.StringVar(value=quality_reverse.get(it.get('quality', QUALITY_TIERS[0][0]), quality_labels[0]))
            quality_combo = ttk.Combobox(body_frame, textvariable=quality_var, values=quality_labels, state="readonly", font=(FONT, 8))
            quality_combo.grid(row=7, column=1, sticky='ew', padx=(6, 0), pady=3)

            # Icon Handling Mode
            tk.Label(body_frame, text="Icon:", bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8)).grid(row=8, column=0, sticky='w', pady=3)
            icon_labels = [label for _, label in ICON_MODES]
            icon_lookup = {label: token for token, label in ICON_MODES}
            icon_reverse = {token: label for token, label in ICON_MODES}
            icon_mode_var = tk.StringVar(value=icon_reverse.get(it.get('icon_mode', ICON_MODES[0][0]), icon_labels[0]))
            icon_combo = ttk.Combobox(body_frame, textvariable=icon_mode_var, values=icon_labels, state="readonly", font=(FONT, 8))
            icon_combo.grid(row=8, column=1, sticky='ew', padx=(6, 0), pady=3)

            icon_hint = tk.Label(
                body_frame,
                text=f"Custom mode needs: {it['name']}_icons.xbm sliced 160px per color",
                bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic'), anchor='w'
            )
            icon_hint.grid(row=9, column=0, columnspan=2, sticky='w', pady=(0, 3))

            def _toggle_icon_hint(event, hint=icon_hint, var=icon_mode_var, lookup=icon_lookup):
                hint.config(fg=C['orange'] if lookup.get(var.get()) == 'custom' else C['text_dim'])
            icon_combo.bind('<<ComboboxSelected>>', _toggle_icon_hint)
            _toggle_icon_hint(None)

            # 5a: Icon sheet preview — show thumbnails of source icon PNGs
            #     if they exist in the item's export folder.
            icon_preview_frame = tk.Frame(body_frame, bg=C['bg_panel'])
            icon_preview_frame.grid(row=10, column=0, columnspan=2, sticky='w', pady=(0, 3))
            try:
                from PIL import Image, ImageTk
                import glob as _glob
                item_path = Path(it.get('path', ''))
                icon_files = sorted(
                    p for p in item_path.glob('*_icon_*.png')
                ) + sorted(
                    p for p in item_path.glob('*_icon_*.jpg')
                )
                if icon_files:
                    thumb_size = (32, 32)
                    thumbs = []
                    for img_path in icon_files[:8]:
                        try:
                            img = Image.open(str(img_path))
                            img.thumbnail(thumb_size, Image.LANCZOS)
                            thumbs.append(ImageTk.PhotoImage(img))
                        except Exception:
                            pass
                    if thumbs:
                        tk.Label(
                            icon_preview_frame, text="Source icons:",
                            bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic')
                        ).pack(side='left', padx=(0, 4))
                        for thumb in thumbs:
                            lbl = tk.Label(icon_preview_frame, image=thumb, bg=C['bg_panel'])
                            lbl.image = thumb
                            lbl.pack(side='left', padx=2)
                    else:
                        tk.Label(
                            icon_preview_frame, text="Custom mode needs: *_icon_*.png files in export folder",
                            bg=C['bg_panel'], fg=C['orange'], font=(FONT, 7, 'italic')
                        ).pack(side='left')
                else:
                    tk.Label(
                        icon_preview_frame, text="Custom mode needs: *_icon_*.png files in export folder",
                        bg=C['bg_panel'], fg=C['orange'], font=(FONT, 7, 'italic')
                    ).pack(side='left')
            except ImportError:
                tk.Label(
                    icon_preview_frame, text="(Install Pillow for icon previews: pip install Pillow)",
                    bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic')
                ).pack(side='left')

            # Summary Label describing found textures and meshes
            glb_summary = ", ".join([g.name for g in it['glbs'][:3]])
            if len(it['glbs']) > 3:
                glb_summary += f" (+{len(it['glbs'])-3} more)"
            
            summary_lbl = tk.Label(
                body_frame, text=f"Matching Assets: {glb_summary}",
                bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 8, 'italic'), anchor='w'
            )
            summary_lbl.grid(row=11, column=0, columnspan=2, sticky='w', pady=(6, 0))

            # ── Per-material settings (base material + two-sided) ──
            # Extract material names from the first GLB file, then show a
            # checkbox row for each one (transparent + two-sided).
            mat_names = get_glb_material_names(str(it['glbs'][0])) if it['glbs'] else []
            saved_mat = it.get('material_settings', {})

            mat_settings_frame = tk.Frame(body_frame, bg=C['bg_panel'])
            mat_settings_frame.grid(row=12, column=0, columnspan=2, sticky='ew', pady=(6, 0))
            mat_settings_frame.columnconfigure(1, weight=1)

            tk.Label(
                mat_settings_frame, text="Material Settings:",
                bg=C['bg_panel'], fg=C['text_label'], font=(FONT, 8, 'bold')
            ).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 4))

            mat_settings_vars = {}
            if mat_names:
                for mi, mn in enumerate(mat_names):
                    ms = saved_mat.get(mn, {})
                    row = mi + 1

                    tk.Label(
                        mat_settings_frame, text=mn,
                        bg=C['bg_panel'], fg=C['text'], font=(FONT, 8), anchor='w'
                    ).grid(row=row, column=0, sticky='w', pady=2)

                    tr_var = tk.BooleanVar(value=ms.get('transparent', False))
                    tr_cb = tk.Checkbutton(
                        mat_settings_frame, text="Transparent", variable=tr_var,
                        bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                        activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 7)
                    )
                    tr_cb.grid(row=row, column=1, sticky='w', padx=(6, 4), pady=2)
                    ToolTip(tr_cb, "Enable alpha masking — diffuse texture alpha becomes the opacity mask (for fishnets, lace, etc.)")

                    ts_var = tk.BooleanVar(value=ms.get('two_sided', False))
                    ts_cb = tk.Checkbutton(
                        mat_settings_frame, text="2-sided", variable=ts_var,
                        bg=C['bg_panel'], fg=C['text_label'], selectcolor=C['bg_input'],
                        activebackground=C['bg_panel'], activeforeground=C['text_label'], font=(FONT, 7)
                    )
                    ts_cb.grid(row=row, column=2, sticky='w', pady=2)

                    mat_settings_vars[mn] = {'transparent_var': tr_var, 'two_sided_var': ts_var}
            else:
                tk.Label(
                    mat_settings_frame, text="(no GLB files found — materials unknown)",
                    bg=C['bg_panel'], fg=C['text_dim'], font=(FONT, 7, 'italic')
                ).grid(row=1, column=0, columnspan=3, sticky='w')

            # Store controls to pull values inside compilation threads
            self.item_widgets.append({
                'name': it['name'],
                'path': it['path'],
                'glbs': it['glbs'],
                'enabled_var': enabled_var,
                'base_var': base_var,
                'eq_var': eq_var,
                'foot_flat_var': foot_flat_var,
                'foot_lifted_var': foot_lifted_var,
                'foot_heel_var': foot_heel_var,
                'needs_body_variants': needs_body_variants,
                'display_name_var': display_name_var,
                'description_var': description_var,
                'quality_var': quality_var,
                'quality_lookup': quality_lookup,
                'icon_mode_var': icon_mode_var,
                'icon_lookup': icon_lookup,
                'mat_settings_vars': mat_settings_vars,
                'has_foot_variants': has_foot_variants,
            })
        self._update_scrollbar_visibility()


    def _build_right_console(self, parent):
        outer = tk.Frame(parent, bg=C['border'])
        outer.pack(fill='both', expand=True)

        inner = tk.Frame(outer, bg=C['bg_panel'])
        inner.pack(fill='both', expand=True, padx=1, pady=1)

        t_bar = tk.Frame(inner, bg=C['bg_panel_alt'], height=30)
        t_bar.pack(fill='x')
        t_bar.pack_propagate(False)
        tk.Label(
            t_bar, text=" 🖥  LIVE COMPILER PROCESS CONSOLE",
            bg=C['bg_panel_alt'], fg=C['text_heading'], font=(FONT, 9, 'bold'), anchor='w'
        ).pack(side='left', padx=8, pady=4)

        tk.Button(
            t_bar, text="Clear", bg=C['btn_primary'], fg=C['text_dim'], font=(FONT, 8), relief='flat', cursor='hand2', width=6,
            command=self._clear_console
        ).pack(side='right', padx=8, pady=4)

        console_frame = tk.Frame(inner, bg=C['bg_console'])
        console_frame.pack(fill='both', expand=True, padx=4, pady=4)

        self.console = tk.Text(
            console_frame, bg=C['bg_console'], fg=C['text'], font=(FONT_MONO, 8), relief='flat', wrap='word',
            state='disabled', padx=8, pady=6, insertbackground=C['cyan']
        )
        scrollbar = tk.Scrollbar(console_frame, orient='vertical', command=self.console.yview)
        scrollbar.pack(side='right', fill='y')
        self.console.pack(side='left', fill='both', expand=True)
        self.console.config(yscrollcommand=scrollbar.set)

        self.console.tag_configure('info',   foreground=C['text'])
        self.console.tag_configure('ok',     foreground=C['green'])
        self.console.tag_configure('warn',   foreground=C['yellow'])
        self.console.tag_configure('error',  foreground=C['red'])
        self.console.tag_configure('header', foreground=C['cyan'])


    def _log(self, msg: str, tag: str = 'info'):
        self.log_queue.put((msg, tag))

    def _poll_log(self):
        try:
            while True:
                msg, tag = self.log_queue.get_nowait()
                self.console.config(state='normal')
                timestamp = datetime.now().strftime('%H:%M:%S')
                prefix_map = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ ', 'header': ''}
                prefix = prefix_map.get(tag, '  ')
                if tag == 'header':
                    self.console.insert('end', f"{msg}\n", tag)
                    line_for_file = msg
                else:
                    line = f" {timestamp}  {prefix}{msg}\n"
                    self.console.insert('end', line, tag)
                    line_for_file = line.rstrip('\n')
                if self._log_file:
                    try:
                        self._log_file.write(line_for_file + '\n')
                        self._log_file.flush()
                    except Exception:
                        pass
                # BUG 13 fix: calling .see('end') synchronously during a burst of
                # rapid log lines can lag behind — scheduling it on the idle
                # queue instead lets it always resolve to the true latest end
                # position once the event loop catches its breath.
                self.root.after_idle(lambda: self.console.see('end'))
                self.console.config(state='disabled')
        except queue.Empty:
            pass
        self.root.after(80, self._poll_log)

    def _clear_console(self):
        self.console.config(state='normal')
        self.console.delete('1.0', 'end')
        self.console.config(state='disabled')

    def _restore_fields(self):
        field_map = [
            (self.e_blender, 'blender_dir'),
            (self.e_output,  'output_dir'),
            (self.e_mod_base,'mod_base_name'),
            (self.e_author,  'mod_author'),
            (self.e_display, 'display_prefix'),
            (self.e_colors,  'colors'),
            (self.e_cli,     'wkit_cli'),
        ]
        for entry, key in field_map:
            val = self.cfg.get(key)
            if val:
                entry.insert(0, val)
        saved_body_types = self.cfg.get('body_types')
        if saved_body_types:
            checked = {t.strip().lower() for t in saved_body_types.split(',') if t.strip()}
            for token, var in self.e_body_types.items():
                var.set(token in checked)

    def _save_fields(self):
        field_map = [
            (self.e_blender, 'blender_dir'),
            (self.e_output,  'output_dir'),
            (self.e_mod_base,'mod_base_name'),
            (self.e_author,  'mod_author'),
            (self.e_display, 'display_prefix'),
            (self.e_colors,  'colors'),
            (self.e_cli,     'wkit_cli'),
        ]
        for entry, key in field_map:
            self.cfg.set(key, entry.get().strip())
        checked_tokens = [token for token, var in self.e_body_types.items() if var.get()]
        self.cfg.set('body_types', ', '.join(checked_tokens))
        self.cfg.set('auto_compile', self.auto_compile_var.get())
        self.cfg.set('geometry', self.root.geometry())
        self.cfg.save()

    def _get_fields(self) -> dict:
        checked_tokens = [token for token, var in self.e_body_types.items() if var.get()]
        return {
            'blender_dir':    self.e_blender.get().strip(),
            'output_dir':     self.e_output.get().strip(),
            'mod_base':       self.e_mod_base.get().strip(),
            'author':         self.e_author.get().strip(),
            'display_prefix': self.e_display.get().strip(),
            'colors':         self.e_colors.get().strip(),
            'body_types':     ', '.join(checked_tokens) or DEFAULT_BODY_TOKENS,
            'cli_path':       self.e_cli.get().strip(),
            'auto_compile':   self.auto_compile_var.get(),
        }


    def _validate(self, fields, need_cli=False) -> bool:
        errors = []
        if not fields['blender_dir']:
            errors.append("Blender Exports Directory path is empty")
        elif not Path(fields['blender_dir']).is_dir():
            errors.append(f"Blender exports path is invalid: {fields['blender_dir']}")
        if not fields['output_dir']:
            errors.append("WolvenKit outputs project directory path is empty")
        if not fields['mod_base']:
            errors.append("Mod Base ID is required")
        if not fields['colors']:
            errors.append("Color variants field is empty")
        if need_cli and not fields['cli_path']:
            errors.append("WolvenKit CLI binary path is empty")

        if errors:
            for e in errors:
                self._log(e, 'error')
            return False
        return True

    def _set_running(self, running: bool, error: bool = False):
        self._running = running
        state = 'disabled' if running else 'normal'
        for btn in (self.btn_scan, self.btn_preview, self.btn_run, self.btn_compile):
            btn.config(state=state)
        self.btn_cancel.config(state='normal' if running else 'disabled')
        if running:
            self._cancel_var.set(False)
            self._progress['value'] = 0
            self._status_label.config(text="● ACTIVE COMPILATION IN PROCESS…", fg=C['orange'])
        elif error:
            self._status_label.config(text="● ERROR — SEE CONSOLE", fg=C['red'])
        else:
            self._progress['value'] = self._progress['maximum']
            self._status_label.config(text="● READY", fg=C['green'])

    def _on_cancel(self):
        """Signal the worker thread to abort at the next cancel-check point."""
        self._cancel_var.set(True)
        if hasattr(self, '_worker') and self._worker:
            self._worker._cancel_event.set()
        self._status_label.config(text="● CANCELLING…", fg=C.get('btn_warn', '#8b4513'))

    def _update_progress(self, step: int, total: int):
        # BUG 12: called from PipelineWorker's progress_fn callback (marshalled
        # onto the main thread) to move the progress bar to a real step count
        # instead of it just spinning indeterminately with no sense of how
        # far along a long CLI compile actually is.
        self._progress['maximum'] = max(total, 1)
        self._progress['value'] = step

    def _run_in_thread(self, target, *args):
        def wrapper():
            crashed = False
            try:
                target(*args)
            except Exception as e:
                crashed = True
                self._log(f"Process pipeline crashed: {e}", 'error')
                import traceback
                self._log(traceback.format_exc(), 'error')
            finally:
                # BUG 11 fix: previously a crash was only ever visible as a
                # scrolled-past console line — the status pill at the top of
                # the window would just silently flip back to "READY" as if
                # nothing had gone wrong. Now a crash leaves a persistent red
                # error state until the user starts a new run.
                self.root.after(0, lambda: self._set_running(False, error=crashed))

        self._set_running(True)
        t = threading.Thread(target=wrapper, daemon=True)
        t.start()

    def _on_scan(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not fields['blender_dir'] or not Path(fields['blender_dir']).is_dir():
            self._log("Enter a valid Blender Exports Directory first", 'error')
            return

        def do_scan():
            worker = PipelineWorker(self._log)
            self._log("━━━  PREVIEW SCAN EXPORTS INITIATED  ━━━", 'header')
            items = worker.scan_export_dir(fields['blender_dir'])
            if items:
                self._log(f"Successfully mapped {len(items)} subfolders. Populating Item Manager...", 'ok')
                self.scanned_items = items
                # Safely update GUI elements inside the main thread
                self.root.after(0, self._populate_item_manager)
            else:
                self._log("No valid clothing assets found inside target exports folder.", 'warn')
                self.scanned_items = []
                self.root.after(0, self._show_middle_placeholder)

        self._run_in_thread(do_scan)

    def _on_preview(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not fields['blender_dir'] or not Path(fields['blender_dir']).is_dir():
            self._log("Enter a valid Blender Exports Directory first", 'error')
            return
        if not fields['output_dir'] or not Path(fields['output_dir']).is_dir():
            self._log("Enter a valid WolvenKit Project Directory first", 'error')
            return

        def do_preview():
            worker = PipelineWorker(self._log)
            self._log("━━━  DRY-RUN PREVIEW (no files will be modified)  ━━━", 'header')

            # Re-use scanned items if available, otherwise scan now
            items = getattr(self, 'scanned_items', None)
            if not items:
                self._log("No scanned items found. Running scan first...", 'info')
                items = worker.scan_export_dir(fields['blender_dir'])
                if not items:
                    self._log("No valid clothing assets found.", 'warn')
                    return
                self.scanned_items = items
                self.root.after(0, self._populate_item_manager)

            # Gather active items from UI
            active_items = []
            if hasattr(self, 'item_widgets'):
                for w in self.item_widgets:
                    ts_list, ms_dict = extract_mat_settings(w)
                    active_items.append({
                        'name': w['name'],
                        'enabled': w['enabled_var'].get(),
                        'slot': w['base_var'].get(),
                        'needs_body_variants': w['needs_body_variants'].get(),
                        'icon_mode': w['icon_lookup'].get(w['icon_mode_var'].get(), 'custom'),
                        'glbs': w['glbs'],
                        'two_sided_materials': ts_list,
                        'material_settings': ms_dict,
                        'has_foot_variants': w['has_foot_variants'].get(),
                    })
            else:
                for s in items:
                    active_items.append({'name': s['name'], 'enabled': True})

            colors = worker.parse_colors(fields['colors'])
            body_suffixes, _ = resolve_body_suffixes(fields['body_types'])

            mod_base = fields['mod_base'].lower()
            wkit = Path(fields['output_dir']) / mod_base
            template_root = Path(__file__).parent / 'template_meshes'

            # --- Dry-run: list what WOULD be generated ---
            self._log("", 'info')
            self._log("─── Files that WOULD be generated ───", 'header')

            total_json = 0
            total_meshes = 0
            total_textures = 0
            total_icons = 0

            for it in active_items:
                if not it.get('enabled', True):
                    continue
                name = it['name']
                slot = it.get('slot', '')
                self._log(f"  [{name}] slot={slot}", 'info')

                # Project dirs
                item_arch = wkit / 'source' / 'archive' / mod_base / name
                item_raw = wkit / 'source' / 'raw' / mod_base / name

                # JSON configs (4 files)
                jsons = [
                    f"{mod_base}_{name}_root.ent.json",
                    f"{mod_base}_{name}_appearance.app.json",
                    f"{mod_base}_{name}_mesh_entity.ent.json",
                ]
                for j in jsons:
                    self._log(f"    JSON: {item_arch / j}", 'info')
                total_json += len(jsons)

                # Mesh files
                needs_body = it.get('needs_body_variants', True)
                glbs = it.get('glbs', [])
                single_glb = any(
                    g.stem.split('_', 1)[-1] == 'base_body' if '_' in g.stem else True
                    for g in glbs
                ) and len(glbs) == 1

                active_suffixes = {tok for _, tok in body_suffixes}
                if needs_body:
                    for suf in active_suffixes:
                        self._log(f"    MESH: {item_arch / 'meshes' / f'{name}_{suf}.mesh'}", 'info')
                        total_meshes += 1
                else:
                    self._log(f"    MESH: {item_arch / 'meshes' / f'{name}_{next(iter(active_suffixes))}.mesh'} (copied to all {len(active_suffixes)} body types)", 'info')
                    total_meshes += len(active_suffixes)

                # Textures
                raw_tex = item_raw / 'textures'
                if raw_tex.is_dir():
                    tex_count = len(list(raw_tex.glob('*.png'))) + len(list(raw_tex.glob('*.jpg')))
                    if tex_count:
                        self._log(f"    TEXTURES: {tex_count} file(s) → {item_arch / 'textures'}", 'info')
                        total_textures += tex_count

                # Icons
                icon_mode = it.get('icon_mode', 'custom')
                if icon_mode == 'custom':
                    raw_icon = item_raw / 'icons'
                    if raw_icon.is_dir():
                        icon_count = len(list(raw_icon.glob('*.png'))) + len(list(raw_icon.glob('*.jpg')))
                        if icon_count:
                            self._log(f"    ICONS: {icon_count} file(s) → {item_arch}", 'info')
                            total_icons += icon_count
                    # inkatlas + icons.inkatlas.json
                    self._log(f"    JSON: {item_arch / f'{name}_icons.inkatlas.json'}", 'info')
                    total_json += 1

                # Localization
                self._log(f"    JSON: localization/en-us.json.json", 'info')
                total_json += 1

            # CSV
            self._log(f"    JSON: {mod_base}.csv.json", 'info')
            total_json += 1

            # Mesh entity templates
            self._log(f"    MESH_TPL: {len(active_suffixes)} template mesh binary copies per item", 'info')

            self._log("", 'info')
            self._log("─── Summary ───", 'header')
            self._log(f"  JSON files to compile:   {total_json}", 'info')
            self._log(f"  Mesh files to generate:  {total_meshes}", 'info')
            self._log(f"  Texture files to import: {total_textures}", 'info')
            self._log(f"  Icon files to import:    {total_icons}", 'info')
            # Rough time estimate based on batch optimization
            cli_starts = len([a for a in active_items if a.get('enabled', True)]) + 2
            est_cli = cli_starts * 6
            est_textures = total_textures * 5
            self._log(f"  Estimated time: ~{est_cli + est_textures}s (batch CLI, parallel imports)", 'info')
            self._log("  (Run '▶ Process && Compile' to execute)", 'ok')

        self._run_in_thread(do_preview)

    def _on_run(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=fields['auto_compile']):
            return

        # Fetch live selected state parameters from custom middle UI widgets
        active_items = []
        if hasattr(self, 'item_widgets'):
            for w in self.item_widgets:
                ts_list, ms_dict = extract_mat_settings(w)
                active_items.append({
                    'name': w['name'],
                    'path': w['path'],
                    'glbs': w['glbs'],
                    'enabled': w['enabled_var'].get(),
                    'slot': w['base_var'].get(),
                    'eq_slot': w['eq_var'].get(),
                    'foot_states': [s for s, v in (
                        ('flat', w['foot_flat_var']), ('lifted', w['foot_lifted_var']), ('heel', w['foot_heel_var'])
                    ) if v.get()],
                    'needs_body_variants': w['needs_body_variants'].get(),
                    'display_name': w['display_name_var'].get().strip(),
                    'description': w['description_var'].get().strip(),
                    'quality': w['quality_lookup'].get(w['quality_var'].get(), QUALITY_TIERS[0][0]),
                    'icon_mode': w['icon_lookup'].get(w['icon_mode_var'].get(), ICON_MODES[0][0]),
                    'two_sided_materials': ts_list,
                    'material_settings': ms_dict,
                    'has_foot_variants': w['has_foot_variants'].get(),
                })
        else:
            # Fallback scanner if compilation triggered without running manual scan preview step first
            self._log("Warning: Triggering build directly without running scan preview. Attempting auto-scan fallback...", 'warn')
            worker = PipelineWorker(self._log)
            active_items = worker.scan_export_dir(fields['blender_dir'])

        if not active_items:
            self._log("Error: No active items configured for compilation.", 'error')
            return

        # Dummy-proof gate: every enabled item must have a display name and
        # description before we let the pipeline touch disk. Catching this
        # here beats a half-built mod with blank inventory text.
        missing = []
        png_warnings = []
        for it in active_items:
            if not it.get('enabled', True):
                continue
            if not it.get('display_name'):
                missing.append(f"'{it['name']}' is missing a Display Name")
            if not it.get('description'):
                missing.append(f"'{it['name']}' is missing a Description")
            # BUG 7: block the build if an enabled item has no GLB files at all —
            # the pipeline has nothing to generate meshes from otherwise.
            if not it.get('glbs'):
                missing.append(f"'{it['name']}' has no .glb mesh files in its export folder")
            # BUG 8: texture PNGs aren't strictly required (the @dynamic material
            # system will still compile without them, it just won't look right
            # in-game), so this is a warning rather than a hard stop.
            item_path = it.get('path')
            if item_path is not None:
                try:
                    has_png = any(_find_images(Path(item_path)))
                except Exception:
                    has_png = True  # don't block the build over a filesystem hiccup
                if not has_png:
                    png_warnings.append(it['name'])
        if missing:
            self._log("Cannot start build — fill in the following in the Item Manager first:", 'error')
            for m in missing:
                self._log(f"  ✗  {m}", 'error')
            return
        if png_warnings:
            self._log(f"Warning: no texture images found for: {', '.join(png_warnings)}", 'warn')
            self._log("  These items will still compile, but may show missing/default textures in-game.", 'warn')

        def do_run():
            self._worker = PipelineWorker(
                self._log,
                progress_fn=lambda step, total: self.root.after(0, lambda: self._update_progress(step, total))
            )
            self._worker.orchestrate_pipeline(
                blender_dir=fields['blender_dir'],
                output_dir=fields['output_dir'],
                mod_base=fields['mod_base'],
                author=fields['author'] or 'V_Designer',
                display_prefix=fields['display_prefix'] or fields['mod_base'].title(),
                colors_raw=fields['colors'],
                cli_path=fields['cli_path'],
                auto_compile=fields['auto_compile'],
                active_items=active_items,
                body_suffixes_raw=fields['body_types']
            )

        self._run_in_thread(do_run)

    def _on_compile(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=True):
            return

        # Fetch current UI assignments
        active_items = []
        if hasattr(self, 'item_widgets'):
            for w in self.item_widgets:
                active_items.append({
                    'name': w['name'],
                    'enabled': w['enabled_var'].get(),
                })
        else:
            worker = PipelineWorker(self._log)
            scanned = worker.scan_export_dir(fields['blender_dir'])
            for s in scanned:
                active_items.append({
                    'name': s['name'],
                    'enabled': True
                })

        def do_compile():
            self._worker = PipelineWorker(self._log)
            wkit = Path(fields['output_dir']) / fields['mod_base']
            if not wkit.is_dir():
                self._log("Error: Project directory does not exist! Run step '2. Process && Compile' first.", 'error')
                return
            colors = PipelineWorker(self._log).parse_colors(fields['colors'])
            self._worker.run_wolvenkit_cli(fields['cli_path'], wkit, fields['mod_base'].lower(), active_items, colors)

        self._run_in_thread(do_compile)

    def _on_compile_single(self, item_name):
        """3c: Run the full pipeline on a single item only."""
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=fields['auto_compile']):
            return

        # Gather only this item's settings from the UI
        single_item = None
        if hasattr(self, 'item_widgets'):
            for w in self.item_widgets:
                if w['name'] == item_name:
                    ts_list, ms_dict = extract_mat_settings(w)
                    single_item = {
                        'name': w['name'],
                        'path': w['path'],
                        'glbs': w['glbs'],
                        'enabled': True,
                        'slot': w['base_var'].get(),
                        'eq_slot': w['eq_var'].get(),
                        'foot_states': [s for s, v in (
                            ('flat', w['foot_flat_var']), ('lifted', w['foot_lifted_var']), ('heel', w['foot_heel_var'])
                        ) if v.get()],
                        'needs_body_variants': w['needs_body_variants'].get(),
                        'display_name': w['display_name_var'].get().strip(),
                        'description': w['description_var'].get().strip(),
                        'quality': w['quality_lookup'].get(w['quality_var'].get(), QUALITY_TIERS[0][0]),
                        'icon_mode': w['icon_lookup'].get(w['icon_mode_var'].get(), ICON_MODES[0][0]),
                        'two_sided_materials': ts_list,
                        'material_settings': ms_dict,
                        'has_foot_variants': w['has_foot_variants'].get(),
                    }
                    break
        if not single_item:
            self._log(f"Item '{item_name}' not found in Item Manager. Run scan first.", 'error')
            return

        self._log(f"━━━  SINGLE ITEM COMPILE: {item_name}  ━━━", 'header')

        def do_single():
            self._worker = PipelineWorker(
                self._log,
                progress_fn=lambda step, total: self.root.after(0, lambda: self._update_progress(step, total))
            )
            self._worker.orchestrate_pipeline(
                blender_dir=fields['blender_dir'],
                output_dir=fields['output_dir'],
                mod_base=fields['mod_base'],
                author=fields['author'] or 'V_Designer',
                display_prefix=fields['display_prefix'] or fields['mod_base'].title(),
                colors_raw=fields['colors'],
                cli_path=fields['cli_path'],
                auto_compile=fields['auto_compile'],
                active_items=[single_item],
                body_suffixes_raw=fields['body_types']
            )

        self._run_in_thread(do_single)

    def _on_close(self):
        self._save_fields()
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == '__main__':
    app = CPMPApp()
    app.run()