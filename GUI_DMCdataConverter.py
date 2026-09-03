"""
GUI_DMCdataConverter.py - DMC Werkzeuge GUI
Tkinter-Oberflaeche mit zwei Tabs:
  - "DMC - TIFFconverter"       : technische 200m-DOP-Kacheln clippen (gueltige
                                   Flaeche) und ins 1km x 1km-Grid umkacheln
                                   (parallelisiert)
  - "DMC - LASconverter [LHN95]": technische 200m-LAZ-Kacheln per AOI croppen,
                                   optional thinnen, ins 1km x 1km-Grid umkacheln
                                   (.las/.laz) und optional zu einem Gesamt-DSM-
                                   Raster (.tif/.tfw) rastern - Hoehe bleibt LHN95,
                                   Reframe zu LN02 erfolgt separat via GeoSuite
Styling analog zu topo-COGTIFFconverter / GUI_cogtiffConverter.py.

Das GUI laeuft mit Standard-Python (kein osgeo erforderlich).
GDAL-Operationen werden via _osgeo_runner.py als Subprocess (OSGeo4W Python) ausgefuehrt.
Punktwolken-Operationen (Tab 2) laufen via PDAL-CLI-Subprocess (pdal.exe, automatisch
erkannt), orchestriert vom selben OSGeo4W-Python-Prozess.
"""

import ctypes
import datetime
import time
import glob as _glob
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
from pathlib import Path
from typing import List, Dict

# ─── Pfade ────────────────────────────────────────────────────────────────────
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
PROCESS_SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "process_scripts")
RUNNER_SCRIPT        = os.path.join(PROCESS_SCRIPTS_DIR, "_osgeo_runner.py")
CONFIG_FILE          = os.path.join(PROCESS_SCRIPTS_DIR, "_dmc_config.json")
DEFAULT_GRID_SHAPE   = os.path.join(SCRIPT_DIR, "swissGRID_1km2_shp", "chGRID_1km2.shp")
DEFAULT_STAGING_DIR  = r"Y:\02_DMC_tempProcessingFolder"


