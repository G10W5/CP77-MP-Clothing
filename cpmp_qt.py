#!/usr/bin/env python3
"""
CP77 Modding Pipeline (CPMP) - Qt Docking Edition
=========================================================
Same pipeline, new shell. This file replaces ONLY the GUI layer of cpmp.py
(the old `CPMPApp` tkinter class) with a PySide6 QMainWindow built entirely
out of QDockWidgets - the same mechanism WolvenKit's own layout uses
(AvalonDock in WPF land; QDockWidget is Qt's equivalent).

Requirements:
    pip install PySide6

Run:
    python cpmp_qt.py
    (cpmp.py, cr2w_mesh.py and make_icon_sheet.py must be next to this file,
    same as before)
"""

import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal, QSettings, QTimer
from PySide6.QtGui import QAction, QTextCursor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QCheckBox,
    QComboBox, QPlainTextEdit, QScrollArea, QGroupBox, QProgressBar,
    QFileDialog, QMessageBox, QFrame
)

# ─────────────────────────────────────────────────────────────────────────
# Reuse every non-GUI piece of the existing pipeline unmodified.
# cpmp.py is guarded by `if __name__ == '__main__':`, so importing it here
# does NOT open the old tkinter window.
# ─────────────────────────────────────────────────────────────────────────
from cpmp import (
    PipelineWorker, ConfigManager, resolve_body_suffixes, extract_mat_settings, _find_images,
    BASE_SLOTS, EQUIPMENT_EX_SLOTS, QUALITY_TIERS, ICON_MODES,
    BODY_SUFFIX_CATALOG, BODY_TYPE_TOOLTIPS, DEFAULT_BODY_TOKENS, VERSION,
)
from cr2w_mesh import get_glb_material_names

APP_ORG = "CPMP"
APP_NAME = "CP77 Modding Pipeline"
BODY_VARIANT_SLOTS = {'GenericInnerChestClothing', 'GenericOuterChestClothing', 'GenericLegClothing'}

# ─────────────────────────────────────────────────────────────────────────
# Dark theme, ported from the tkinter C{} token dict
# ─────────────────────────────────────────────────────────────────────────
QSS = """
QMainWindow, QWidget { background: #08080e; color: #e2e2f0; font-family: 'Segoe UI'; font-size: 9pt; }
QDockWidget { color: #e2e2f0; titlebar-close-icon: none; }
QDockWidget::title {
    background: #16162a; padding: 6px 8px; border: 1px solid #28284a;
    font-weight: bold;
}
QGroupBox {
    background: #121220; border: 1px solid #28284a; border-radius: 3px;
    margin-top: 14px; padding: 8px; font-weight: bold; color: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #00d4ff; }
QLabel { color: #b0b0d0; }
QLabel[role="heading"] { color: #ffffff; font-weight: bold; }
QLabel[role="dim"] { color: #7a7a9e; font-style: italic; font-size: 8pt; }
QLineEdit, QComboBox {
    background: #1a1a30; color: #e2e2f0; border: 1px solid #28284a; border-radius: 2px; padding: 4px;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #00d4ff; }
QComboBox QAbstractItemView { background: #1a1a30; color: #e2e2f0; selection-background-color: #222240; }
QCheckBox { color: #b0b0d0; spacing: 6px; }
QPushButton {
    background: #0d3860; color: #e2e2f0; border: none; border-radius: 3px; padding: 6px 10px; font-weight: bold;
}
QPushButton:hover { background: #222240; }
QPushButton:disabled { background: #16162a; color: #7a7a9e; }
QPushButton[cls="run"] { background: #143a1e; }
QPushButton[cls="compile"] { background: #3d2a08; }
QPushButton[cls="cancel"] { background: #8b4513; }
QProgressBar { background: #1a1a30; border: 1px solid #28284a; border-radius: 2px; text-align: center; color: #e2e2f0; }
QProgressBar::chunk { background: #00d4ff; }
QPlainTextEdit { background: #0a0a10; color: #e2e2f0; border: none; font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 8pt; }
QScrollArea { background: #0a0a10; border: none; }
QGroupBox#itemCard { background: #121220; border: 1px solid #28284a; }
QToolTip {
    background-color: #1a1a30; color: #e2e2f0; border: 1px solid #00d4ff;
    padding: 4px; font-size: 8pt;
}
"""

TAG_COLORS = {
    'info': '#e2e2f0', 'ok': '#00ff88', 'warn': '#ffe066', 'error': '#ff3860', 'header': '#00d4ff',
}


# ═══════════════════════════════════════════════════════════════════════
# Background worker: runs PipelineWorker methods off the GUI thread and
# marshals log/progress callbacks back via Qt signals (thread-safe).
# ═══════════════════════════════════════════════════════════════════════
class PipelineRunner(QObject):
    log_line = Signal(str, str)
    progress = Signal(int, int)
    result_ready = Signal(object)
    finished = Signal(bool)   # True = crashed

    def __init__(self, job_fn):
        super().__init__()
        self._job_fn = job_fn
        self.worker = PipelineWorker(self._log, progress_fn=self._progress)

    def _log(self, msg, tag='info'):
        self.log_line.emit(str(msg), tag)

    def _progress(self, step, total):
        self.progress.emit(step, total)

    def run(self):
        crashed = False
        result = None
        try:
            result = self._job_fn(self.worker)
        except InterruptedError:
            self._log("Pipeline cancelled by user.", 'warn')
        except Exception as e:
            crashed = True
            import traceback
            self._log(f"Process pipeline crashed: {e}", 'error')
            self._log(traceback.format_exc(), 'error')
        if not crashed:
            self.result_ready.emit(result)
        self.finished.emit(crashed)


# ═══════════════════════════════════════════════════════════════════════
# Item Manager card - one per scanned clothing folder
# ═══════════════════════════════════════════════════════════════════════
class ItemCard(QGroupBox):
    def __init__(self, item: dict, compile_single_cb):
        super().__init__(f"Active Folder: '{item['name']}'")
        self.setObjectName("itemCard")
        self.setCheckable(True)
        self.setChecked(item.get('enabled', True))
        self.item = item

        root = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addStretch(1)
        btn_single = QPushButton("Compile This Item")
        btn_single.setProperty("cls", "compile")
        btn_single.clicked.connect(lambda: compile_single_cb(item['name']))
        top.addWidget(btn_single)
        root.addLayout(top)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.base_combo = QComboBox()
        for token, label in BASE_SLOTS:
            self.base_combo.addItem(label, token)
        idx = self.base_combo.findData(item['slot'])
        self.base_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Vanilla Base Category:", self.base_combo)

        # Foot-state row, only relevant/visible for the Feet slot
        # and for Leg slot when "has foot state variants" is checked.
        self.foot_row = QWidget()
        foot_layout = QHBoxLayout(self.foot_row)
        foot_layout.setContentsMargins(0, 0, 0, 0)
        self.foot_flat = QCheckBox("Flat")
        self.foot_lifted = QCheckBox("Lifted")
        self.foot_heel = QCheckBox("Heel")
        for cb, key in ((self.foot_flat, 'flat'), (self.foot_lifted, 'lifted'), (self.foot_heel, 'heel')):
            cb.setChecked(key in item.get('foot_states', []))
            foot_layout.addWidget(cb)
        hint = QLabel("(Lifted = default, no tag needed)")
        hint.setProperty("role", "dim")
        foot_layout.addWidget(hint)
        foot_layout.addStretch(1)
        form.addRow("Foot State:", self.foot_row)
        self.foot_row_label = form.labelForField(self.foot_row)

        self.has_foot_variants = QCheckBox("Has foot state variants")
        self.has_foot_variants.setChecked(item.get('has_foot_variants', False))
        self.has_foot_variants.setToolTip(
            "Check if this leg item has separate GLBs per foot state (e.g. tights with flat/high heel variants)."
        )
        form.addRow("", self.has_foot_variants)

        self.eq_combo = QComboBox()
        for token, label in EQUIPMENT_EX_SLOTS:
            self.eq_combo.addItem(label, token)
        idx = self.eq_combo.findData(item['eq_slot'])
        self.eq_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Equipment-EX Outfit Slot:", self.eq_combo)

        self.needs_body_variants = QCheckBox("Needs body-type variants")
        self.needs_body_variants.setChecked(item['slot'] in BODY_VARIANT_SLOTS)
        self.needs_body_variants.setToolTip(
            "Auto-checked for Torso/Legs. Uncheck for accessories that don't need per-body meshes (faster compile)."
        )
        form.addRow("", self.needs_body_variants)

        def _sync_slot_dependents():
            slot = self.base_combo.currentData()
            is_feet = slot == 'GenericFootClothing'
            is_leg = slot == 'GenericLegClothing'
            self.foot_row_label.setVisible(is_feet or (is_leg and self.has_foot_variants.isChecked()))
            self.foot_row.setVisible(is_feet or (is_leg and self.has_foot_variants.isChecked()))
            self.has_foot_variants.setVisible(is_leg)
            self.needs_body_variants.setChecked(slot in BODY_VARIANT_SLOTS)
        self.base_combo.currentIndexChanged.connect(lambda _: _sync_slot_dependents())
        self.has_foot_variants.stateChanged.connect(lambda _: _sync_slot_dependents())
        _sync_slot_dependents()

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background:#28284a;")
        form.addRow(sep)

        self.display_name = QLineEdit(item.get('display_name', item['name'].replace('_', ' ').title()))
        form.addRow("Display Name *:", self.display_name)

        self.description = QLineEdit(item.get('description', ''))
        form.addRow("Description *:", self.description)

        self.quality_combo = QComboBox()
        for token, label in QUALITY_TIERS:
            self.quality_combo.addItem(label, token)
        idx = self.quality_combo.findData(item.get('quality', QUALITY_TIERS[0][0]))
        self.quality_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Rarity:", self.quality_combo)

        self.icon_combo = QComboBox()
        for token, label in ICON_MODES:
            self.icon_combo.addItem(label, token)
        idx = self.icon_combo.findData(item.get('icon_mode', ICON_MODES[0][0]))
        self.icon_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Icon:", self.icon_combo)

        icon_hint = QLabel(f"Custom mode needs: {item['name']}_icons.xbm sliced 160px per color")
        icon_hint.setProperty("role", "dim")
        form.addRow("", icon_hint)

        def _sync_icon_hint():
            custom = self.icon_combo.currentData() == 'custom'
            icon_hint.setStyleSheet(f"color: {'#ff9f1a' if custom else '#7a7a9e'}; font-style: italic;")
        self.icon_combo.currentIndexChanged.connect(lambda _: _sync_icon_hint())
        _sync_icon_hint()

        glb_names = ", ".join(g.name for g in item['glbs'][:3])
        if len(item['glbs']) > 3:
            glb_names += f" (+{len(item['glbs']) - 3} more)"
        assets_lbl = QLabel(f"Matching Assets: {glb_names}")
        assets_lbl.setProperty("role", "dim")
        form.addRow(assets_lbl)

        # Icon sheet preview - thumbnails of source icon PNGs/JPGs sitting in
        # the item's export folder, so you can eyeball what's there without
        # tabbing out to a file browser. Qt loads PNG/JPG natively via
        # QPixmap, so unlike the tkinter version this needs no Pillow install.
        icon_preview_row = QHBoxLayout()
        item_path = Path(item.get('path', '.'))
        icon_files = sorted(item_path.glob('*_icon_*.png')) + sorted(item_path.glob('*_icon_*.jpg'))
        if icon_files:
            tag = QLabel("Source icons:")
            tag.setProperty("role", "dim")
            icon_preview_row.addWidget(tag)
            for img_path in icon_files[:8]:
                pix = QPixmap(str(img_path))
                thumb_lbl = QLabel()
                if not pix.isNull():
                    thumb_lbl.setPixmap(pix.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    thumb_lbl.setToolTip(img_path.name)
                else:
                    thumb_lbl.setText("?")
                thumb_lbl.setFixedSize(32, 32)
                thumb_lbl.setStyleSheet("border: 1px solid #28284a; background:#0a0a10;")
                icon_preview_row.addWidget(thumb_lbl)
            if len(icon_files) > 8:
                more_lbl = QLabel(f"+{len(icon_files) - 8} more")
                more_lbl.setProperty("role", "dim")
                icon_preview_row.addWidget(more_lbl)
        else:
            icon_warn = QLabel("Custom mode needs: *_icon_*.png files in export folder")
            icon_warn.setStyleSheet("color:#ff9f1a; font-style: italic; font-size: 8pt;")
            icon_preview_row.addWidget(icon_warn)
        icon_preview_row.addStretch(1)
        icon_preview_container = QWidget()
        icon_preview_container.setLayout(icon_preview_row)
        form.addRow(icon_preview_container)

        # ── Per-material settings (transparent + two-sided) ──
        mat_names = get_glb_material_names(str(item['glbs'][0])) if item['glbs'] else []
        saved_mat = item.get('material_settings', {})

        self.mat_settings_widgets = {}
        if mat_names:
            mat_group = QGroupBox("Material Settings")
            mat_form = QFormLayout()
            mat_form.setLabelAlignment(Qt.AlignRight)
            for mn in mat_names:
                ms = saved_mat.get(mn, {})

                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)

                tr_cb = QCheckBox("Transparent")
                tr_cb.setChecked(ms.get('transparent', False))
                tr_cb.setToolTip("Enable alpha masking - diffuse texture alpha becomes the opacity mask (for fishnets, lace, etc.)")
                row_layout.addWidget(tr_cb)

                ts_cb = QCheckBox("2-sided")
                ts_cb.setChecked(ms.get('two_sided', False))
                row_layout.addWidget(ts_cb)
                row_layout.addStretch(1)

                mat_form.addRow(f"{mn}:", row_widget)
                self.mat_settings_widgets[mn] = {'transparent_cb': tr_cb, 'two_sided_cb': ts_cb}

            mat_group.setLayout(mat_form)
            form.addRow(mat_group)
        else:
            no_mat_lbl = QLabel("(no GLB files found - materials unknown)")
            no_mat_lbl.setProperty("role", "dim")
            form.addRow("Materials:", no_mat_lbl)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("background:#28284a;")
        form.addRow(sep2)

        self.apply_garment_support = QCheckBox("Apply GarmentSupport from GLB")
        self.apply_garment_support.setChecked(item.get('apply_garment_support', False))
        self.apply_garment_support.setToolTip(
            "Read the GarmentSupport morph target from the GLB and write it into the mesh's morphOffsets buffer.\n\n"
            "LEAVE OFF if you apply garment support manually in Blender (recommended — the game will auto-shrink "
            "under jackets/boots based on garment score, but you control the deformation).\n\n"
            "When OFF, the pipeline writes a zero-filled morphOffsets buffer so the game doesn't crash."
        )
        form.addRow(self.apply_garment_support)

        self.disable_garment_support = QCheckBox("Disable GarmentSupport (stopgap)")
        self.disable_garment_support.setChecked(item.get('disable_garment_support', False))
        self.disable_garment_support.setToolTip(
            "STOPGAP OPTION: Disables GarmentSupport entirely. The mesh will NOT auto-shrink under other items.\n\n"
            "Use this if the mesh 'explodes' or looks distorted when equipped alongside other clothing items.\n\n"
            "This is a temporary fix while the GarmentSupport color attribute requirements are being resolved."
        )
        form.addRow(self.disable_garment_support)

        root.addLayout(form)

    def to_active_item(self) -> dict:
        it = self.item
        mat_settings = {}
        two_sided = []
        for mn, ws in self.mat_settings_widgets.items():
            transparent = ws['transparent_cb'].isChecked()
            ts = ws['two_sided_cb'].isChecked()
            mat_settings[mn] = {'transparent': transparent, 'two_sided': ts}
            if ts:
                two_sided.append(mn)
        return {
            'name': it['name'],
            'path': it['path'],
            'glbs': it['glbs'],
            'enabled': self.isChecked(),
            'slot': self.base_combo.currentData(),
            'eq_slot': self.eq_combo.currentData(),
            'foot_states': [k for cb, k in (
                (self.foot_flat, 'flat'), (self.foot_lifted, 'lifted'), (self.foot_heel, 'heel')
            ) if cb.isChecked()],
            'needs_body_variants': self.needs_body_variants.isChecked(),
            'display_name': self.display_name.text().strip(),
            'description': self.description.text().strip(),
            'quality': self.quality_combo.currentData(),
            'icon_mode': self.icon_combo.currentData(),
            'two_sided_materials': two_sided,
            'material_settings': mat_settings,
            'has_foot_variants': self.has_foot_variants.isChecked(),
            'apply_garment_support': self.apply_garment_support.isChecked(),
            'disable_garment_support': self.disable_garment_support.isChecked(),
        }