# ─── OSGeo4W Python Erkennung (identisch zu topo-COGTIFFconverter) ───────────
def _detect_osgeo_python() -> str:
    """Gibt den Pfad zum OSGeo4W Python zurueck (aus Config, System-Python oder bekannten Pfaden)."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                path = json.load(f).get("osgeo_python", "")
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass

    try:
        if importlib.util.find_spec("osgeo") is not None:
            return sys.executable
    except Exception:
        pass

    kandidaten: List[str] = []
    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        kandidaten.append(str(Path(osgeo_root) / "bin" / "python3.exe"))
    kandidaten += [
        r"C:\OSGeo4W\bin\python3.exe",
        r"C:\OSGeo4W64\bin\python3.exe",
    ]
    for pat in [
        r"C:\Program Files\QGIS*\bin\python3.exe",
        r"C:\Program Files (x86)\QGIS*\bin\python3.exe",
    ]:
        kandidaten.extend(sorted(_glob.glob(pat), reverse=True))

    return next((p for p in kandidaten if Path(p).is_file()), "")


def _detect_pdal_exe(osgeo_python: str = "") -> str:
    """Gibt den Pfad zur pdal.exe zurueck (PATH, OSGEO4W_ROOT, QGIS-Installationen).
    Kein eigenes Config-/GUI-Feld - Autodetektion analog zum OSGeo4W-Python."""
    import shutil as _shutil
    found = _shutil.which("pdal")
    if found:
        return found

    kandidaten: List[str] = []
    if osgeo_python and os.path.isfile(osgeo_python):
        kandidaten.append(str(Path(os.path.dirname(osgeo_python)) / "pdal.exe"))

    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        kandidaten.append(str(Path(osgeo_root) / "bin" / "pdal.exe"))
    kandidaten += [
        r"C:\OSGeo4W\bin\pdal.exe",
        r"C:\OSGeo4W64\bin\pdal.exe",
    ]
    for pat in [
        r"C:\Program Files\QGIS*\bin\pdal.exe",
        r"C:\Program Files (x86)\QGIS*\bin\pdal.exe",
    ]:
        kandidaten.extend(sorted(_glob.glob(pat), reverse=True))

    return next((p for p in kandidaten if Path(p).is_file()), "")


def _detect_python_home(python_exe: str) -> str:
    """Leitet PYTHONHOME vom Python-Executable ab (QGIS: apps\\PythonXXX, OSGeo4W: root)."""
    bin_dir  = os.path.dirname(python_exe)
    root_dir = os.path.dirname(bin_dir)
    apps_dir = os.path.join(root_dir, "apps")
    if os.path.isdir(apps_dir):
        for name in sorted(os.listdir(apps_dir), reverse=True):
            if name.lower().startswith("python"):
                candidate = os.path.join(apps_dir, name)
                if os.path.isdir(candidate):
                    return candidate
    return root_dir


def _format_bitdepth(info: dict) -> str:
    bits = info.get("bitdepth")
    dt   = info.get("dtype", "")
    if not bits:
        return dt or "–"
    return f"{bits}bit ({dt})" if dt else f"{bits}bit"


def _save_osgeo_config(path: str) -> None:
    try:
        cfg: Dict = {}
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["osgeo_python"] = path
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Farbpaletten (identisch zu topo-COGTIFFconverter) ────────────────────────
LIGHT = {
    "root":      "#f0f0f0",
    "panel":     "#f5f5f5",
    "input":     "#ffffff",
    "fg":        "#1a1a1a",
    "fg_dim":    "#666666",
    "accent":    "#0063b1",
    "hdr_bg":    "#1a3a5c",
    "hdr_fg":    "#ffffff",
    "btn":       "#e1e1e1",
    "btn_hover": "#c8c8c8",
    "list":      "#ffffff",
    "log_bg":    "#1e1e1e",
    "log_fg":    "#d4d4d4",
    "sep":       "#c0c0c0",
    "sel_bg":    "#0078d4",
    "sel_fg":    "#ffffff",
    "ok":        "#2e7d32",
    "err":       "#c62828",
    "hint":      "#8a6f2e",
}

DARK = {
    "root":      "#1e1e1e",
    "panel":     "#252526",
    "input":     "#3c3c3c",
    "fg":        "#cccccc",
    "fg_dim":    "#7a7a7a",
    "accent":    "#4fc3f7",
    "hdr_bg":    "#1a1a1a",
    "hdr_fg":    "#cccccc",
    "btn":       "#3c3c3c",
    "btn_hover": "#505050",
    "list":      "#2d2d30",
    "log_bg":    "#1e1e1e",
    "log_fg":    "#d4d4d4",
    "sep":       "#3c3c3c",
    "sel_bg":    "#094771",
    "sel_fg":    "#cccccc",
    "ok":        "#66bb6a",
    "err":       "#ef5350",
    "hint":      "#c9a84c",
}


# ─── Haupt-App ─────────────────────────────────────────────────────────────────
class DMCConverterApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DMC Werkzeuge")
        screen_h = self.winfo_screenheight()
        win_h    = min(880, screen_h - 80)
        self.geometry(f"860x{win_h}")
        self.minsize(700, min(760, win_h))
        self.resizable(True, True)

        self._dark    = False
        self._running = False
        self._log_q   = queue.Queue()

        self._dim_labels    = []
        self._accent_labels = []
        self._hint_labels   = []

        self._osgeo_python = _detect_osgeo_python()
        self._osgeo_lbl    = None
        self._osgeo_status = None
        self._pdal_exe     = _detect_pdal_exe(self._osgeo_python)
        self._active_start_btn = None

        self._build_ui()
        self._apply_theme(True)   # Dark Mode als Standard
        self.after(100, self._poll_log)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ── UI Aufbau ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        self._hdr = tk.Frame(self, height=52)
        self._hdr.pack(fill="x")
        self._hdr.pack_propagate(False)
        self._hdr_lbl = tk.Label(self._hdr, text="DMC Werkzeuge",
                                  font=("Segoe UI", 15, "bold"))
        self._hdr_lbl.pack(side="left", padx=16, pady=12)
        self._theme_btn = tk.Button(self._hdr, text="Dark",
                                     command=self._toggle_theme,
                                     relief="flat", borderwidth=0,
                                     font=("", 9), cursor="hand2",
                                     padx=10, pady=4)
        self._theme_btn.pack(side="right", padx=12)

        # OSGeo4W Python Zeile
        self._osgeo_frame = ttk.Frame(self)
        self._osgeo_frame.pack(fill="x", padx=12, pady=(6, 0))
        osgeo_lbl_static = ttk.Label(self._osgeo_frame, text="OSGeo4W Python:",
                                      font=("Segoe UI", 9))
        osgeo_lbl_static.pack(side="left")
        self._dim_labels.append(osgeo_lbl_static)
        self._osgeo_lbl = ttk.Label(self._osgeo_frame, font=("Courier New", 8),
                                     text=self._osgeo_python or "(nicht gefunden)")
        self._osgeo_lbl.pack(side="left", padx=(6, 0))
        self._osgeo_status = ttk.Label(self._osgeo_frame, font=("Segoe UI", 8, "bold"))
        self._osgeo_status.pack(side="left", padx=(6, 0))
        ttk.Button(self._osgeo_frame, text="Aendern…",
                    command=self._set_osgeo_python).pack(side="right")

        # Tabs
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=12, pady=6)

        tab_tiff = ttk.Frame(self._notebook)
        tab_las  = ttk.Frame(self._notebook)
        self._notebook.add(tab_tiff, text="DMC - TIFFconverter")
        self._notebook.add(tab_las,  text="DMC - LASconverter [LHN95]")

        self._build_tiff_tab(tab_tiff)
        self._build_las_tab(tab_las)

        # Log
        ttk.Separator(self).pack(fill="x", padx=12, pady=4)
        log_frame = ttk.LabelFrame(self, text="Log-Ausgabe", padding=4,
                                    style="Section.TLabelframe")
        log_frame.pack(fill="x", padx=12, pady=(0, 4))
        self._log_box = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word", state="disabled",
            font=("Courier New", 9))
        self._log_box.pack(fill="both", expand=True)

        # Fortschrittsbalken (versteckt bis Verarbeitung laeuft)
        self._progress_frame = ttk.Frame(self)
        self._progress_bar   = ttk.Progressbar(self._progress_frame, mode="indeterminate")
        self._progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._progress_lbl = ttk.Label(self._progress_frame,
                                        text="Verarbeitung laeuft…", font=("", 9))
        self._progress_lbl.pack(side="left")

        # Buttons
        self._btn_row = ttk.Frame(self)
        self._btn_row.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(self._btn_row, text="Log loeschen",
                    command=self._clear_log).pack(side="right")

    def _build_scrollable(self, parent, canvas_attr: str, frame_attr: str):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        sf     = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=sf, anchor="nw")
        sf.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        setattr(self, canvas_attr, canvas)
        setattr(self, frame_attr, sf)
        return sf

    def _build_group_header(self, parent, text):
        lbl = ttk.Label(parent, text=text, font=("Segoe UI", 10, "bold"))
        lbl.pack(fill="x", pady=(10, 2), anchor="w")
        self._accent_labels.append(lbl)
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=(0, 6))

    # ── Tab: DMC - LASconverter ────────────────────────────────────────────────
    def _build_las_tab(self, parent):
        sf = self._build_scrollable(parent, "_canvas_las", "_sf_las")

        self._build_group_header(sf, "Projekt-Parameter")
        self._build_las_projekt(sf)

        self._build_group_header(sf, "Dateien")
        self._build_las_dateien(sf)

        self._build_group_header(sf, "Datei-Info  (aus erster gefundenen Kachel)")
        self._build_las_dateiinfo(sf)

        self._build_group_header(sf, "Staging & Parallelisierung")
        self._build_las_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn_las = ttk.Button(btn_row, text="▶   DMC LAS KONVERTIEREN",
                                          command=self._start_las)
        self._start_btn_las.pack(side="right", ipadx=22, ipady=7)

    def _build_las_projekt(self, parent):
        sec = ttk.LabelFrame(parent, text="Projekt", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=0)

        lbl1 = ttk.Label(sec, text="Jahr:", font=("Segoe UI", 9, "bold"))
        lbl1.grid(row=0, column=0, sticky="w", pady=3)
        self._las_jahr_var = tk.StringVar(value=str(datetime.date.today().year))
        ttk.Entry(sec, textvariable=self._las_jahr_var, width=10
                   ).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)

        lbl2 = ttk.Label(sec, text="AREA / AOI - Name:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=1, column=0, sticky="w", pady=3)
        self._las_area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_area_var, width=24
                   ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
        h2 = ttk.Label(sec, text="z.B.  GUPPENFIRN", font=("", 8))
        h2.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

        lbl_thin = ttk.Label(sec, text="Thinning:", font=("Segoe UI", 9, "bold"))
        lbl_thin.grid(row=2, column=0, sticky="w", pady=(10, 3))
        self._las_thin_var = tk.StringVar(value="Kein Thinning")
        ttk.Combobox(sec, textvariable=self._las_thin_var,
                     values=["Kein Thinning", "0.1m", "0.2m", "0.4m", "1m", "2m"],
                     state="readonly", width=14
                     ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 3))
        self._las_thin_var.trace_add("write", lambda *_: self._update_las_name_preview())
        h_thin = ttk.Label(sec, text="Mindestabstand zwischen Punkten nach Reduktion (Poisson-Disk-Sampling)", font=("", 8))
        h_thin.grid(row=3, column=0, columnspan=3, sticky="w")
        self._dim_labels.append(h_thin)

        self._las_create_raster_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Create Raster from LAZ  (ein Gesamt-TIFF+TFW fuer die AOI)",
                         variable=self._las_create_raster_var,
                         command=self._on_las_create_raster_toggle
                         ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        h_rast = ttk.Label(sec, text="Alle Kacheln mergen -> IDW-Raster -> per AOI NoData-maskiert", font=("", 8))
        h_rast.grid(row=5, column=0, columnspan=3, sticky="w", padx=(20, 0))
        self._dim_labels.append(h_rast)

        self._las_gsd_frame = ttk.Frame(sec)
        self._las_gsd_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=(4, 0))
        lbl3 = ttk.Label(self._las_gsd_frame, text="Raster-Aufloesung (GSD):", font=("Segoe UI", 9, "bold"))
        lbl3.pack(side="left")
        self._las_gsd_var = tk.StringVar(value="0.5")
        ttk.Entry(self._las_gsd_frame, textvariable=self._las_gsd_var, width=10
                   ).pack(side="left", padx=(8, 8))
        h3 = ttk.Label(self._las_gsd_frame, text="in Metern, z.B. 0.5", font=("", 8))
        h3.pack(side="left")
        self._dim_labels.append(h3)
        self._on_las_create_raster_toggle()

        name_lbl = ttk.Label(sec, text="Ausgabe-Benennung:", font=("Segoe UI", 9, "bold"))
        name_lbl.grid(row=7, column=0, sticky="nw", pady=(10, 3))
        self._las_name_preview_lbl = ttk.Label(sec, text="–", font=("Courier New", 9),
                                                justify="left")
        self._las_name_preview_lbl.grid(row=7, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(10, 3))
        self._accent_labels.append(self._las_name_preview_lbl)

        for var in (self._las_jahr_var, self._las_area_var):
            var.trace_add("write", lambda *_: self._update_las_name_preview())
        self._update_las_name_preview()

    def _on_las_create_raster_toggle(self):
        active = self._las_create_raster_var.get()
        if active:
            self._las_gsd_frame.grid()
        else:
            self._las_gsd_frame.grid_remove()
        raster_out_frame = getattr(self, "_las_out_raster_frame", None)
        if raster_out_frame is not None:
            if active:
                raster_out_frame.grid()
            else:
                raster_out_frame.grid_remove()
        self._update_las_name_preview()

    def _update_las_name_preview(self):
        if getattr(self, "_las_name_preview_lbl", None) is None:
            return
        jahr = self._las_jahr_var.get().strip() or "JAHR"
        area = self._las_area_var.get().strip() or "AREA"
        thin_token = self._las_thin_token()
        out_format = getattr(self, "_las_out_format_var", None)
        ext = out_format.get() if out_format is not None else "las"
        text = f"Punktwolke (pro 1km-Kachel):  {jahr}_{area}_TIN_{thin_token}raw_<NAME>_LV95_LHN95.{ext}"
        if getattr(self, "_las_create_raster_var", None) and self._las_create_raster_var.get():
            text += f"\nRaster (gesamte AOI):  {jahr}_{area}_TIN_{thin_token}raw_LV95_LHN95.tif  (+ .tfw)"
        self._las_name_preview_lbl.config(text=text)

    def _las_thin_token(self) -> str:
        label = getattr(self, "_las_thin_var", None)
        if label is None:
            return ""
        val = label.get().strip()
        if not val or val == "Kein Thinning":
            return ""
        try:
            m = float(val.replace("m", "").strip())
        except ValueError:
            return ""
        return f"thinnedout{round(m * 10):02d}_"

    def _build_las_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Ordner & Shapes", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        row = 0
        lbl = ttk.Label(sec, text="Input-Ordner (.laz-Kacheln):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=3)
        self._las_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_in_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_las_input
                    ).grid(row=row, column=2, pady=3)
        row += 1
        h = ttk.Label(sec, text="technical - Tiles (.laz), Koordinatensystem CH1903+/LV95 + LHN95", font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        lbl = ttk.Label(sec, text="Output-Ordner (LAS -Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._las_out_laz_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_out_laz_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Ordner…", command=self._browse_las_output_laz
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        fmt_row = ttk.Frame(sec)
        fmt_row.grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(fmt_row, text="Ausgabeformat:", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._las_out_format_var = tk.StringVar(value="las")
        ttk.Combobox(fmt_row, textvariable=self._las_out_format_var,
                     values=["las", "laz"], state="readonly", width=6
                     ).pack(side="left", padx=(8, 8))
        self._las_out_format_var.trace_add("write", lambda *_: self._update_las_name_preview())
        h = ttk.Label(fmt_row, text="1km-Grid-Kacheln  |  Default 'las' (fuer GeoSuite-Reframe LHN95->LN02)",
                       font=("", 8))
        h.pack(side="left")
        self._dim_labels.append(h)
        row += 1

        self._las_out_raster_frame = ttk.Frame(sec)
        self._las_out_raster_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 3))
        self._las_out_raster_frame.columnconfigure(1, weight=1)
        lbl = ttk.Label(self._las_out_raster_frame, text="Output-Ordner (DSM-Raster):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w")
        self._las_out_raster_var = tk.StringVar()
        ttk.Entry(self._las_out_raster_frame, textvariable=self._las_out_raster_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4))
        ttk.Button(self._las_out_raster_frame, text="Ordner…", command=self._browse_las_output_raster
                    ).grid(row=0, column=2)
        h = ttk.Label(self._las_out_raster_frame, text="Ein Gesamt-.tif/.tfw fuer die AOI", font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        self._on_las_create_raster_toggle()
        row += 1

        lbl = ttk.Label(sec, text="Clip-Shape (AOI / gueltige Flaeche):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._las_clip_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_clip_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei…", command=self._browse_las_clip_shape
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        h = ttk.Label(sec, text="LAZ: alles ausserhalb wird aus der Punktwolke entfernt (Crop)  |  "
                                 "Raster: alles ausserhalb wird NoData", font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        lbl = ttk.Label(sec, text="Grid-Shape (1km x 1km):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._las_grid_var = tk.StringVar(value=DEFAULT_GRID_SHAPE if os.path.isfile(DEFAULT_GRID_SHAPE) else "")
        ttk.Entry(sec, textvariable=self._las_grid_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei…", command=self._browse_las_grid_shape
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        h = ttk.Label(sec, text="Nur fuer die LAZ-Ausgabe (Attributfeld 'NAME')  |  gilt fuer EPSG:2056",
                       font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

    def _build_las_dateiinfo(self, parent):
        sec = ttk.LabelFrame(parent, text="Datei-Info  (aus Quelldatei gelesen)",
                              padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        fields = [
            ("Anzahl Punkte:",   "_las_info_count"),
            ("Extent (X/Y):",    "_las_info_extent"),
            ("Z-Bereich:",       "_las_info_zrange"),
            ("Koordinatensys.:", "_las_info_crs"),
            ("Komprimiert:",     "_las_info_compressed"),
            ("Dateigroesse:",    "_las_info_size"),
        ]
        for row, (label, attr) in enumerate(fields):
            lbl = ttk.Label(sec, text=label, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=row, column=0, sticky="w", pady=1)
            val = ttk.Label(sec, text="–", font=("Segoe UI", 9))
            val.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=1)
            setattr(self, attr, val)
            self._accent_labels.append(val)

        info_hint = ttk.Label(sec,
            text="Metadaten der ersten gefundenen Kachel im Input-Ordner (stellvertretend fuer alle Kacheln), via pdal info",
            font=("", 8))
        info_hint.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._dim_labels.append(info_hint)

        refresh_btn = ttk.Button(sec, text="Datei-Info aktualisieren",
                                  command=self._refresh_las_info)
        refresh_btn.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_las_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging & Parallelisierung", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._las_staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._las_staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_las_staging
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Zwischendateien (PDAL-Pipelines, Rohraster) fuer die Verarbeitung", font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="CPU-Kerne:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        cpu_max = max(1, os.cpu_count() or 8)
        self._las_workers_var = tk.StringVar(value=str(min(6, cpu_max)))
        tk.Spinbox(sec, from_=1, to=cpu_max, textvariable=self._las_workers_var, width=6
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        self._las_keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._las_keep_staging_var
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ── Tab: DMC - TIFFconverter ───────────────────────────────────────────────
    def _build_tiff_tab(self, parent):
        self.bind_class("TCombobox", "<MouseWheel>", self._fwd_wheel)
        self.bind_all("<MouseWheel>", self._fwd_wheel)

        sf = self._build_scrollable(parent, "_canvas", "_sf")

        self._build_group_header(sf, "Projekt-Parameter")
        self._build_projekt(sf)

        self._build_group_header(sf, "Dateien")
        self._build_dateien(sf)

        self._build_group_header(sf, "Datei-Info  (aus erster gefundenen Kachel)")
        self._build_dateiinfo(sf)

        self._build_group_header(sf, "Staging & Parallelisierung")
        self._build_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn = ttk.Button(btn_row, text="▶   DMC TIFF KONVERTIEREN",
                                      command=self._start)
        self._start_btn.pack(side="right", ipadx=22, ipady=7)

    def _build_projekt(self, parent):
        sec = ttk.LabelFrame(parent, text="Projekt", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=0)

        lbl1 = ttk.Label(sec, text="Jahr:", font=("Segoe UI", 9, "bold"))
        lbl1.grid(row=0, column=0, sticky="w", pady=3)
        self._jahr_var = tk.StringVar(value=str(datetime.date.today().year))
        ttk.Entry(sec, textvariable=self._jahr_var, width=10
                   ).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)

        lbl2 = ttk.Label(sec, text="AREA / AOI - Name:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=1, column=0, sticky="w", pady=3)
        self._area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._area_var, width=24
                   ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
        h2 = ttk.Label(sec, text="z.B.  GUPPENFIRN", font=("", 8))
        h2.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

        lbl3 = ttk.Label(sec, text="GSD:", font=("Segoe UI", 9, "bold"))
        lbl3.grid(row=2, column=0, sticky="w", pady=3)
        self._gsd_var = tk.StringVar(value="10cm")
        ttk.Entry(sec, textvariable=self._gsd_var, width=10
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        h3 = ttk.Label(sec, text="z.B.  10cm", font=("", 8))
        h3.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h3)

        name_lbl = ttk.Label(sec, text="Ausgabe-Benennung:", font=("Segoe UI", 9, "bold"))
        name_lbl.grid(row=3, column=0, sticky="nw", pady=(8, 3))
        self._name_preview_lbl = ttk.Label(sec, text="–", font=("Courier New", 9))
        self._name_preview_lbl.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 3))
        self._accent_labels.append(self._name_preview_lbl)

        for var in (self._jahr_var, self._area_var, self._gsd_var):
            var.trace_add("write", lambda *_: self._update_name_preview())
        self._update_name_preview()

    def _update_name_preview(self):
        jahr = self._jahr_var.get().strip() or "JAHR"
        area = self._area_var.get().strip() or "AREA"
        gsd  = self._gsd_var.get().strip() or "GSD"
        self._name_preview_lbl.config(
            text=f"{jahr}_{area}_DOP_{gsd}_<NAME>_LV95.tif  (+ .tfw)")

    def _build_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Ordner & Shapes", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        row = 0
        lbl = ttk.Label(sec, text="Input-Ordner (technical Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=3)
        self._in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._in_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_input
                    ).grid(row=row, column=2, pady=3)
        row += 1
        h = ttk.Label(sec, text="technical Tiles (.tif/.tfw), optional mit True_Ortho.vrt", font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        lbl = ttk.Label(sec, text="Output-Ordner (1km-Kacheln):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._out_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._out_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Ordner…", command=self._browse_output
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1

        lbl = ttk.Label(sec, text="Clip-Shape (gueltige Flaeche):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._clip_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._clip_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei…", command=self._browse_clip_shape
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        h = ttk.Label(sec, text="Alles ausserhalb wird zu NoData (Randverzerrungen entfernen)", font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        lbl = ttk.Label(sec, text="Grid-Shape (1km x 1km):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._grid_var = tk.StringVar(value=DEFAULT_GRID_SHAPE if os.path.isfile(DEFAULT_GRID_SHAPE) else "")
        ttk.Entry(sec, textvariable=self._grid_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei…", command=self._browse_grid_shape
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        h = ttk.Label(sec, text="Attributfeld 'NAME' liefert die Kachel-Bezeichnung  |  Shape wird nach EPSG:2056 referenziert",
                       font=("", 8))
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

    def _build_dateiinfo(self, parent):
        sec = ttk.LabelFrame(parent, text="Datei-Info  (aus Quelldatei gelesen)",
                              padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        fields = [
            ("BANDS:",          "_info_bands"),
            ("ColorInterp:",     "_info_colorinterp"),
            ("Aufloesung:",       "_info_res"),
            ("Bit-Tiefe:",       "_info_bitdepth"),
            ("Kompression:",      "_info_compression"),
            ("Koordinatensys.:", "_info_crs"),
            ("Dateigroesse:",     "_info_size"),
        ]
        for row, (label, attr) in enumerate(fields):
            lbl = ttk.Label(sec, text=label, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=row, column=0, sticky="w", pady=1)
            val = ttk.Label(sec, text="–", font=("Segoe UI", 9))
            val.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=1)
            setattr(self, attr, val)
            self._accent_labels.append(val)

        info_hint = ttk.Label(sec,
            text="Metadaten der ersten gefundenen Kachel im Input-Ordner (stellvertretend fuer alle Kacheln)",
            font=("", 8))
        info_hint.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._dim_labels.append(info_hint)

        refresh_btn = ttk.Button(sec, text="Datei-Info aktualisieren",
                                  command=self._refresh_info)
        refresh_btn.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging & Parallelisierung", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_staging
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Zwischenraster (VRT, geclipptes Mosaik) fuer parallele Verarbeitung", font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="CPU-Kerne:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        cpu_max = max(1, os.cpu_count() or 8)
        self._workers_var = tk.StringVar(value=str(min(6, cpu_max)))
        tk.Spinbox(sec, from_=1, to=cpu_max, textvariable=self._workers_var, width=6
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        self._keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._keep_staging_var
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ── Hilfsfunktionen ────────────────────────────────────────────────────────
    def _fwd_wheel(self, event):
        canvas = self._canvas_for_widget(event.widget)
        if canvas is not None:
            canvas.yview_scroll(-1*(event.delta//120), "units")
        return "break"

    def _canvas_for_widget(self, widget):
        w = widget
        while w is not None:
            if w in (getattr(self, "_canvas", None), getattr(self, "_sf", None)):
                return getattr(self, "_canvas", None)
            if w in (getattr(self, "_canvas_las", None), getattr(self, "_sf_las", None)):
                return getattr(self, "_canvas_las", None)
            w = w.master
        return None

    # ── Datei-/Ordner-Dialoge ──────────────────────────────────────────────────
    def _browse_input(self):
        path = filedialog.askdirectory(title="Input-Ordner (technical Tiles) auswaehlen")
        if path:
            self._in_var.set(path.replace("/", "\\"))
            self._clear_log()
            self._refresh_info()

    def _browse_output(self):
        path = filedialog.askdirectory(title="Output-Ordner auswaehlen")
        if path:
            self._out_var.set(path.replace("/", "\\"))

    def _browse_clip_shape(self):
        current   = self._clip_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) else self._in_var.get().strip()
        kwargs = {"title": "Clip-Shape (gueltige Flaeche) auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if start_dir and os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._clip_var.set(path.replace("/", "\\"))

    def _browse_grid_shape(self):
        current   = self._grid_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) \
                    else os.path.dirname(DEFAULT_GRID_SHAPE)
        kwargs = {"title": "Grid-Shape (1km x 1km) auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._grid_var.set(path.replace("/", "\\"))

    def _browse_staging(self):
        current = self._staging_var.get().strip()
        kwargs = {"title": "Staging-Ordner auswaehlen"}
        if current and os.path.isdir(current):
            kwargs["initialdir"] = current
        path = filedialog.askdirectory(**kwargs)
        if path:
            self._staging_var.set(path.replace("/", "\\"))

    # ── Datei-/Ordner-Dialoge (LAS-Tab) ─────────────────────────────────────────
    def _browse_las_input(self):
        path = filedialog.askdirectory(title="Input-Ordner (.laz-Kacheln) auswaehlen")
        if path:
            self._las_in_var.set(path.replace("/", "\\"))
            self._refresh_las_info()

    def _browse_las_output_laz(self):
        path = filedialog.askdirectory(title="Output-Ordner (LAZ-Kacheln) auswaehlen")
        if path:
            self._las_out_laz_var.set(path.replace("/", "\\"))

    def _browse_las_output_raster(self):
        path = filedialog.askdirectory(title="Output-Ordner (DSM-Raster) auswaehlen")
        if path:
            self._las_out_raster_var.set(path.replace("/", "\\"))

    def _browse_las_clip_shape(self):
        current   = self._las_clip_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) else self._las_in_var.get().strip()
        kwargs = {"title": "Clip-Shape (AOI) auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if start_dir and os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._las_clip_var.set(path.replace("/", "\\"))

    def _browse_las_grid_shape(self):
        current   = self._las_grid_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) \
                    else os.path.dirname(DEFAULT_GRID_SHAPE)
        kwargs = {"title": "Grid-Shape (1km x 1km) auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._las_grid_var.set(path.replace("/", "\\"))

    def _browse_las_staging(self):
        current = self._las_staging_var.get().strip()
        kwargs = {"title": "Staging-Ordner auswaehlen"}
        if current and os.path.isdir(current):
            kwargs["initialdir"] = current
        path = filedialog.askdirectory(**kwargs)
        if path:
            self._las_staging_var.set(path.replace("/", "\\"))

    # ── OSGeo4W Python Verwaltung ──────────────────────────────────────────────
    def _update_osgeo_label(self):
        T = DARK if self._dark else LIGHT
        if self._osgeo_python and os.path.isfile(self._osgeo_python):
            self._osgeo_lbl.config(text=self._osgeo_python)
            self._osgeo_status.config(text="✓", foreground=T["ok"])
        else:
            self._osgeo_lbl.config(text=self._osgeo_python or "(nicht gefunden)")
            self._osgeo_status.config(text="✗ nicht gefunden", foreground=T["err"])

    def _set_osgeo_python(self):
        init_dir = os.path.dirname(self._osgeo_python) if self._osgeo_python else r"C:\OSGeo4W\bin"
        if not os.path.isdir(init_dir):
            init_dir = "C:\\"
        path = filedialog.askopenfilename(
            title="OSGeo4W Python auswaehlen",
            initialdir=init_dir,
            filetypes=[("Python", "python*.exe"), ("Executable", "*.exe"), ("Alle", "*.*")],
        )
        if path:
            path = path.replace("/", "\\")
            self._osgeo_python = path
            _save_osgeo_config(path)
            self._update_osgeo_label()

    # ── Datei-Info via Runner ──────────────────────────────────────────────────
    def _refresh_info(self):
        src_dir = self._in_var.get().strip()
        info_attrs = ("_info_bands", "_info_colorinterp", "_info_res",
                      "_info_bitdepth", "_info_compression", "_info_crs", "_info_size")

        def _reset():
            for attr in info_attrs:
                getattr(self, attr).config(text="–")

        if not src_dir or not os.path.isdir(src_dir):
            _reset()
            return

        tiles = sorted(
            {p for pat in ("*.tif", "*.tiff") for p in _glob.glob(os.path.join(src_dir, pat))}
        )
        if not tiles:
            _reset()
            self._info_bands.config(text="(keine Kacheln gefunden)")
            return
        sample = tiles[0]

        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            self._info_bands.config(text="OSGeo4W Python nicht gefunden – bitte Pfad setzen")
            return

        def ui_error(msg):
            try:
                from tkinter import messagebox
                messagebox.showerror("Datei-Info Fehler", msg, parent=self)
            except Exception:
                pass
            _reset()

        def ui_info(info):
            try:
                ci = info.get("colorinterp", [])
                ci_parts = ["B{}:{}".format(i+1, c) for i, c in enumerate(ci)]
                self._info_bands.config(text=str(info.get("bands")))
                self._info_colorinterp.config(text="  ".join(ci_parts))
                self._info_res.config(text="{} × {} px".format(info.get('width'), info.get('height')))
                self._info_bitdepth.config(text=_format_bitdepth(info))
                comp   = info.get("compression", "–")
                layout = info.get("layout", "")
                self._info_compression.config(
                    text="{}  |  {}".format(comp, layout) if layout else comp)
                self._info_crs.config(text=info.get("crs", "–"))
                try:
                    self._info_size.config(text="{:.1f} MB".format(info.get('size_mb', 0.0)))
                except Exception:
                    pass
            except Exception:
                ui_error("Fehler beim Darstellen der Datei-Info")

        self._fetch_file_info_async(sample, ui_info, ui_error)

    def _fetch_file_info_async(self, path: str, on_info, on_error) -> None:
        def worker():
            tmp_name = None
            try:
                cfg = {"action": "info", "input_path": path}
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                    json.dump(cfg, tmp, ensure_ascii=False)
                    tmp_name = tmp.name
                env = os.environ.copy()
                env["PYTHONHOME"] = _detect_python_home(self._osgeo_python)
                env["PYTHONNOUSERSITE"] = "1"
                result = subprocess.run([self._osgeo_python, RUNNER_SCRIPT, tmp_name],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         universal_newlines=True, env=env)
                try:
                    if tmp_name and os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                except Exception:
                    pass
                if result.returncode != 0:
                    err = (result.stdout or "") + "\n" + (result.stderr or "")
                    self.after(0, on_error, err.strip())
                    return
                info = json.loads(result.stdout.strip() or "{}")
                self.after(0, on_info, info)
            except Exception as e:
                try:
                    if tmp_name and os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                except Exception:
                    pass
                self.after(0, on_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    # ── Datei-Info via pdal (LAS-Tab) ────────────────────────────────────────────
    def _refresh_las_info(self):
        src_dir = self._las_in_var.get().strip()
        info_attrs = ("_las_info_count", "_las_info_extent", "_las_info_zrange",
                      "_las_info_crs", "_las_info_compressed", "_las_info_size")

        def _reset():
            for attr in info_attrs:
                getattr(self, attr).config(text="–")

        if not src_dir or not os.path.isdir(src_dir):
            _reset()
            return

        tiles = sorted(
            {p for pat in ("*.laz", "*.las") for p in _glob.glob(os.path.join(src_dir, pat))}
        )
        if not tiles:
            _reset()
            self._las_info_count.config(text="(keine .laz/.las Kacheln gefunden)")
            return
        sample = tiles[0]

        if not self._pdal_exe or not os.path.isfile(self._pdal_exe):
            _reset()
            self._las_info_count.config(text="pdal.exe nicht gefunden – bitte zum PATH hinzufuegen")
            return

        def ui_error(msg):
            try:
                from tkinter import messagebox
                messagebox.showerror("Datei-Info Fehler", msg, parent=self)
            except Exception:
                pass
            _reset()

        def ui_info(meta):
            try:
                count = meta.get("count")
                self._las_info_count.config(text=f"{count:,}".replace(",", "'") if count is not None else "–")
                self._las_info_extent.config(
                    text="{:.1f} – {:.1f}  /  {:.1f} – {:.1f}".format(
                        meta.get("minx", 0), meta.get("maxx", 0),
                        meta.get("miny", 0), meta.get("maxy", 0)))
                self._las_info_zrange.config(
                    text="{:.2f} – {:.2f} m".format(meta.get("minz", 0), meta.get("maxz", 0)))
                srs = meta.get("srs", {}) or {}
                crs_name = srs.get("compoundwkt", "") or srs.get("wkt", "") or "–"
                if crs_name and crs_name != "–":
                    import re
                    m1 = re.search(r'COMPD_CS\["([^"]+)"', crs_name)
                    crs_name = m1.group(1) if m1 else crs_name[:60]
                self._las_info_crs.config(text=crs_name)
                self._las_info_compressed.config(text="Ja (LAZ)" if meta.get("compressed") else "Nein (LAS)")
                try:
                    size_mb = Path(sample).stat().st_size / (1024 ** 2)
                    self._las_info_size.config(text=f"{size_mb:.1f} MB")
                except Exception:
                    pass
            except Exception:
                ui_error("Fehler beim Darstellen der Datei-Info")

        def worker():
            try:
                result = subprocess.run([self._pdal_exe, "info", "--metadata", sample],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         universal_newlines=True)
                if result.returncode != 0:
                    self.after(0, ui_error, (result.stderr or result.stdout or "unbekannter Fehler").strip())
                    return
                data = json.loads(result.stdout)
                self.after(0, ui_info, data.get("metadata", {}))
            except Exception as e:
                self.after(0, ui_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    # ── Theme ──────────────────────────────────────────────────────────────────
    def _toggle_theme(self):
        self._apply_theme(not self._dark)

    def _apply_theme(self, dark: bool):
        self._dark = dark
        T = DARK if dark else LIGHT
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",
            background=T["panel"], foreground=T["fg"],
            fieldbackground=T["input"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
            bordercolor=T["sep"], lightcolor=T["panel"], darkcolor=T["sep"],
            insertcolor=T["fg"], troughcolor=T["root"],
        )
        s.configure("TFrame",      background=T["panel"])
        s.configure("TLabelframe", background=T["panel"], bordercolor=T["sep"])
        s.configure("TLabelframe.Label",
                    background=T["panel"], foreground=T["fg"],
                    font=("Segoe UI", 9, "bold"))
        s.configure("Section.TLabelframe",
                    background=T["panel"], bordercolor=T["sep"])
        s.configure("Section.TLabelframe.Label",
                    background=T["panel"], foreground=T["accent"],
                    font=("Segoe UI", 10, "bold"))
        s.configure("TLabel",  background=T["panel"], foreground=T["fg"])
        s.configure("TButton",
            background=T["btn"], foreground=T["fg"],
            bordercolor=T["sep"], relief="flat",
            padding=(8, 4), focuscolor=T["panel"])
        s.map("TButton",
            background=[("active", T["btn_hover"]), ("pressed", T["sep"])],
            foreground=[("active", T["fg"])],
            relief=[("pressed", "flat")])
        s.configure("TCombobox",
            fieldbackground=T["input"], background=T["btn"],
            foreground=T["fg"], arrowcolor=T["fg"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"],
            bordercolor=T["sep"], insertcolor=T["fg"])
        s.map("TCombobox",
            fieldbackground=[("readonly", T["input"]), ("disabled", T["panel"])],
            selectbackground=[("readonly", T["input"])],
            selectforeground=[("readonly", T["fg"])],
            foreground=[("readonly", T["fg"]), ("disabled", T["fg_dim"])],
            background=[("active", T["btn_hover"])])
        s.configure("TEntry",
            fieldbackground=T["input"], foreground=T["fg"],
            bordercolor=T["sep"], insertcolor=T["fg"],
            selectbackground=T["sel_bg"], selectforeground=T["sel_fg"])
        s.configure("TCheckbutton", background=T["panel"], foreground=T["fg"])
        s.map("TCheckbutton", background=[("active", T["panel"])])
        s.configure("Vertical.TScrollbar",
            background=T["btn"], troughcolor=T["root"],
            bordercolor=T["sep"], arrowcolor=T["fg"])
        s.configure("TSeparator",  background=T["sep"])
        s.configure("TProgressbar",
            background=T["accent"], troughcolor=T["root"],
            bordercolor=T["sep"])
        s.configure("TNotebook",
            background=T["root"], bordercolor=T["sep"])
        s.configure("TNotebook.Tab",
            background=T["btn"], foreground=T["fg"],
            bordercolor=T["sep"], padding=(10, 4))
        s.map("TNotebook.Tab",
            background=[("selected", T["panel"]), ("active", T["btn_hover"])],
            foreground=[("selected", T["accent"])],
            padding=[("selected", (16, 8))])

        self.option_add("*TCombobox*Listbox.background",       T["list"])
        self.option_add("*TCombobox*Listbox.foreground",       T["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", T["sel_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", T["sel_fg"])

        self.configure(bg=T["root"])
        self._canvas.configure(bg=T["panel"], highlightbackground=T["sep"])
        if getattr(self, "_canvas_las", None) is not None:
            self._canvas_las.configure(bg=T["panel"], highlightbackground=T["sep"])

        self._hdr.configure(bg=T["hdr_bg"])
        self._hdr_lbl.configure(bg=T["hdr_bg"], fg=T["hdr_fg"])
        self._theme_btn.configure(
            bg=T["hdr_bg"], fg=T["hdr_fg"],
            activebackground=T["btn"], activeforeground=T["fg"],
            text="Hell" if dark else "Dark")

        self._log_box.configure(bg=T["log_bg"], fg=T["log_fg"],
                                 insertbackground=T["log_fg"])

        for lbl in self._dim_labels:
            try: lbl.configure(foreground=T["fg_dim"])
            except tk.TclError: pass
        for lbl in self._accent_labels:
            try: lbl.configure(foreground=T["accent"])
            except tk.TclError: pass
        for lbl in self._hint_labels:
            try: lbl.configure(foreground=T["hint"])
            except tk.TclError: pass

        if self._osgeo_lbl is not None:
            self._update_osgeo_label()

        self._set_titlebar_dark(dark)

    def _set_titlebar_dark(self, dark: bool):
        if not self.winfo_ismapped():
            self.after(50, lambda: self._set_titlebar_dark(dark))
            return
        try:
            hwnd  = int(self.wm_frame(), 16)
            value = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    # ── Log ───────────────────────────────────────────────────────────────────
    def _log(self, text: str):
        self._log_box.config(state="normal")
        self._log_box.insert("end", text)
        self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _clear_log(self):
        self._log_box.config(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.config(state="disabled")

    def _poll_log(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _on_done(self, success: bool, vorgang: str = "DMC TIFF-Konvertierung"):
        self._running = False
        if self._active_start_btn is not None:
            self._active_start_btn.config(state="normal")
        self._progress_bar.stop()
        self._progress_frame.pack_forget()
        if hasattr(self, "_progress_start_time"):
            del self._progress_start_time
        if success:
            self._log(f"\n✔  {vorgang} erfolgreich abgeschlossen.\n")
        else:
            self._log(f"\n✘  {vorgang} fehlgeschlagen.\n")
        self._show_done_popup(success, vorgang)

    def _show_done_popup(self, success: bool, vorgang: str) -> None:
        from tkinter import messagebox
        if success:
            messagebox.showinfo(f"{vorgang} abgeschlossen",
                                 f"{vorgang} erfolgreich abgeschlossen.", parent=self)
        else:
            messagebox.showerror(f"{vorgang} fehlgeschlagen",
                                  f"{vorgang} ist fehlgeschlagen.\nDetails siehe Log-Ausgabe.", parent=self)

    def _update_progress(self, fraction: float):
        try:
            if not hasattr(self, "_progress_start_time"):
                self._progress_start_time = time.time()
                try:
                    self._progress_bar.stop()
                    self._progress_bar.config(mode="determinate", maximum=100)
                except Exception:
                    pass
            pct = max(0.0, min(1.0, fraction))
            try:
                self._progress_bar['value'] = pct * 100.0
            except Exception:
                pass
            now = time.time()
            elapsed = now - getattr(self, "_progress_start_time", now)
            eta_str = "--:--"
            if pct > 0:
                remaining = elapsed * (1.0 - pct) / pct
                m = int(remaining // 60)
                s = int(remaining % 60)
                eta_str = f"{m:d}m {s:02d}s"
            try:
                self._progress_lbl.config(text=f"{pct*100:5.1f}% — verbleibend: {eta_str}")
            except Exception:
                pass
        except Exception:
            pass

    # ── Validierung ───────────────────────────────────────────────────────────
    def _validate(self):
        errors = []

        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append(
                "OSGeo4W Python nicht gefunden.\n"
                "Bitte Pfad via 'Aendern…' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe)."
            )

        jahr = self._jahr_var.get().strip()
        if not jahr or not jahr.isdigit():
            errors.append("Jahr fehlt oder ist ungueltig (numerisch erwartet, z.B. 2026).")

        area = self._area_var.get().strip()
        if not area:
            errors.append("AREA / AOI - Name fehlt.")

        gsd = self._gsd_var.get().strip()
        if not gsd:
            errors.append("GSD fehlt (z.B. 10cm).")

        in_dir = self._in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")

        out_dir = self._out_var.get().strip()
        if not out_dir:
            errors.append("Output-Ordner fehlt.")

        clip = self._clip_var.get().strip()
        if not clip:
            errors.append("Clip-Shape (gueltige Flaeche) fehlt.")
        elif not os.path.isfile(clip):
            errors.append(f"Clip-Shape nicht gefunden:\n  {clip}")

        grid = self._grid_var.get().strip()
        if not grid:
            errors.append("Grid-Shape (1km x 1km) fehlt.")
        elif not os.path.isfile(grid):
            errors.append(f"Grid-Shape nicht gefunden:\n  {grid}")

        staging = self._staging_var.get().strip()
        if not staging:
            errors.append("Staging-Ordner fehlt.")

        try:
            workers = int(self._workers_var.get())
            if workers < 1:
                raise ValueError
        except Exception:
            errors.append("CPU-Kerne ungueltig.")

        if errors:
            from tkinter import messagebox
            messagebox.showerror("Eingabe-Fehler",
                                  "\n\n".join(f"• {e}" for e in errors), parent=self)
            return False
        return True

    # ── Konvertierung starten ─────────────────────────────────────────────────
    def _start(self):
        if self._running:
            return
        if not self._validate():
            return

        cfg = {
            "action":          "process",
            "jahr":             self._jahr_var.get().strip(),
            "area":             self._area_var.get().strip(),
            "gsd":              self._gsd_var.get().strip(),
            "input_dir":        self._in_var.get().strip(),
            "output_dir":       self._out_var.get().strip(),
            "clip_shape_path":  self._clip_var.get().strip(),
            "grid_shape_path":  self._grid_var.get().strip(),
            "staging_dir":      self._staging_var.get().strip(),
            "num_workers":      int(self._workers_var.get()),
            "keep_staging":     bool(self._keep_staging_var.get()),
        }

        self._running = True
        self._active_start_btn = self._start_btn
        self._start_btn.config(state="disabled")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._log("=== DMC TIFF-Konvertierung gestartet ===\n\n")

        log_stem = f"{cfg['jahr']}_{cfg['area']}_DOP_{cfg['gsd']}"
        threading.Thread(
            target=self._run_thread, args=(cfg, log_stem, "DMC TIFF-Konvertierung"), daemon=True
        ).start()

    def _run_thread(self, cfg: dict, log_stem: str, vorgang: str = "DMC TIFF-Konvertierung"):
        try:
            self._run_osgeo_subprocess(cfg, log_stem)
            self.after(0, self._on_done, True, vorgang)
        except Exception as e:
            self._log_q.put(f"\n[FEHLER] {e}\n")
            self._log_q.put(traceback.format_exc())
            self.after(0, self._on_done, False, vorgang)

    # ── Validierung (LAS-Tab) ─────────────────────────────────────────────────
    def _validate_las(self):
        errors = []

        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append(
                "OSGeo4W Python nicht gefunden.\n"
                "Bitte Pfad via 'Aendern…' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe)."
            )

        if not self._pdal_exe or not os.path.isfile(self._pdal_exe):
            errors.append(
                "pdal.exe wurde nicht gefunden.\n"
                "Bitte pdal (Teil von OSGeo4W/QGIS) zum System-PATH hinzufuegen."
            )

        jahr = self._las_jahr_var.get().strip()
        if not jahr or not jahr.isdigit():
            errors.append("Jahr fehlt oder ist ungueltig (numerisch erwartet, z.B. 2026).")

        area = self._las_area_var.get().strip()
        if not area:
            errors.append("AREA / AOI - Name fehlt.")

        if self._las_create_raster_var.get():
            try:
                gsd = float(self._las_gsd_var.get().strip().replace("m", ""))
                if gsd <= 0:
                    raise ValueError
            except Exception:
                errors.append("Raster-Aufloesung (GSD) ungueltig (Zahl in Metern erwartet, z.B. 0.5).")

        in_dir = self._las_in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")

        out_dir_laz = self._las_out_laz_var.get().strip()
        if not out_dir_laz:
            errors.append("Output-Ordner (LAZ-Kacheln) fehlt.")

        if self._las_create_raster_var.get():
            out_dir_raster = self._las_out_raster_var.get().strip()
            if not out_dir_raster:
                errors.append("Output-Ordner (DSM-Raster) fehlt (da 'Create Raster from LAZ' aktiviert ist).")

        clip = self._las_clip_var.get().strip()
        if not clip:
            errors.append("Clip-Shape (AOI) fehlt.")
        elif not os.path.isfile(clip):
            errors.append(f"Clip-Shape nicht gefunden:\n  {clip}")

        grid = self._las_grid_var.get().strip()
        if not grid:
            errors.append("Grid-Shape (1km x 1km) fehlt.")
        elif not os.path.isfile(grid):
            errors.append(f"Grid-Shape nicht gefunden:\n  {grid}")

        staging = self._las_staging_var.get().strip()
        if not staging:
            errors.append("Staging-Ordner fehlt.")

        try:
            workers = int(self._las_workers_var.get())
            if workers < 1:
                raise ValueError
        except Exception:
            errors.append("CPU-Kerne ungueltig.")

        if errors:
            from tkinter import messagebox
            messagebox.showerror("Eingabe-Fehler",
                                  "\n\n".join(f"• {e}" for e in errors), parent=self)
            return False
        return True

    # ── Konvertierung starten (LAS-Tab) ───────────────────────────────────────
    def _start_las(self):
        if self._running:
            return
        if not self._validate_las():
            return

        thin_label = self._las_thin_var.get().strip()
        thin_m = None
        if thin_label and thin_label != "Kein Thinning":
            thin_m = float(thin_label.replace("m", "").strip())

        create_raster = bool(self._las_create_raster_var.get())
        cfg = {
            "action":          "process_las",
            "jahr":             self._las_jahr_var.get().strip(),
            "area":             self._las_area_var.get().strip(),
            "create_raster":    create_raster,
            "gsd":              float(self._las_gsd_var.get().strip().replace("m", "")) if create_raster else None,
            "input_dir":        self._las_in_var.get().strip(),
            "output_dir_laz":     self._las_out_laz_var.get().strip(),
            "output_dir_raster":  self._las_out_raster_var.get().strip() if create_raster else None,
            "out_format":       self._las_out_format_var.get(),
            "clip_shape_path":  self._las_clip_var.get().strip(),
            "grid_shape_path":  self._las_grid_var.get().strip(),
            "staging_dir":      self._las_staging_var.get().strip(),
            "num_workers":      int(self._las_workers_var.get()),
            "keep_staging":     bool(self._las_keep_staging_var.get()),
            "thin_m":           thin_m,
            "pdal_exe":         self._pdal_exe,
        }

        self._running = True
        self._active_start_btn = self._start_btn_las
        self._start_btn_las.config(state="disabled")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._log("=== DMC LAS-Konvertierung gestartet ===\n\n")

        log_stem = f"{cfg['jahr']}_{cfg['area']}_TIN"
        threading.Thread(
            target=self._run_thread, args=(cfg, log_stem, "DMC LAS-Konvertierung"), daemon=True
        ).start()

    # ── Subprocess-Ausfuehrung ─────────────────────────────────────────────────
    def _run_osgeo_subprocess(self, cfg: dict, log_stem: str) -> None:
        """Startet _osgeo_runner.py als Subprocess; Log-Ausgabe + Fortschritt live im GUI."""
        logs_dir  = Path(SCRIPT_DIR) / "logs"
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log_path  = logs_dir / f"{log_stem}_{timestamp}.log"

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            tmp_name = tmp.name
        try:
            env = os.environ.copy()
            env["PYTHONHOME"] = _detect_python_home(self._osgeo_python)
            env["PYTHONNOUSERSITE"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            header = f"[Subprocess] {self._osgeo_python}\n\n"
            self._log_q.put(header)
            proc = subprocess.Popen(
                [self._osgeo_python, RUNNER_SCRIPT, tmp_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write(header)
                for line in proc.stdout:
                    stripped = line.strip()
                    if stripped.startswith("PROGRESS:"):
                        try:
                            val = float(stripped.split(":", 1)[1])
                        except Exception:
                            val = None
                        if val is not None:
                            self.after(0, self._update_progress, float(val))
                    self._log_q.put(line)
                    lf.write(line)
            proc.wait()
            self._log_q.put(f"\nLog gespeichert: {log_path}\n")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"OSGeo4W Subprocess beendet mit Exit-Code {proc.returncode}"
                )
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DMCConverterApp()
    app.mainloop()