# ═══════════════════════════════════════════════════════════════════════
# Main window
# ═══════════════════════════════════════════════════════════════════════
class CPMPMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"CP77 Modding Pipeline Master v{VERSION} - Equipment-EX Engine (Qt Docking)")
        self.resize(1500, 940)

        self.cfg = ConfigManager()
        self.settings = QSettings(APP_ORG, APP_NAME)
        self.scanned_items = []
        self.item_cards: list[ItemCard] = []
        self._running = False
        self._active_runner = None
        self._active_thread = None
        self._pending_on_result = None

        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)

        self._build_docks()
        self._build_menu()
        self._restore_fields()
        self._restore_layout()
        self._log(f"Ready. Drag any panel's title bar to float or re-dock it — layout is saved on exit.", 'header')

    # ── Dock construction ────────────────────────────────────────────
    def _make_dock(self, title, widget, area, object_name):
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, dock)
        return dock

    def _build_docks(self):
        self.dock_project = self._make_dock("📁 MOD COMPILER OPTIONS", self._build_project_panel(), Qt.LeftDockWidgetArea, "dockProject")
        self.dock_body = self._make_dock("🧬 BODY TYPES", self._build_body_panel(), Qt.LeftDockWidgetArea, "dockBody")
        self.dock_cli = self._make_dock("🔧 WOLVENKIT CLI TOOLS", self._build_cli_panel(), Qt.LeftDockWidgetArea, "dockCli")
        self.dock_actions = self._make_dock("⚡ PIPELINE ORCHESTRATION", self._build_actions_panel(), Qt.LeftDockWidgetArea, "dockActions")
        self.dock_items = self._make_dock("👕 DYNAMIC EQUIPMENT-EX ITEM MANAGER", self._build_item_manager_panel(), Qt.RightDockWidgetArea, "dockItems")
        self.dock_console = self._make_dock("🖥 LIVE COMPILER PROCESS CONSOLE", self._build_console_panel(), Qt.RightDockWidgetArea, "dockConsole")

        # Default layout: stack the left-column config panels on top of each
        # other so they're ALL visible on first launch (no manual tab-clicking
        # required) - closer to WolvenKit's default multi-panel layout. Users
        # remain free to drag any panel into a tab group, float it, or move
        # it elsewhere afterward; whatever they end up with gets saved.
        self.splitDockWidget(self.dock_project, self.dock_body, Qt.Vertical)
        self.splitDockWidget(self.dock_body, self.dock_cli, Qt.Vertical)
        self.splitDockWidget(self.dock_cli, self.dock_actions, Qt.Vertical)

        self.splitDockWidget(self.dock_items, self.dock_console, Qt.Vertical)
        self.resizeDocks([self.dock_project], [420], Qt.Horizontal)
        self.resizeDocks(
            [self.dock_project, self.dock_body, self.dock_cli, self.dock_actions],
            [260, 160, 140, 160], Qt.Vertical
        )

    def _build_menu(self):
        view_menu = self.menuBar().addMenu("&View")
        for dock in (self.dock_project, self.dock_body, self.dock_cli, self.dock_actions, self.dock_items, self.dock_console):
            view_menu.addAction(dock.toggleViewAction())

        window_menu = self.menuBar().addMenu("&Window")
        reset_action = QAction("Reset Layout to Default", self)
        reset_action.triggered.connect(self._reset_layout)
        window_menu.addAction(reset_action)

    # ── Panel builders ───────────────────────────────────────────────
    def _labeled_row(self, form, label, browse=False, browse_type='dir'):
        edit = QLineEdit()
        if browse:
            row = QHBoxLayout()
            row.addWidget(edit)
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda: self._browse(edit, browse_type))
            row.addWidget(btn)
            container = QWidget()
            container.setLayout(row)
            form.addRow(label, container)
        else:
            form.addRow(label, edit)
        return edit

    def _browse(self, edit, btype):
        if btype == 'dir':
            path = QFileDialog.getExistingDirectory(self, "Select Folder")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select WolvenKit CLI", filter="Executable (*.exe)")
        if path:
            edit.setText(path)

    def _build_project_panel(self):
        w = QWidget()
        form = QFormLayout(w)
        self.e_blender = self._labeled_row(form, "Blender Exports Directory", browse=True)
        self.e_output = self._labeled_row(form, "WolvenKit Projects Path", browse=True)
        self.e_mod_base = self._labeled_row(form, "Mod Base ID (e.g. gothic_dress)")
        self.e_author = self._labeled_row(form, "Author / Maker Name")
        self.e_display = self._labeled_row(form, "Inventory Catalog Prefix")
        self.e_colors = self._labeled_row(form, "Color Variants (comma-separated)")
        hint = QLabel("Per-item display name, description, rarity and icon handling\nare set in the Item Manager panel.")
        hint.setProperty("role", "dim")
        form.addRow(hint)
        return self._scrollable(w)

    def _build_body_panel(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        grid = QGridLayout()
        default_tokens = {t.strip().lower() for t in DEFAULT_BODY_TOKENS.split(',') if t.strip()}
        self.body_type_checks = {}
        for i, (_, token) in enumerate(BODY_SUFFIX_CATALOG):
            cb = QCheckBox(token)
            cb.setChecked(token in default_tokens)
            if token in BODY_TYPE_TOOLTIPS:
                cb.setToolTip(BODY_TYPE_TOOLTIPS[token])
            grid.addWidget(cb, i // 3, i % 3)
            self.body_type_checks[token] = cb
        layout.addLayout(grid)
        layout.addStretch(1)
        return self._scrollable(w)

    def _build_cli_panel(self):
        w = QWidget()
        form = QFormLayout(w)
        self.e_cli = self._labeled_row(form, "WolvenKit CLI Executable File", browse=True, browse_type='file')
        self.auto_compile_check = QCheckBox("Build game binary files on compilation")
        form.addRow(self.auto_compile_check)
        hint = QLabel("Without this, everything is written as .json only —\nwon't open in WolvenKit or work in-game until converted.")
        hint.setProperty("role", "dim")
        form.addRow(hint)
        return self._scrollable(w)

    def _build_actions_panel(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        prog_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 6)
        prog_row.addWidget(self.progress_bar, 1)
        self.btn_cancel = QPushButton("✖ Cancel")
        self.btn_cancel.setProperty("cls", "cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        prog_row.addWidget(self.btn_cancel)
        layout.addLayout(prog_row)

        self.status_label = QLabel("● READY")
        self.status_label.setStyleSheet("color:#00ff88; font-weight:bold;")
        layout.addWidget(self.status_label)

        self.btn_scan = QPushButton("🔍  1. Scan Blender Export Folders")
        self.btn_scan.clicked.connect(self._on_scan)
        layout.addWidget(self.btn_scan)

        self.btn_preview = QPushButton("👁  Dry-Run Preview (no files written)")
        self.btn_preview.clicked.connect(self._on_preview)
        layout.addWidget(self.btn_preview)

        self.btn_run = QPushButton("▶  2. Process && Compile Project")
        self.btn_run.setProperty("cls", "run")
        self.btn_run.clicked.connect(self._on_run)
        layout.addWidget(self.btn_run)

        self.btn_compile = QPushButton("🔨  Manual WolvenKit CLI Sync")
        self.btn_compile.setProperty("cls", "compile")
        self.btn_compile.clicked.connect(self._on_compile)
        layout.addWidget(self.btn_compile)

        layout.addStretch(1)
        return w

    def _build_item_manager_panel(self):
        self.item_scroll_area = QScrollArea()
        self.item_scroll_area.setWidgetResizable(True)
        self.item_container = QWidget()
        self.item_layout = QVBoxLayout(self.item_container)
        self.item_layout.addStretch(1)
        self.item_scroll_area.setWidget(self.item_container)
        self._show_item_placeholder()
        return self.item_scroll_area

    def _show_item_placeholder(self):
        self._clear_item_layout()
        placeholder = QLabel("Run '1. Scan Blender Export Folders' to populate this panel.")
        placeholder.setProperty("role", "dim")
        placeholder.setAlignment(Qt.AlignCenter)
        self.item_layout.insertWidget(0, placeholder)

    def _clear_item_layout(self):
        while self.item_layout.count() > 1:  # keep trailing stretch
            item = self.item_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.item_cards = []

    def _build_console_panel(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        bar.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.console.clear())
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        layout.addWidget(self.console, 1)
        return w

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        return sa

    # ── Layout persistence (this is the "remember my docking" feature) ─
    def _restore_layout(self):
        geo = self.settings.value("geometry")
        state = self.settings.value("windowState")
        if geo is not None:
            self.restoreGeometry(geo)
        if state is not None:
            self.restoreState(state)

    def _reset_layout(self):
        self.settings.remove("geometry")
        self.settings.remove("windowState")
        QMessageBox.information(self, "Layout Reset", "Restart CPMP to apply the default panel layout.")

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self._save_fields()
        super().closeEvent(event)

    # ── Logging ──────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = 'info'):
        color = TAG_COLORS.get(tag, TAG_COLORS['info'])
        prefix_map = {'info': '  ', 'ok': '✓ ', 'warn': '⚠ ', 'error': '✗ ', 'header': ''}
        if tag == 'header':
            line = str(msg)
        else:
            from datetime import datetime
            line = f" {datetime.now().strftime('%H:%M:%S')}  {prefix_map.get(tag, '  ')}{msg}"
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        html = f'<span style="color:{color};">{line}</span>'.replace('\n', '<br>')
        cursor.insertHtml(html + '<br>')
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    # ── Field persistence ────────────────────────────────────────────
    def _restore_fields(self):
        field_map = [
            (self.e_blender, 'blender_dir'), (self.e_output, 'output_dir'),
            (self.e_mod_base, 'mod_base_name'), (self.e_author, 'mod_author'),
            (self.e_display, 'display_prefix'), (self.e_colors, 'colors'),
            (self.e_cli, 'wkit_cli'),
        ]
        for edit, key in field_map:
            val = self.cfg.get(key)
            if val:
                edit.setText(val)
        saved_body_types = self.cfg.get('body_types')
        if saved_body_types:
            checked = {t.strip().lower() for t in saved_body_types.split(',') if t.strip()}
            for token, cb in self.body_type_checks.items():
                cb.setChecked(token in checked)
        self.auto_compile_check.setChecked(bool(self.cfg.get('auto_compile')))

    def _save_fields(self):
        field_map = [
            (self.e_blender, 'blender_dir'), (self.e_output, 'output_dir'),
            (self.e_mod_base, 'mod_base_name'), (self.e_author, 'mod_author'),
            (self.e_display, 'display_prefix'), (self.e_colors, 'colors'),
            (self.e_cli, 'wkit_cli'),
        ]
        for edit, key in field_map:
            self.cfg.set(key, edit.text().strip())
        checked_tokens = [t for t, cb in self.body_type_checks.items() if cb.isChecked()]
        self.cfg.set('body_types', ', '.join(checked_tokens))
        self.cfg.set('auto_compile', self.auto_compile_check.isChecked())
        self.cfg.save()

    def _get_fields(self) -> dict:
        checked_tokens = [t for t, cb in self.body_type_checks.items() if cb.isChecked()]
        return {
            'blender_dir': self.e_blender.text().strip(),
            'output_dir': self.e_output.text().strip(),
            'mod_base': self.e_mod_base.text().strip(),
            'author': self.e_author.text().strip(),
            'display_prefix': self.e_display.text().strip(),
            'colors': self.e_colors.text().strip(),
            'body_types': ', '.join(checked_tokens) or DEFAULT_BODY_TOKENS,
            'cli_path': self.e_cli.text().strip(),
            'auto_compile': self.auto_compile_check.isChecked(),
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
        for e in errors:
            self._log(e, 'error')
        return not errors

    # ── Running state / thread orchestration ────────────────────────
    def _set_running(self, running: bool, error: bool = False):
        self._running = running
        for btn in (self.btn_scan, self.btn_preview, self.btn_run, self.btn_compile):
            btn.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        if running:
            self.progress_bar.setValue(0)
            self.status_label.setText("● ACTIVE COMPILATION IN PROCESS…")
            self.status_label.setStyleSheet("color:#ff9f1a; font-weight:bold;")
        elif error:
            self.status_label.setText("● ERROR — SEE CONSOLE")
            self.status_label.setStyleSheet("color:#ff3860; font-weight:bold;")
        else:
            self.progress_bar.setValue(self.progress_bar.maximum())
            self.status_label.setText("● READY")
            self.status_label.setStyleSheet("color:#00ff88; font-weight:bold;")

    def _on_cancel(self):
        if self._active_runner is not None:
            self._active_runner.worker._cancel_event.set()
        self.status_label.setText("● CANCELLING…")
        self.status_label.setStyleSheet("color:#ff9f1a; font-weight:bold;")

    def _run_job(self, job_fn, on_result=None):
        """job_fn(worker: PipelineWorker) -> Any, executed on a QThread.

        job_fn must ONLY talk to the outside world through `worker.log(...)`
        (thread-safe, signal-based) - never touch self.console, self.item_*,
        or any other widget directly, since job_fn runs on a background
        thread and Qt widgets may only be touched from the GUI thread.
        If job_fn needs to hand data back for GUI updates, return it and
        handle it in `on_result`, which IS run back on the main thread.

        IMPORTANT: every signal below is connected to an actual bound method
        of `self` (this QMainWindow), never a lambda/local closure. Qt/PySide
        can only detect a slot's thread affinity - and therefore only
        actually queues delivery onto the GUI thread - when the slot is a
        bound method of a QObject. Connecting to a bare Python
        function/lambda has no such affinity, so Qt just invokes it inline
        on whatever thread emitted the signal (here: the worker thread),
        which silently mutates widgets off the GUI thread. That mismatch was
        the actual cause of the scan crash / paint corruption.
        """
        self._set_running(True)
        self._pending_on_result = on_result
        thread = QThread(self)
        runner = PipelineRunner(job_fn)
        runner.moveToThread(thread)

        runner.log_line.connect(self._log)
        runner.progress.connect(self._update_progress)
        runner.result_ready.connect(self._dispatch_job_result)
        runner.finished.connect(self._on_job_finished)
        thread.started.connect(runner.run)
        thread.finished.connect(thread.deleteLater)

        self._active_runner = runner
        self._active_thread = thread
        thread.start()

    def _update_progress(self, step, total):
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(step)

    def _dispatch_job_result(self, result):
        # Guaranteed to run on the GUI thread (see _run_job docstring), so
        # it's now safe for the stored callback to touch widgets directly.
        cb = self._pending_on_result
        self._pending_on_result = None
        if cb is not None:
            cb(result)

    def _on_job_finished(self, crashed):
        self._set_running(False, error=crashed)
        if self._active_thread is not None:
            self._active_thread.quit()

    # ── Item manager population ──────────────────────────────────────
    def _populate_item_manager(self):
        self._clear_item_layout()
        if not self.scanned_items:
            self._show_item_placeholder()
            return
        note = QLabel("Verify your scanned clothing folders and override assignments below:")
        note.setStyleSheet("color:#00d4ff; font-weight:bold;")
        self.item_layout.insertWidget(self.item_layout.count() - 1, note)
        for item in self.scanned_items:
            card = ItemCard(item, self._on_compile_single)
            self.item_cards.append(card)
            self.item_layout.insertWidget(self.item_layout.count() - 1, card)

    # ── Actions ──────────────────────────────────────────────────────
    def _on_scan(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not fields['blender_dir'] or not Path(fields['blender_dir']).is_dir():
            self._log("Enter a valid Blender Exports Directory first", 'error')
            return

        def job(worker: PipelineWorker):
            worker.log("━━━  PREVIEW SCAN EXPORTS INITIATED  ━━━", 'header')
            items = worker.scan_export_dir(fields['blender_dir'])
            if items:
                worker.log(f"Successfully mapped {len(items)} subfolders. Populating Item Manager...", 'ok')
            else:
                worker.log("No valid clothing assets found inside target exports folder.", 'warn')
            return items

        def on_result(items):
            # Runs back on the GUI thread - safe to touch widgets here.
            self.scanned_items = items or []
            self._populate_item_manager()

        self._run_job(job, on_result=on_result)

    def _on_preview(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields):
            return

        def job(worker: PipelineWorker):
            worker.log("━━━  DRY-RUN PREVIEW (no files will be modified)  ━━━", 'header')
            items = worker.scan_export_dir(fields['blender_dir'])
            colors = worker.parse_colors(fields['colors'])
            body_suffixes, unknown = resolve_body_suffixes(fields['body_types'])
            if unknown:
                worker.log(f"Unrecognized body type token(s), ignored: {', '.join(unknown)}", 'warn')
            worker.log(f"Would generate {len(items)} item(s) x {len(colors)} color(s) x {len(body_suffixes)} body type(s).", 'info')
            for it in items:
                worker.log(f"  ▸ {it['name']}  →  slot={it['slot']}  eq_slot={it['eq_slot']}", 'info')

        self._run_job(job)

    def _gather_active_items(self, full=True):
        active = []
        for card in self.item_cards:
            it = card.to_active_item()
            if not full:
                it = {'name': it['name'], 'enabled': it['enabled']}
            active.append(it)
        return active

    def _on_run(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=fields['auto_compile']):
            return

        if self.item_cards:
            active_items = self._gather_active_items(full=True)
        else:
            self._log("Warning: Triggering build directly without running scan preview. Attempting auto-scan fallback...", 'warn')
            active_items = None  # resolved inside job with its own worker

        missing, png_warnings = [], []
        if active_items is not None:
            for it in active_items:
                if not it.get('enabled', True):
                    continue
                if not it.get('display_name'):
                    missing.append(f"'{it['name']}' is missing a Display Name")
                if not it.get('description'):
                    missing.append(f"'{it['name']}' is missing a Description")
                if not it.get('glbs'):
                    missing.append(f"'{it['name']}' has no .glb mesh files in its export folder")
                item_path = it.get('path')
                if item_path is not None:
                    try:
                        has_png = any(_find_images(Path(item_path)))
                    except Exception:
                        has_png = True
                    if not has_png:
                        png_warnings.append(it['name'])
            if missing:
                self._log("Cannot start build — fill in the following in the Item Manager first:", 'error')
                for m in missing:
                    self._log(f"  ✗  {m}", 'error')
                return
            if png_warnings:
                self._log(f"Warning: no texture images found for: {', '.join(png_warnings)}", 'warn')

        def job(worker: PipelineWorker):
            items = active_items
            if items is None:
                items = worker.scan_export_dir(fields['blender_dir'])
                if not items:
                    worker.log("Error: No active items configured for compilation.", 'error')
                    return
            worker.orchestrate_pipeline(
                blender_dir=fields['blender_dir'],
                output_dir=fields['output_dir'],
                mod_base=fields['mod_base'],
                author=fields['author'] or 'V_Designer',
                display_prefix=fields['display_prefix'] or fields['mod_base'].title(),
                colors_raw=fields['colors'],
                cli_path=fields['cli_path'],
                auto_compile=fields['auto_compile'],
                active_items=items,
                body_suffixes_raw=fields['body_types'],
            )

        self._run_job(job)

    def _on_compile(self):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=True):
            return

        if self.item_cards:
            active_items = self._gather_active_items(full=False)
        else:
            active_items = None

        def job(worker: PipelineWorker):
            items = active_items
            if items is None:
                scanned = worker.scan_export_dir(fields['blender_dir'])
                items = [{'name': s['name'], 'enabled': True} for s in scanned]
            wkit = Path(fields['output_dir']) / fields['mod_base']
            if not wkit.is_dir():
                worker.log("Error: Project directory does not exist! Run step '2. Process && Compile' first.", 'error')
                return
            colors = worker.parse_colors(fields['colors'])
            worker.run_wolvenkit_cli(fields['cli_path'], wkit, fields['mod_base'].lower(), items, colors)

        self._run_job(job)

    def _on_compile_single(self, item_name):
        if self._running:
            return
        self._save_fields()
        fields = self._get_fields()
        if not self._validate(fields, need_cli=fields['auto_compile']):
            return

        single_item = None
        for card in self.item_cards:
            if card.item['name'] == item_name:
                single_item = card.to_active_item()
                single_item['enabled'] = True
                break
        if not single_item:
            self._log(f"Item '{item_name}' not found in Item Manager. Run scan first.", 'error')
            return

        self._log(f"━━━  SINGLE ITEM COMPILE: {item_name}  ━━━", 'header')

        def job(worker: PipelineWorker):
            worker.orchestrate_pipeline(
                blender_dir=fields['blender_dir'],
                output_dir=fields['output_dir'],
                mod_base=fields['mod_base'],
                author=fields['author'] or 'V_Designer',
                display_prefix=fields['display_prefix'] or fields['mod_base'].title(),
                colors_raw=fields['colors'],
                cli_path=fields['cli_path'],
                auto_compile=fields['auto_compile'],
                active_items=[single_item],
                body_suffixes_raw=fields['body_types'],
            )

        self._run_job(job)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    win = CPMPMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()