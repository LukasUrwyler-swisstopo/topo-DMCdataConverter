"""
GUI_DMCdataConverter.py - DMC Werkzeuge GUI
Tkinter-Oberflaeche mit sechs Tabs:
  - "DMC - TIFFconverter"       : technische 200m-DOP-Tiles clippen (gueltige
                                   Flaeche) und in 1km x 1km-Tiles zerlegen
                                   (parallelisiert), optional ein QC-Mosaik (COG)
  - "DMC - LASconverter [LHN95]": technische 200m-LAZ-Tiles per AOI croppen,
                                   optional thinnen, in 1km x 1km-Tiles zerlegen
                                   (.las/.laz) und optional zu einem Gesamt-DSM-
                                   Raster (.tif/.tfw) rastern - Hoehe bleibt LHN95,
                                   Reframe zu LN02 erfolgt separat via GeoSuite
  - "DMC - LASconverter [LN02]"  : die via GeoSuite nach LN02 reframten 1km-Tiles
                                   in die GDWH-taugliche LAS-1.4-Form bringen (PF7
                                   mit RGB, global_encoding 17, scale 0.01, Offset =
                                   Tile-Ursprung, byte-exakte LV95/LN02-CRS-VLRs),
                                   optional ein QC-COPC der ganzen AOI (Sichtkontrolle)
                                   und optional ebenfalls DSM + Hillshade rastern
  - "Create DSM-Raster"          : DSM + Hillshade aus einem beliebigen Ordner mit
                                   LAS/LAZ-Kacheln - ohne Punktwolken-Verarbeitung
  - "Create COGTIFF"             : EIN COG aus einem beliebigen Ordner mit TIFF-
                                   Kacheln (Mosaik, Bandauswahl, Kompression)
  - "Create COPC"                : EIN COPC aus einem beliebigen Ordner mit LAS/LAZ-
                                   Kacheln (Merge via untwine)
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
import re
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

# ─── Band-Ausgabe (Tab "DMC - TIFFconverter") ────────────────────────────────
# DMC-Ausgangsdaten sind praktisch immer 4-Band (RGBN: Rot, Gruen, Blau, NIR).
# Fuer die Publikation wird daraus optional ein 3-Band-Auszug gebildet. Die
# Schluessel entsprechen 1:1 _osgeo_runner.BAND_MODES.
BAND_KEEP = "4-Band  (RGBN, unveraendert)"
BAND_RGB  = "RGBN → RGB  (3-Band, Echtfarbe)"
BAND_NRG  = "RGBN → NRG  (3-Band, Falschfarben-Infrarot)"
BAND_CHOICES   = [BAND_KEEP, BAND_RGB, BAND_NRG]
BAND_MODE_KEYS = {BAND_KEEP: "keep", BAND_RGB: "rgb", BAND_NRG: "nrg"}
BAND_PREVIEW_TEXT = {
    "keep": "alle Baender der Quelle (bei RGBN: 4-Band)",
    "rgb":  "3-Band RGB  –  Quellbaender 1,2,3",
    "nrg":  "3-Band NRG  –  Quellbaender 4,1,2  (NIR, Rot, Gruen)",
}
BAND_HINT_UNKNOWN = "RGB-/NRG-Auszug nur bei 4-Band-Input (RGBN)"
BAND_HINT_4BAND   = "4-Band erkannt  |  RGB = 1,2,3  |  NRG = 4,1,2"
BAND_HINT_NOT4    = "Input hat {count} Band/Baender - Auswahl nur bei 4-Band (RGBN)"


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


def _detect_osgeo_exe(name: str, osgeo_python: str = "") -> str:
    """Sucht ein Kommandozeilen-Werkzeug aus dem OSGeo4W-/QGIS-Umfeld: PATH, neben dem
    OSGeo4W-Python, dann OSGEO4W_ROOT, Standard-OSGeo4W und QGIS-Installationen (je
    'bin' und 'apps/qgis*/bin'). Kein eigenes Config-/GUI-Feld."""
    import shutil as _shutil
    found = _shutil.which(name)
    if found:
        return found

    exe = f"{name}.exe"
    kandidaten: List[str] = []
    if osgeo_python and os.path.isfile(osgeo_python):
        kandidaten.append(str(Path(os.path.dirname(osgeo_python)) / exe))

    roots: List[str] = []
    osgeo_root = os.environ.get("OSGEO4W_ROOT")
    if osgeo_root:
        roots.append(osgeo_root)
    roots += [r"C:\OSGeo4W", r"C:\OSGeo4W64"]
    for pat in [r"C:\Program Files\QGIS*", r"C:\Program Files (x86)\QGIS*"]:
        roots.extend(sorted(_glob.glob(pat), reverse=True))
    for root in roots:
        kandidaten.append(str(Path(root) / "bin" / exe))
        kandidaten.extend(sorted(_glob.glob(str(Path(root) / "apps" / "qgis*" / "bin" / exe)),
                                 reverse=True))

    return next((p for p in kandidaten if Path(p).is_file()), "")


def _detect_pdal_exe(osgeo_python: str = "") -> str:
    """Pfad zur pdal.exe (Punktwolken-Verarbeitung)."""
    return _detect_osgeo_exe("pdal", osgeo_python)


def _detect_untwine_exe(osgeo_python: str = "") -> str:
    """Pfad zur untwine.exe (Hobu) - baut das QC-COPC im Tab [LN02]. Liegt
    normalerweise im bin-Ordner der QGIS-Installation."""
    return _detect_osgeo_exe("untwine", osgeo_python)


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


# ─── Formatierung der Punktwolken-Datei-Info (beide LAS-Tabs) ─────────────────
# Jede Funktion bekommt die 'metadata'-Struktur aus 'pdal info --metadata' und den
# Pfad des Beispiel-Tiles und liefert den anzuzeigenden Text.
def _pc_count(meta: dict, path: str) -> str:
    c = meta.get("count")
    return f"{c:,}".replace(",", "'") if c is not None else "–"


def _pc_extent(meta: dict, path: str) -> str:
    return "{:.1f} – {:.1f}  /  {:.1f} – {:.1f}".format(
        meta.get("minx", 0), meta.get("maxx", 0),
        meta.get("miny", 0), meta.get("maxy", 0))


def _pc_zrange(meta: dict, path: str) -> str:
    return "{:.2f} – {:.2f} m".format(meta.get("minz", 0), meta.get("maxz", 0))


def _pc_crs(meta: dict, path: str) -> str:
    srs  = meta.get("srs", {}) or {}
    name = srs.get("compoundwkt", "") or srs.get("wkt", "")
    if not name:
        return "– (kein CRS-Tag in der Datei)"
    m = re.search(r'(?:COMPD_CS|COMPOUNDCRS)\["([^"]+)"', name)
    label = m.group(1) if m else name[:60]
    # Hoehenbezug explizit ausweisen - der CompoundCRS-Name allein sagt nichts darueber
    for token in ("LN02", "LHN95"):
        if token in name:
            return f"{label}  [{token}]"
    return label


def _pc_version(meta: dict, path: str) -> str:
    return (f"LAS {meta.get('major_version', 1)}.{meta.get('minor_version', '?')}"
            f"  /  PF{meta.get('dataformat_id', '?')}")


# Kachelname der Konverter-Tabs, z.B.
# "2026_GUPPENFIRN_v2_TIN_thinnedout02_raw_2713_1206_LV95_LN02.laz".
# Daraus lassen sich Jahr, AREA und Hoehenbezug fuer den Tab "Create DSM-Raster"
# vorbelegen - eintippen muss man sie dann nur bei Fremddaten.
_TILE_PROJECT_PATTERN = re.compile(
    r"^(\d{4})_(.+?)_TIN_.*_LV95_(LHN95|LN02)\.(?:las|laz)$", re.IGNORECASE)


def _project_from_tile_name(filename: str):
    """(jahr, area, hoehenbezug) aus einem Kachelnamen - oder None."""
    m = _TILE_PROJECT_PATTERN.match(os.path.basename(filename))
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).upper()


# Point Data Record Formats mit RGB-Feldern (LAS 1.4 R15, Tabellen 6-13)
_PC_FORMATS_WITH_RGB = (2, 3, 5, 7, 8, 10)


def _pc_rgb(meta: dict, path: str) -> str:
    """Ob das Punktformat ueberhaupt RGB-Felder fuehrt - headerbasiert, ohne die
    Punktdaten zu lesen. Ob dort auch Werte stehen (statt lauter Nullen), sagt erst
    eine Statistik ueber alle Punkte; die laeuft im Tab-Lauf selbst und meldet sich
    dort als Warnung pro Kachel."""
    fmt = meta.get("dataformat_id")
    if fmt is None:
        return "-"
    return (f"ja  (PF{fmt} fuehrt RGB)" if fmt in _PC_FORMATS_WITH_RGB
            else f"nein  (PF{fmt} hat kein Farbfeld)")


def _pc_globalenc(meta: dict, path: str) -> str:
    ge = meta.get("global_encoding")
    if ge is None:
        return "–"
    return str(ge) if ge == 17 else f"{ge}   (Ziel: 17)"


def _pc_compressed(meta: dict, path: str) -> str:
    return "Ja (LAZ)" if meta.get("compressed") else "Nein (LAS)"


def _pc_size(meta: dict, path: str) -> str:
    return f"{Path(path).stat().st_size / (1024 ** 2):.1f} MB"


# ─── Ausgabenamen (Tabs "Create COGTIFF" / "Create COPC") ─────────────────────
# Entspricht _osgeo_runner.COG_COMPRESSIONS (Auswahl wie im Mosaik-Tab von
# topo-COGTIFFconverter).
COG_COMPRESSIONS = ["JPEG", "DEFLATE", "LZW", "ZSTD", "NONE"]


def _normalize_cog_path(path: str) -> str:
    """Ausgabe-COG: Endung .tif ergaenzen, falls keine TIFF-Endung angegeben ist."""
    path = path.strip()
    if path and not path.lower().endswith((".tif", ".tiff")):
        path += ".tif"
    return path


def _normalize_copc_path(path: str) -> str:
    """Ausgabe-COPC: muss auf .copc.laz enden - an dieser Konvention erkennen QGIS und
    PDAL das Format. '.laz'/'.las' wird zu '.copc.laz', sonst wird ergaenzt."""
    path = path.strip()
    low = path.lower()
    if not path or low.endswith(".copc.laz"):
        return path
    if low.endswith((".laz", ".las")):
        return path[:-4] + ".copc.laz"
    return path + ".copc.laz"


# ─── Haupt-App ─────────────────────────────────────────────────────────────────
class DMCConverterApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DMC Werkzeuge")
        screen_h = self.winfo_screenheight()
        win_h    = min(880, screen_h - 80)
        self.geometry(f"900x{win_h}")
        self.minsize(700, min(760, win_h))
        self.resizable(True, True)

        self._dark    = False
        self._running = False
        self._log_q   = queue.Queue()

        self._dim_labels    = []
        self._accent_labels = []
        self._hint_labels   = []
        self._scroll_areas  = []   # (Canvas, Frame) aller Scroll-Flaechen

        self._osgeo_python = _detect_osgeo_python()
        self._osgeo_lbl    = None
        self._osgeo_status = None
        self._pdal_exe     = _detect_pdal_exe(self._osgeo_python)
        self._untwine_exe  = _detect_untwine_exe(self._osgeo_python)
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
        tab_ln02 = ttk.Frame(self._notebook)
        tab_dsm  = ttk.Frame(self._notebook)
        tab_cog  = ttk.Frame(self._notebook)
        tab_copc = ttk.Frame(self._notebook)
        self._notebook.add(tab_tiff, text="DMC - TIFFconverter")
        self._notebook.add(tab_las,  text="DMC - LASconverter [LHN95]")
        self._notebook.add(tab_ln02, text="DMC - LASconverter [LN02]")
        self._notebook.add(tab_dsm,  text="Create DSM-Raster")
        self._notebook.add(tab_cog,  text="Create COGTIFF")
        self._notebook.add(tab_copc, text="Create COPC")

        self._build_tiff_tab(tab_tiff)
        self._build_las_tab(tab_las)
        self._build_ln02_tab(tab_ln02)
        self._build_dsm_tab(tab_dsm)
        self._build_cog_tab(tab_cog)
        self._build_copc_tab(tab_copc)

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
        self._scroll_areas.append((canvas, sf))
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

        self._build_group_header(sf, "Datei-Info  (aus erstem gefundenen Tile gelesen)")
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
        h_thin = ttk.Label(sec, text="Mindestpunktabstand nach dem Ausduennen (Poisson-Disk)", font=("", 8))
        h_thin.grid(row=3, column=0, columnspan=3, sticky="w")
        self._dim_labels.append(h_thin)

        self._las_create_raster_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Create DSM-Raster from LAZ  (ein Gesamt-TIFF+TFW fuer die AOI)",
                         variable=self._las_create_raster_var,
                         command=self._on_las_create_raster_toggle
                         ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        h_rast = ttk.Label(sec, text="Tiles -> DSM (IDW) -> Loecher bis 900 m2 gefuellt -> AOI-Maske -> Hillshade",
                            font=("", 8), justify="left")
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
        self._las_gsd_var.trace_add("write", lambda *_: self._update_las_name_preview())
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
        text = f"Punktwolke (pro 1km-Tiles):  {jahr}_{area}_TIN_{thin_token}raw_<NAME>_LV95_LHN95.{ext}"
        if getattr(self, "_las_create_raster_var", None) and self._las_create_raster_var.get():
            try:
                gsd_label = f"{round(float(self._las_gsd_var.get().strip().replace('m', '')) * 100)}cm"
            except (ValueError, AttributeError):
                gsd_label = "GSD"
            text += (f"\nDSM (gesamte AOI):        {jahr}_{area}_DSM_{gsd_label}_LV95_LHN95.tif  (+ .tfw)"
                     f"\nHillshade (gesamte AOI):  {jahr}_{area}_hillshade_{gsd_label}_LV95_LHN95.tif  (+ .tfw)")
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
        lbl = ttk.Label(sec, text="Input-Ordner (.laz-Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=3)
        self._las_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_in_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_las_input
                    ).grid(row=row, column=2, pady=3)
        row += 1
        h = ttk.Label(sec, text="technical Tiles (.laz), LV95 + LHN95", font=("", 8))
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
        h = ttk.Label(fmt_row, text="LAS 1.4 / PF7 mit RGB, EPSG:2056 - Eingabe fuer GeoSuite/REFRAME\n"
                                    "'laz' spart rund 80 % Platz, Daten identisch",
                       font=("", 8), justify="left")
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

        lbl = ttk.Label(sec, text="AOI Clip-Shape (gültige Fläche):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._las_clip_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._las_clip_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei…", command=self._browse_las_clip_shape
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        h = ttk.Label(sec, text="Punktwolke: Crop  |  Raster: ausserhalb NoData", font=("", 8))
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
        h = ttk.Label(sec, text="Feld 'NAME' = Tile-Bezeichnung  |  EPSG:2056",
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
            ("Farbe (RGB):",     "_las_info_rgb"),
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
            text="Aus dem ersten Tile im Input-Ordner (pdal info)",
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
        h = ttk.Label(sec, text="Zwischendateien (PDAL-Pipelines, Rohraster)", font=("", 8))
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

    # ── Tab: DMC - LASconverter [LN02] ─────────────────────────────────────────
    def _build_ln02_tab(self, parent):
        sf = self._build_scrollable(parent, "_canvas_ln02", "_sf_ln02")

        self._build_group_header(sf, "Projekt-Parameter")
        self._build_ln02_projekt(sf)

        self._build_group_header(sf, "Dateien")
        self._build_ln02_dateien(sf)

        self._build_group_header(sf, "Datei-Info  (aus erstem gefundenen Tile gelesen)")
        self._build_ln02_dateiinfo(sf)

        self._build_group_header(sf, "Staging & Parallelisierung")
        self._build_ln02_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn_ln02 = ttk.Button(btn_row, text="▶   DMC LAS KONVERTIEREN  [LN02]",
                                           command=self._start_ln02)
        self._start_btn_ln02.pack(side="right", ipadx=22, ipady=7)

    def _build_ln02_projekt(self, parent):
        sec = ttk.LabelFrame(parent, text="Projekt", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=0)

        intro = ttk.Label(sec, text="Setzt auf den nach LN02 reframten Tiles nur die GDWH-Metadaten - kein Reframe, kein Crop",
                           font=("", 8), justify="left")
        intro.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._dim_labels.append(intro)

        lbl1 = ttk.Label(sec, text="Jahr:", font=("Segoe UI", 9, "bold"))
        lbl1.grid(row=1, column=0, sticky="w", pady=3)
        self._ln02_jahr_var = tk.StringVar(value=str(datetime.date.today().year))
        ttk.Entry(sec, textvariable=self._ln02_jahr_var, width=10
                   ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)

        lbl2 = ttk.Label(sec, text="AREA / AOI - Name:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=3)
        self._ln02_area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._ln02_area_var, width=24
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        h2 = ttk.Label(sec, text="z.B.  GUPPENFIRN", font=("", 8))
        h2.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

        self._ln02_create_copc_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Create COPC  (QC: alle Tiles als EINE Punktwolke, nur zur Kontrolle)",
                         variable=self._ln02_create_copc_var,
                         command=self._update_ln02_name_preview
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        h_copc = ttk.Label(sec, text="Via untwine aus den fertigen Tiles - in QGIS auf jeder Zoomstufe sichtbar",
                            font=("", 8), justify="left")
        h_copc.grid(row=4, column=0, columnspan=3, sticky="w", padx=(20, 0))
        self._dim_labels.append(h_copc)

        self._ln02_create_raster_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Create DSM-Raster from LAS/LAZ  (ein Gesamt-TIFF+TFW fuer die AOI)",
                         variable=self._ln02_create_raster_var,
                         command=self._on_ln02_create_raster_toggle
                         ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        h_rast = ttk.Label(sec, text="Tiles -> DSM (IDW) -> Loecher bis 900 m2 gefuellt -> AOI-Maske -> Hillshade",
                            font=("", 8), justify="left")
        h_rast.grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0))
        self._dim_labels.append(h_rast)

        self._ln02_gsd_frame = ttk.Frame(sec)
        self._ln02_gsd_frame.grid(row=7, column=0, columnspan=3, sticky="w",
                                   padx=(20, 0), pady=(4, 0))
        lbl3 = ttk.Label(self._ln02_gsd_frame, text="Raster-Aufloesung (GSD):",
                          font=("Segoe UI", 9, "bold"))
        lbl3.pack(side="left")
        self._ln02_gsd_var = tk.StringVar(value="0.5")
        ttk.Entry(self._ln02_gsd_frame, textvariable=self._ln02_gsd_var, width=10
                   ).pack(side="left", padx=(8, 8))
        h3 = ttk.Label(self._ln02_gsd_frame, text="in Metern, z.B. 0.5", font=("", 8))
        h3.pack(side="left")
        self._dim_labels.append(h3)
        self._ln02_gsd_var.trace_add("write", lambda *_: self._update_ln02_name_preview())
        self._on_ln02_create_raster_toggle()

        name_lbl = ttk.Label(sec, text="Ausgabe-Benennung:", font=("Segoe UI", 9, "bold"))
        name_lbl.grid(row=8, column=0, sticky="nw", pady=(10, 3))
        self._ln02_name_preview_lbl = ttk.Label(sec, text="–", font=("Courier New", 9),
                                                 justify="left")
        self._ln02_name_preview_lbl.grid(row=8, column=1, columnspan=2, sticky="w",
                                          padx=(8, 0), pady=(10, 3))
        self._accent_labels.append(self._ln02_name_preview_lbl)

        h_name = ttk.Label(sec, text="[thinnedout<NN>_] und <E>_<N> kommen aus dem Input-Dateinamen", font=("", 8), justify="left")
        h_name.grid(row=9, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h_name)

        meta_lbl = ttk.Label(sec, text="Ziel-Metadaten:", font=("Segoe UI", 9, "bold"))
        meta_lbl.grid(row=10, column=0, sticky="nw", pady=(6, 3))
        meta_val = ttk.Label(sec, justify="left", font=("", 8),
                              text="LAS 1.4 / PF7 (mit RGB), global_encoding 17, scale 0.01, Offset = Tile-Ursprung\n"
                                   "CRS LV95 + LN02 (EPSG:2056+5728) als byte-exakte VLRs 34735 + 2112")
        meta_val.grid(row=10, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 3))
        self._dim_labels.append(meta_val)

        for var in (self._ln02_jahr_var, self._ln02_area_var):
            var.trace_add("write", lambda *_: self._update_ln02_name_preview())
        self._update_ln02_name_preview()

    def _on_ln02_create_raster_toggle(self):
        """GSD-Feld, Raster-Output-Ordner und AOI-Shape nur zeigen, wenn ein Raster
        gebaut wird - fuer die reine Punktwolken-Konversion wird nichts davon gebraucht."""
        active = self._ln02_create_raster_var.get()
        for attr in ("_ln02_gsd_frame", "_ln02_out_raster_frame", "_ln02_clip_frame"):
            frame = getattr(self, attr, None)
            if frame is None:
                continue
            if active:
                frame.grid()
            else:
                frame.grid_remove()
        self._update_ln02_name_preview()

    def _update_ln02_name_preview(self):
        if getattr(self, "_ln02_name_preview_lbl", None) is None:
            return
        jahr = self._ln02_jahr_var.get().strip() or "JAHR"
        area = self._ln02_area_var.get().strip() or "AREA"
        out_format = getattr(self, "_ln02_out_format_var", None)
        ext = out_format.get() if out_format is not None else "laz"
        text = (f"Punktwolke (pro 1km-Tile):  "
                f"{jahr}_{area}_TIN_[thinnedout<NN>_]raw_<E>_<N>_LV95_LN02.{ext}")
        if getattr(self, "_ln02_create_copc_var", None) and self._ln02_create_copc_var.get():
            text += f"\nQC-Punktwolke (COPC):         copc_QC\\{jahr}_{area}_checkData_LV95_LN02.copc.laz"
        if getattr(self, "_ln02_create_raster_var", None) and self._ln02_create_raster_var.get():
            try:
                gsd_label = f"{round(float(self._ln02_gsd_var.get().strip().replace('m', '')) * 100)}cm"
            except (ValueError, AttributeError):
                gsd_label = "GSD"
            text += (f"\nDSM (gesamte AOI):            {jahr}_{area}_DSM_{gsd_label}_LV95_LN02.tif  (+ .tfw)"
                     f"\nHillshade (gesamte AOI):      {jahr}_{area}_hillshade_{gsd_label}_LV95_LN02.tif  (+ .tfw)")
        self._ln02_name_preview_lbl.config(text=text)

    def _build_ln02_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Ordner & Shapes", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        row = 0
        lbl = ttk.Label(sec, text="Input-Ordner (LN02-Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=3)
        self._ln02_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._ln02_in_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_ln02_input
                    ).grid(row=row, column=2, pady=3)
        row += 1
        h = ttk.Label(sec, text="Name muss auf _<E>_<N>_LV95_<LHN95|LN02>.las/.laz enden (daraus der Tile-Ursprung)", font=("", 8), justify="left")
        h.grid(row=row, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        lbl = ttk.Label(sec, text="Output-Ordner (Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._ln02_out_las_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._ln02_out_las_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Ordner…", command=self._browse_ln02_output_las
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1
        fmt_row = ttk.Frame(sec)
        fmt_row.grid(row=row, column=1, columnspan=2, sticky="w", padx=(8, 0))
        ttk.Label(fmt_row, text="Ausgabeformat:", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._ln02_out_format_var = tk.StringVar(value="laz")
        ttk.Combobox(fmt_row, textvariable=self._ln02_out_format_var,
                     values=["las", "laz"], state="readonly", width=6
                     ).pack(side="left", padx=(8, 8))
        self._ln02_out_format_var.trace_add("write", lambda *_: self._update_ln02_name_preview())
        h = ttk.Label(fmt_row, text="'laz' = GDWH-Auslieferungsformat",
                       font=("", 8))
        h.pack(side="left")
        self._dim_labels.append(h)
        row += 1

        self._ln02_out_raster_frame = ttk.Frame(sec)
        self._ln02_out_raster_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 3))
        self._ln02_out_raster_frame.columnconfigure(1, weight=1)
        lbl = ttk.Label(self._ln02_out_raster_frame, text="Output-Ordner (DSM-Raster):",
                         font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w")
        self._ln02_out_raster_var = tk.StringVar()
        ttk.Entry(self._ln02_out_raster_frame, textvariable=self._ln02_out_raster_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4))
        ttk.Button(self._ln02_out_raster_frame, text="Ordner…",
                    command=self._browse_ln02_output_raster).grid(row=0, column=2)
        h = ttk.Label(self._ln02_out_raster_frame,
                       text="DSM + Hillshade, je ein .tif/.tfw fuer die AOI",
                       font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        row += 1

        self._ln02_clip_frame = ttk.Frame(sec)
        self._ln02_clip_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 3))
        self._ln02_clip_frame.columnconfigure(1, weight=1)
        lbl = ttk.Label(self._ln02_clip_frame, text="Footprint / AOI-Shape:",
                         font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w")
        self._ln02_clip_var = tk.StringVar()
        ttk.Entry(self._ln02_clip_frame, textvariable=self._ln02_clip_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4))
        ttk.Button(self._ln02_clip_frame, text="Datei…",
                    command=self._browse_ln02_clip_shape).grid(row=0, column=2)
        h = ttk.Label(self._ln02_clip_frame,
                       text="Nur fuer das Raster: ausserhalb -> NoData. Die Punktwolke wird nicht gecroppt.", font=("", 8), justify="left")
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)
        self._on_ln02_create_raster_toggle()

    def _build_ln02_dateiinfo(self, parent):
        sec = ttk.LabelFrame(parent, text="Datei-Info  (aus Quelldatei gelesen)",
                              padding=10, style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        fields = [
            ("Anzahl Punkte:",   "_ln02_info_count"),
            ("Extent (X/Y):",    "_ln02_info_extent"),
            ("Z-Bereich:",       "_ln02_info_zrange"),
            ("Koordinatensys.:", "_ln02_info_crs"),
            ("LAS-Version:",     "_ln02_info_version"),
            ("Farbe (RGB):",     "_ln02_info_rgb"),
            ("global_encoding:", "_ln02_info_globalenc"),
            ("Komprimiert:",     "_ln02_info_compressed"),
            ("Dateigroesse:",    "_ln02_info_size"),
        ]
        for row, (label, attr) in enumerate(fields):
            lbl = ttk.Label(sec, text=label, font=("Segoe UI", 9, "bold"))
            lbl.grid(row=row, column=0, sticky="w", pady=1)
            val = ttk.Label(sec, text="–", font=("Segoe UI", 9))
            val.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=1)
            setattr(self, attr, val)
            self._accent_labels.append(val)

        info_hint = ttk.Label(sec,
            text="Aus dem ersten Tile (pdal info)  |  'LAS 1.4 / PF7' + '17' = schon im Zielformat",
            font=("", 8), justify="left")
        info_hint.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._dim_labels.append(info_hint)

        refresh_btn = ttk.Button(sec, text="Datei-Info aktualisieren",
                                  command=self._refresh_ln02_info)
        refresh_btn.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_ln02_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging & Parallelisierung", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._ln02_staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._ln02_staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner…", command=self._browse_ln02_staging
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Zwischendateien (PDAL-Pipelines, Zell-Raster)",
                       font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="CPU-Kerne:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        cpu_max = max(1, os.cpu_count() or 8)
        self._ln02_workers_var = tk.StringVar(value=str(min(6, cpu_max)))
        tk.Spinbox(sec, from_=1, to=cpu_max, textvariable=self._ln02_workers_var, width=6
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        self._ln02_keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._ln02_keep_staging_var
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ── Tab: Create DSM-Raster ────────────────────────────────────────────────
    def _build_dsm_tab(self, parent):
        sf = self._build_scrollable(parent, "_canvas_dsm", "_sf_dsm")

        self._build_group_header(sf, "Projekt-Parameter")
        self._build_dsm_projekt(sf)

        self._build_group_header(sf, "Dateien")
        self._build_dsm_dateien(sf)

        self._build_group_header(sf, "Staging & Parallelisierung")
        self._build_dsm_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn_dsm = ttk.Button(btn_row, text="\u25b6   DSM-RASTER ERSTELLEN",
                                          command=self._start_dsm)
        self._start_btn_dsm.pack(side="right", ipadx=22, ipady=7)

    def _build_dsm_projekt(self, parent):
        sec = ttk.LabelFrame(parent, text="Projekt", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        intro = ttk.Label(sec, text="Beliebige LAS/LAZ-Kacheln -> EIN DSM + Hillshade. Punkte und Hoehen bleiben unveraendert.",
                           font=("", 8), justify="left")
        intro.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._dim_labels.append(intro)

        lbl = ttk.Label(sec, text="Jahr:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=1, column=0, sticky="w", pady=3)
        self._dsm_jahr_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._dsm_jahr_var, width=12
                   ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
        h = ttk.Label(sec, text="z.B.  2026", font=("", 8))
        h.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="AREA / AOI - Name:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=3)
        self._dsm_area_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._dsm_area_var, width=24
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        h2 = ttk.Label(sec, text="z.B.  GUPPENFIRN", font=("", 8))
        h2.grid(row=2, column=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

        lbl3 = ttk.Label(sec, text="Hoehenbezug:", font=("Segoe UI", 9, "bold"))
        lbl3.grid(row=3, column=0, sticky="w", pady=3)
        self._dsm_href_var = tk.StringVar(value="LHN95")
        ttk.Combobox(sec, textvariable=self._dsm_href_var, values=["LHN95", "LN02"],
                     state="readonly", width=10
                     ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=3)
        h3 = ttk.Label(sec, text="Nur Benennung + SRS-Tag, Z bleibt unveraendert. Vorbelegt aus dem Kachelnamen.",
                        font=("", 8), justify="left")
        h3.grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h3)

        gsd_row = ttk.Frame(sec)
        gsd_row.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 3))
        ttk.Label(gsd_row, text="Raster-Aufloesung (GSD):",
                  font=("Segoe UI", 9, "bold")).pack(side="left")
        self._dsm_gsd_var = tk.StringVar(value="0.5")
        ttk.Entry(gsd_row, textvariable=self._dsm_gsd_var, width=10
                   ).pack(side="left", padx=(8, 8))
        h4 = ttk.Label(gsd_row, text="in Metern, z.B. 0.5", font=("", 8))
        h4.pack(side="left")
        self._dim_labels.append(h4)

        name_lbl = ttk.Label(sec, text="Ausgabe-Benennung:", font=("Segoe UI", 9, "bold"))
        name_lbl.grid(row=6, column=0, sticky="nw", pady=(10, 3))
        self._dsm_name_preview_lbl = ttk.Label(sec, text="\u2013", font=("Courier New", 9),
                                                justify="left")
        self._dsm_name_preview_lbl.grid(row=6, column=1, columnspan=2, sticky="w",
                                         padx=(8, 0), pady=(10, 3))
        self._accent_labels.append(self._dsm_name_preview_lbl)

        for var in (self._dsm_jahr_var, self._dsm_area_var, self._dsm_gsd_var,
                    self._dsm_href_var):
            var.trace_add("write", lambda *_: self._update_dsm_name_preview())
        self._update_dsm_name_preview()

    def _update_dsm_name_preview(self):
        if getattr(self, "_dsm_name_preview_lbl", None) is None:
            return
        jahr = self._dsm_jahr_var.get().strip() or "JAHR"
        area = self._dsm_area_var.get().strip() or "AREA"
        ref  = self._dsm_href_var.get().strip() or "LHN95"
        try:
            gsd_label = f"{round(float(self._dsm_gsd_var.get().strip().replace('m', '')) * 100)}cm"
        except (ValueError, AttributeError):
            gsd_label = "GSD"
        self._dsm_name_preview_lbl.config(
            text=(f"DSM (gesamte AOI):        {jahr}_{area}_DSM_{gsd_label}_LV95_{ref}.tif  (+ .tfw)"
                  f"\nHillshade (gesamte AOI):  {jahr}_{area}_hillshade_{gsd_label}_LV95_{ref}.tif  (+ .tfw)"))

    def _build_dsm_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Ordner & Shapes", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Input-Ordner (LAS/LAZ):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._dsm_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._dsm_in_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026", command=self._browse_dsm_input
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Alle .las/.laz im Ordner, Kachelung beliebig",
                       font=("", 8), justify="left")
        h.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="Output-Ordner (Raster):", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        self._dsm_out_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._dsm_out_var
                   ).grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Ordner\u2026", command=self._browse_dsm_output
                    ).grid(row=2, column=2, pady=(8, 3))
        h2 = ttk.Label(sec, text="DSM + Hillshade, je ein .tif/.tfw",
                        font=("", 8))
        h2.grid(row=3, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

        lbl3 = ttk.Label(sec, text="Footprint / AOI-Shape:", font=("Segoe UI", 9, "bold"))
        lbl3.grid(row=4, column=0, sticky="w", pady=(8, 3))
        self._dsm_clip_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._dsm_clip_var
                   ).grid(row=4, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei\u2026", command=self._browse_dsm_clip_shape
                    ).grid(row=4, column=2, pady=(8, 3))
        h3 = ttk.Label(sec, text="Ausserhalb -> NoData (DSM -3.4028235e+38, Hillshade 255), kein Crop der Punkte",
                        font=("", 8), justify="left")
        h3.grid(row=5, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(h3)

    def _build_dsm_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging & Parallelisierung", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._dsm_staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._dsm_staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026", command=self._browse_dsm_staging
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Zwischendateien (PDAL-Pipelines, Zell-Raster)",
                       font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="CPU-Kerne:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        cpu_max = max(1, os.cpu_count() or 8)
        self._dsm_workers_var = tk.StringVar(value=str(min(6, cpu_max)))
        tk.Spinbox(sec, from_=1, to=cpu_max, textvariable=self._dsm_workers_var, width=6
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        self._dsm_keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._dsm_keep_staging_var
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ── Browse-Helfer (DSM-Tab) ───────────────────────────────────────────────
    def _browse_dsm_input(self):
        path = filedialog.askdirectory(title="Input-Ordner (LAS/LAZ) auswaehlen")
        if not path:
            return
        self._dsm_in_var.set(path.replace("/", "\\"))
        # Jahr/AREA/Hoehenbezug aus dem ersten Kachelnamen vorbelegen, sofern er der
        # Konvention folgt - bei Fremddaten bleibt alles wie eingetippt.
        tiles = sorted(_glob.glob(os.path.join(path, "*.laz")) +
                       _glob.glob(os.path.join(path, "*.las")))
        if not tiles:
            return
        parsed = _project_from_tile_name(tiles[0])
        if not parsed:
            return
        jahr, area, ref = parsed
        if not self._dsm_jahr_var.get().strip():
            self._dsm_jahr_var.set(jahr)
        if not self._dsm_area_var.get().strip():
            self._dsm_area_var.set(area)
        self._dsm_href_var.set(ref)

    def _browse_dsm_output(self):
        path = filedialog.askdirectory(title="Output-Ordner (Raster) auswaehlen")
        if path:
            self._dsm_out_var.set(path.replace("/", "\\"))

    def _browse_dsm_clip_shape(self):
        current   = self._dsm_clip_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) \
                    else self._dsm_in_var.get().strip()
        kwargs = {"title": "Footprint / AOI-Shape auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if start_dir and os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._dsm_clip_var.set(path.replace("/", "\\"))

    def _browse_dsm_staging(self):
        current = self._dsm_staging_var.get().strip()
        kwargs = {"title": "Staging-Ordner auswaehlen"}
        if current and os.path.isdir(current):
            kwargs["initialdir"] = current
        path = filedialog.askdirectory(**kwargs)
        if path:
            self._dsm_staging_var.set(path.replace("/", "\\"))

    # ── Validierung (DSM-Tab) ─────────────────────────────────────────────────
    def _validate_dsm(self):
        errors = []

        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append(
                "OSGeo4W Python nicht gefunden.\n"
                "Bitte Pfad via 'Aendern\u2026' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe)."
            )
        if not self._pdal_exe or not os.path.isfile(self._pdal_exe):
            errors.append(
                "pdal.exe wurde nicht gefunden.\n"
                "Bitte pdal (Teil von OSGeo4W/QGIS) zum System-PATH hinzufuegen."
            )

        jahr = self._dsm_jahr_var.get().strip()
        if not jahr or not jahr.isdigit():
            errors.append("Jahr fehlt oder ist ungueltig (numerisch erwartet, z.B. 2026).")
        if not self._dsm_area_var.get().strip():
            errors.append("AREA / AOI - Name fehlt.")
        if self._dsm_href_var.get().strip().upper() not in ("LHN95", "LN02"):
            errors.append("Hoehenbezug ungueltig (LHN95 oder LN02).")

        in_dir = self._dsm_in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")
        elif not [p for pat in ("*.las", "*.laz")
                  for p in _glob.glob(os.path.join(in_dir, pat))]:
            errors.append(f"Keine .las/.laz Kacheln im Input-Ordner:\n  {in_dir}")

        if not self._dsm_out_var.get().strip():
            errors.append("Output-Ordner (Raster) fehlt.")

        clip = self._dsm_clip_var.get().strip()
        if not clip:
            errors.append("Footprint / AOI-Shape fehlt (wird fuer die Raster-Maskierung gebraucht).")
        elif not os.path.isfile(clip):
            errors.append(f"Footprint / AOI-Shape nicht gefunden:\n  {clip}")

        try:
            if float(self._dsm_gsd_var.get().strip().replace("m", "")) <= 0:
                raise ValueError
        except Exception:
            errors.append("Raster-Aufloesung (GSD) ungueltig (Zahl in Metern erwartet, z.B. 0.5).")

        if not self._dsm_staging_var.get().strip():
            errors.append("Staging-Ordner fehlt.")
        try:
            if int(self._dsm_workers_var.get()) < 1:
                raise ValueError
        except Exception:
            errors.append("CPU-Kerne ungueltig.")

        if errors:
            from tkinter import messagebox
            messagebox.showerror("Eingabe-Fehler",
                                  "\n\n".join(f"\u2022 {e}" for e in errors), parent=self)
            return False
        return True

    # ── Verarbeitung starten (DSM-Tab) ────────────────────────────────────────
    def _start_dsm(self):
        if self._running:
            return
        if not self._validate_dsm():
            return

        cfg = {
            "action":             "process_dsm",
            "jahr":                self._dsm_jahr_var.get().strip(),
            "area":                self._dsm_area_var.get().strip(),
            "height_ref":          self._dsm_href_var.get().strip().upper(),
            "gsd":                 float(self._dsm_gsd_var.get().strip().replace("m", "")),
            "input_dir":           self._dsm_in_var.get().strip(),
            "output_dir_raster":   self._dsm_out_var.get().strip(),
            "clip_shape_path":     self._dsm_clip_var.get().strip(),
            "staging_dir":         self._dsm_staging_var.get().strip(),
            "num_workers":         int(self._dsm_workers_var.get()),
            "keep_staging":        bool(self._dsm_keep_staging_var.get()),
            "pdal_exe":            self._pdal_exe,
        }

        self._running = True
        self._active_start_btn = self._start_btn_dsm
        self._start_btn_dsm.config(state="disabled")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._log("=== DSM-Raster-Erstellung gestartet ===\n\n")

        log_stem = f"{cfg['jahr']}_{cfg['area']}_DSM_{cfg['height_ref']}"
        threading.Thread(
            target=self._run_thread, args=(cfg, log_stem, "DSM-Raster-Erstellung"),
            daemon=True
        ).start()

    # ── Gemeinsame Helfer (Tabs "Create COGTIFF" / "Create COPC") ─────────────
    def _browse_dir_into(self, var, title: str) -> bool:
        """Ordner-Dialog, Ergebnis in 'var'. True, wenn ein Ordner gewaehlt wurde."""
        current = var.get().strip()
        kwargs = {"title": title}
        if current and os.path.isdir(current):
            kwargs["initialdir"] = current
        path = filedialog.askdirectory(**kwargs)
        if not path:
            return False
        var.set(path.replace("/", "\\"))
        return True

    def _show_errors(self, errors: list) -> bool:
        """Zeigt gesammelte Eingabefehler; True, wenn es keine gibt."""
        if not errors:
            return True
        from tkinter import messagebox
        messagebox.showerror("Eingabe-Fehler",
                              "\n\n".join(f"\u2022 {e}" for e in errors), parent=self)
        return False

    def _launch(self, cfg: dict, start_btn, vorgang: str, log_stem: str) -> None:
        """Gemeinsamer Start fuer die Tabs 'Create COGTIFF' und 'Create COPC'."""
        self._running = True
        self._active_start_btn = start_btn
        start_btn.config(state="disabled")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._log(f"=== {vorgang} gestartet ===\n\n")
        threading.Thread(target=self._run_thread, args=(cfg, log_stem, vorgang),
                         daemon=True).start()

    # ── Tab: Create COGTIFF ───────────────────────────────────────────────────
    def _build_cog_tab(self, parent):
        sf = self._build_scrollable(parent, "_canvas_cog", "_sf_cog")

        self._build_group_header(sf, "Dateien")
        self._build_cog_dateien(sf)

        self._build_group_header(sf, "Ausgabe")
        self._build_cog_ausgabe(sf)

        self._build_group_header(sf, "Staging")
        self._build_cog_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn_cog = ttk.Button(btn_row, text="\u25b6   COGTIFF ERSTELLEN",
                                          command=self._start_cog)
        self._start_btn_cog.pack(side="right", ipadx=22, ipady=7)

    def _build_cog_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Input & Output", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        intro = ttk.Label(sec, text="Beliebige TIFF-Kacheln -> EIN COGTIFF (Mosaik). Kein Clip, kein Grid-Zuschnitt.",
                           font=("", 8), justify="left")
        intro.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._dim_labels.append(intro)

        lbl = ttk.Label(sec, text="Input-Ordner (TIFF-Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=1, column=0, sticky="w", pady=3)
        self._cog_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._cog_in_var
                   ).grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026", command=self._browse_cog_input
                    ).grid(row=1, column=2, pady=3)
        h = ttk.Label(sec, text="Alle .tif/.tiff im Ordner (+ .tfw), CRS von den Kacheln",
                       font=("", 8))
        h.grid(row=2, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="Output-Datei (COGTIFF):", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=3, column=0, sticky="w", pady=(8, 3))
        self._cog_out_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._cog_out_var
                   ).grid(row=3, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei\u2026", command=self._browse_cog_output
                    ).grid(row=3, column=2, pady=(8, 3))
        h2 = ttk.Label(sec, text="Pfad inkl. Dateiname, z.B. ...\\2026_GUPPENFIRN_DOP_10cm_LV95.tif",
                        font=("", 8))
        h2.grid(row=4, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

    def _build_cog_ausgabe(self, parent):
        sec = ttk.LabelFrame(parent, text="Baender & Kompression", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl0 = ttk.Label(sec, text="Input (erstes Tile):", font=("Segoe UI", 9, "bold"))
        lbl0.grid(row=0, column=0, sticky="w", pady=3)
        self._cog_info_lbl = ttk.Label(sec, text="\u2013", font=("Segoe UI", 9))
        self._cog_info_lbl.grid(row=0, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        self._accent_labels.append(self._cog_info_lbl)

        lbl = ttk.Label(sec, text="Band-Ausgabe:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=1, column=0, sticky="w", pady=3)
        self._cog_band_var = tk.StringVar(value=BAND_KEEP)
        self._cog_band_combo = ttk.Combobox(sec, textvariable=self._cog_band_var,
                                             values=BAND_CHOICES, state="readonly", width=38)
        self._cog_band_combo.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        self._cog_band_hint_lbl = ttk.Label(sec, text=BAND_HINT_UNKNOWN, font=("", 8))
        self._cog_band_hint_lbl.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(self._cog_band_hint_lbl)

        lbl2 = ttk.Label(sec, text="Kompression:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=3, column=0, sticky="w", pady=(8, 3))
        self._cog_compress_var = tk.StringVar(value="JPEG")
        ttk.Combobox(sec, textvariable=self._cog_compress_var, values=COG_COMPRESSIONS,
                     state="readonly", width=10
                     ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        q_row = ttk.Frame(sec)
        q_row.grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        ttk.Label(q_row, text="JPEG-Qualitaet:", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._cog_quality_var = tk.StringVar(value="90")
        self._cog_quality_entry = ttk.Entry(q_row, textvariable=self._cog_quality_var, width=6)
        self._cog_quality_entry.pack(side="left", padx=(8, 8))
        h = ttk.Label(q_row, text="% (1-100)", font=("", 8))
        h.pack(side="left")
        self._dim_labels.append(h)

        self._cog_compress_hint_lbl = ttk.Label(sec, text="", font=("", 8))
        self._cog_compress_hint_lbl.grid(row=5, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(self._cog_compress_hint_lbl)

        self._cog_compress_var.trace_add("write", lambda *_: self._on_cog_compress_change())
        self._on_cog_compress_change()

    def _on_cog_compress_change(self):
        """JPEG-Qualitaet nur bei JPEG editierbar; Hinweis zur NoData-Behandlung."""
        jpeg = self._cog_compress_var.get().upper() == "JPEG"
        self._cog_quality_entry.config(state="normal" if jpeg else "disabled")
        self._cog_compress_hint_lbl.config(
            text=("Verlustbehaftet, nur 8 bit  |  NoData als interne Maske, ohne Randsaeume"
                  if jpeg else "Verlustfrei  |  der NoData-Wert der Kacheln bleibt erhalten"))

    def _build_cog_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._cog_staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._cog_staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026",
                    command=lambda: self._browse_dir_into(self._cog_staging_var,
                                                          "Staging-Ordner auswaehlen")
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Zwischendateien (VRT, bei JPEG ein Zwischenraster)",
                       font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        self._cog_keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._cog_keep_staging_var
                         ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _browse_cog_input(self):
        if self._browse_dir_into(self._cog_in_var, "Input-Ordner (TIFF-Tiles) auswaehlen"):
            self._refresh_cog_info()

    def _browse_cog_output(self):
        path = filedialog.asksaveasfilename(
            title="Output-Datei (COGTIFF) festlegen", defaultextension=".tif",
            filetypes=[("GeoTIFF", "*.tif *.tiff"), ("Alle Dateien", "*.*")])
        if path:
            self._cog_out_var.set(_normalize_cog_path(path.replace("/", "\\")))

    def _refresh_cog_info(self):
        """Bandzahl, Bit-Tiefe, CRS und NoData des ersten Tiles - steuert die
        Bandauswahl (RGB/NRG nur bei 4-Band)."""
        src_dir = self._cog_in_var.get().strip()
        tiles = sorted({p for pat in ("*.tif", "*.tiff")
                        for p in _glob.glob(os.path.join(src_dir, pat))}) if src_dir else []
        widgets = (self._cog_band_combo, self._cog_band_var, self._cog_band_hint_lbl)
        if not tiles:
            self._cog_info_lbl.config(text="(keine Tiles gefunden)" if src_dir else "\u2013")
            self._apply_band_availability(None, *widgets)
            return
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            self._cog_info_lbl.config(text="OSGeo4W Python nicht gefunden - bitte Pfad setzen")
            return
        self._cog_info_lbl.config(text="wird gelesen\u2026")

        def ui_info(info):
            nd = info.get("nodata")
            nd_txt = "\u2013" if nd is None else "{:g}".format(nd)
            self._cog_info_lbl.config(text="{} Baender  |  {}  |  {}  |  NoData {}".format(
                info.get("bands"), _format_bitdepth(info), info.get("crs", "\u2013"), nd_txt))
            self._apply_band_availability(info.get("bands"), *widgets)

        def ui_error(msg):
            self._cog_info_lbl.config(text="Datei-Info nicht lesbar (Details im Log)")
            self._log(f"Datei-Info Create COGTIFF: {msg}\n")
            self._apply_band_availability(None, *widgets)

        self._fetch_file_info_async(tiles[0], ui_info, ui_error)

    def _validate_cog(self) -> bool:
        errors = []
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append("OSGeo4W Python nicht gefunden.\n"
                          "Bitte Pfad via 'Aendern\u2026' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe).")
        in_dir = self._cog_in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")
        elif not [p for pat in ("*.tif", "*.tiff") for p in _glob.glob(os.path.join(in_dir, pat))]:
            errors.append(f"Keine .tif/.tiff Kacheln im Input-Ordner:\n  {in_dir}")
        out = _normalize_cog_path(self._cog_out_var.get())
        if not out:
            errors.append("Output-Datei (COGTIFF) fehlt.")
        elif not os.path.isabs(out):
            errors.append("Output-Datei bitte mit vollstaendigem Pfad angeben.")
        if self._cog_compress_var.get().upper() == "JPEG":
            try:
                quality = int(self._cog_quality_var.get().strip().rstrip("%"))
                if not 1 <= quality <= 100:
                    raise ValueError
            except Exception:
                errors.append("JPEG-Qualitaet ungueltig (ganze Zahl von 1 bis 100, z.B. 90).")
        if not self._cog_staging_var.get().strip():
            errors.append("Staging-Ordner fehlt.")
        return self._show_errors(errors)

    def _start_cog(self):
        if self._running or not self._validate_cog():
            return
        out = _normalize_cog_path(self._cog_out_var.get())
        self._cog_out_var.set(out)
        compress = self._cog_compress_var.get().upper()
        cfg = {
            "action":       "create_cog",
            "input_dir":    self._cog_in_var.get().strip(),
            "output_path":  out,
            "band_mode":    BAND_MODE_KEYS.get(self._cog_band_var.get(), "keep"),
            "compress":     compress,
            "quality":      (int(self._cog_quality_var.get().strip().rstrip("%"))
                             if compress == "JPEG" else 90),
            "staging_dir":  self._cog_staging_var.get().strip(),
            "keep_staging": bool(self._cog_keep_staging_var.get()),
        }
        self._launch(cfg, self._start_btn_cog, "Create COGTIFF", "COG_" + Path(out).stem)

    # ── Tab: Create COPC ──────────────────────────────────────────────────────
    def _build_copc_tab(self, parent):
        sf = self._build_scrollable(parent, "_canvas_copc", "_sf_copc")

        self._build_group_header(sf, "Dateien")
        self._build_copc_dateien(sf)

        self._build_group_header(sf, "Staging & Parallelisierung")
        self._build_copc_staging(sf)

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", pady=(6, 0))
        self._start_btn_copc = ttk.Button(btn_row, text="\u25b6   COPC ERSTELLEN",
                                           command=self._start_copc)
        self._start_btn_copc.pack(side="right", ipadx=22, ipady=7)

    def _build_copc_dateien(self, parent):
        sec = ttk.LabelFrame(parent, text="Input & Output", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        intro = ttk.Label(sec, text="Beliebige LAS/LAZ-Kacheln -> EIN COPC (Merge via untwine). Keine Punktwolken-Verarbeitung.",
                           font=("", 8), justify="left")
        intro.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._dim_labels.append(intro)

        lbl = ttk.Label(sec, text="Input-Ordner (LAS/LAZ):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=1, column=0, sticky="w", pady=3)
        self._copc_in_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._copc_in_var
                   ).grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026",
                    command=lambda: self._browse_dir_into(self._copc_in_var,
                                                          "Input-Ordner (LAS/LAZ) auswaehlen")
                    ).grid(row=1, column=2, pady=3)
        h = ttk.Label(sec, text="Alle .las/.laz im Ordner (meist .laz), CRS von den Kacheln",
                       font=("", 8))
        h.grid(row=2, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="Output-Datei (COPC):", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=3, column=0, sticky="w", pady=(8, 3))
        self._copc_out_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._copc_out_var
                   ).grid(row=3, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Datei\u2026", command=self._browse_copc_output
                    ).grid(row=3, column=2, pady=(8, 3))
        h2 = ttk.Label(sec, text="Pfad inkl. Dateiname, endet auf .copc.laz (wird sonst ergaenzt)",
                        font=("", 8))
        h2.grid(row=4, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h2)

    def _build_copc_staging(self, parent):
        sec = ttk.LabelFrame(parent, text="Staging & Parallelisierung", padding=10,
                              style="Section.TLabelframe")
        sec.pack(fill="x", pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        lbl = ttk.Label(sec, text="Staging-Ordner:", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=0, column=0, sticky="w", pady=3)
        self._copc_staging_var = tk.StringVar(value=DEFAULT_STAGING_DIR)
        ttk.Entry(sec, textvariable=self._copc_staging_var
                   ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(sec, text="Ordner\u2026",
                    command=lambda: self._browse_dir_into(self._copc_staging_var,
                                                          "Staging-Ordner auswaehlen")
                    ).grid(row=0, column=2, pady=3)
        h = ttk.Label(sec, text="Temp-Dateien von untwine", font=("", 8))
        h.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self._dim_labels.append(h)

        lbl2 = ttk.Label(sec, text="CPU-Kerne:", font=("Segoe UI", 9, "bold"))
        lbl2.grid(row=2, column=0, sticky="w", pady=(8, 3))
        cpu_max = max(1, os.cpu_count() or 8)
        self._copc_workers_var = tk.StringVar(value=str(min(6, cpu_max)))
        tk.Spinbox(sec, from_=1, to=cpu_max, textvariable=self._copc_workers_var, width=6
                   ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 3))

        self._copc_keep_staging_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Staging-Dateien nach Abschluss behalten (nicht loeschen)",
                         variable=self._copc_keep_staging_var
                         ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _browse_copc_output(self):
        path = filedialog.asksaveasfilename(
            title="Output-Datei (COPC) festlegen", defaultextension=".copc.laz",
            filetypes=[("COPC", "*.copc.laz"), ("LAZ", "*.laz"), ("Alle Dateien", "*.*")])
        if path:
            self._copc_out_var.set(_normalize_copc_path(path.replace("/", "\\")))

    def _validate_copc(self) -> bool:
        errors = []
        if not self._osgeo_python or not os.path.isfile(self._osgeo_python):
            errors.append("OSGeo4W Python nicht gefunden.\n"
                          "Bitte Pfad via 'Aendern\u2026' festlegen  (z.B. C:\\OSGeo4W\\bin\\python3.exe).")
        if not self._pdal_exe or not os.path.isfile(self._pdal_exe):
            errors.append("pdal.exe wurde nicht gefunden (prueft die Kachel-Header).\n"
                          "Bitte pdal (Teil von OSGeo4W/QGIS) zum System-PATH hinzufuegen.")
        if not self._untwine_exe or not os.path.isfile(self._untwine_exe):
            errors.append("untwine.exe wurde nicht gefunden (baut das COPC).\n"
                          "Liegt normalerweise im bin-Ordner der QGIS-Installation - diesen "
                          "zum System-PATH hinzufuegen.")
        in_dir = self._copc_in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")
        elif not [p for pat in ("*.las", "*.laz") for p in _glob.glob(os.path.join(in_dir, pat))]:
            errors.append(f"Keine .las/.laz Kacheln im Input-Ordner:\n  {in_dir}")
        out = _normalize_copc_path(self._copc_out_var.get())
        if not out:
            errors.append("Output-Datei (COPC) fehlt.")
        elif not os.path.isabs(out):
            errors.append("Output-Datei bitte mit vollstaendigem Pfad angeben.")
        if not self._copc_staging_var.get().strip():
            errors.append("Staging-Ordner fehlt.")
        try:
            if int(self._copc_workers_var.get()) < 1:
                raise ValueError
        except Exception:
            errors.append("CPU-Kerne ungueltig.")
        return self._show_errors(errors)

    def _start_copc(self):
        if self._running or not self._validate_copc():
            return
        out = _normalize_copc_path(self._copc_out_var.get())
        self._copc_out_var.set(out)
        cfg = {
            "action":       "create_copc",
            "input_dir":    self._copc_in_var.get().strip(),
            "output_path":  out,
            "untwine_exe":  self._untwine_exe,
            "pdal_exe":     self._pdal_exe,
            "staging_dir":  self._copc_staging_var.get().strip(),
            "num_workers":  int(self._copc_workers_var.get()),
            "keep_staging": bool(self._copc_keep_staging_var.get()),
        }
        self._launch(cfg, self._start_btn_copc, "Create COPC",
                     "COPC_" + Path(out).name[:-len(".copc.laz")])

    # ── Tab: DMC - TIFFconverter ───────────────────────────────────────────────
    def _build_tiff_tab(self, parent):
        self.bind_class("TCombobox", "<MouseWheel>", self._fwd_wheel)
        self.bind_all("<MouseWheel>", self._fwd_wheel)

        sf = self._build_scrollable(parent, "_canvas", "_sf")

        self._build_group_header(sf, "Projekt-Parameter")
        self._build_projekt(sf)

        self._build_group_header(sf, "Dateien")
        self._build_dateien(sf)

        self._build_group_header(sf, "Datei-Info  (aus erstem gefundenen Tile)")
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

        lbl4 = ttk.Label(sec, text="Band-Ausgabe:", font=("Segoe UI", 9, "bold"))
        lbl4.grid(row=3, column=0, sticky="w", pady=3)
        self._band_var = tk.StringVar(value=BAND_KEEP)
        self._band_combo = ttk.Combobox(sec, textvariable=self._band_var,
                                         values=BAND_CHOICES, state="readonly", width=38)
        self._band_combo.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3)
        self._band_hint_lbl = ttk.Label(sec, text=BAND_HINT_UNKNOWN, font=("", 8), justify="left")
        self._band_hint_lbl.grid(row=4, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self._dim_labels.append(self._band_hint_lbl)

        self._create_cog_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sec, text="Create COGTIFF  (QC: alle Tiles als EIN Mosaik, nur zur Kontrolle)",
                         variable=self._create_cog_var, command=self._on_create_cog_toggle
                         ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._cog_frame = ttk.Frame(sec)
        self._cog_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=(4, 0))
        ttk.Label(self._cog_frame, text="JPEG-Qualitaet:", font=("Segoe UI", 9, "bold")
                   ).pack(side="left")
        self._cog_quality_var = tk.StringVar(value="90")
        ttk.Entry(self._cog_frame, textvariable=self._cog_quality_var, width=6
                   ).pack(side="left", padx=(8, 8))
        h_cog = ttk.Label(self._cog_frame, text="% (1-100)  |  NoData als interne Maske, ohne Randsaeume",
                           font=("", 8))
        h_cog.pack(side="left")
        self._dim_labels.append(h_cog)

        name_lbl = ttk.Label(sec, text="Ausgabe-Benennung:", font=("Segoe UI", 9, "bold"))
        name_lbl.grid(row=7, column=0, sticky="nw", pady=(8, 3))
        self._name_preview_lbl = ttk.Label(sec, text="–", font=("Courier New", 9), justify="left")
        self._name_preview_lbl.grid(row=7, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 3))
        self._accent_labels.append(self._name_preview_lbl)

        for var in (self._jahr_var, self._area_var, self._gsd_var, self._band_var):
            var.trace_add("write", lambda *_: self._update_name_preview())
        self._on_create_cog_toggle()

    def _on_create_cog_toggle(self):
        """JPEG-Qualitaet nur zeigen, wenn das QC-Mosaik gebaut wird."""
        if self._create_cog_var.get():
            self._cog_frame.grid()
        else:
            self._cog_frame.grid_remove()
        self._update_name_preview()

    def _update_name_preview(self):
        jahr = self._jahr_var.get().strip() or "JAHR"
        area = self._area_var.get().strip() or "AREA"
        gsd  = self._gsd_var.get().strip() or "GSD"
        text = (f"{jahr}_{area}_DOP_{gsd}_<NAME>_LV95.tif  (+ .tfw)"
                f"\nBaender: {BAND_PREVIEW_TEXT[self._band_mode()]}")
        if getattr(self, "_create_cog_var", None) and self._create_cog_var.get():
            text += f"\nQC-Mosaik (COG): cog_QC\\{jahr}_{area}_DOP_{gsd}_checkData_LV95.tif"
        self._name_preview_lbl.config(text=text)

    def _band_mode(self) -> str:
        """Combobox-Beschriftung -> Runner-Schluessel ('keep' | 'rgb' | 'nrg')."""
        return BAND_MODE_KEYS.get(self._band_var.get(), "keep")

    def _apply_band_availability(self, band_count, combo=None, var=None, hint=None) -> None:
        """Die 3-Band-Auszuege setzen einen 4-Band-Input (RGBN) voraus. Passt der
        erkannte Input nicht dazu, wird die Auswahl gesperrt und auf '4-Band'
        zurueckgesetzt - sonst laeuft der Job erst im Runner in einen Fehler.
        band_count=None bedeutet 'unbekannt' (keine Datei-Info gelesen).
        Ohne Widget-Angabe die des Tabs TIFFconverter; 'Create COGTIFF' uebergibt
        seine eigenen."""
        combo = combo or self._band_combo
        var = var or self._band_var
        hint = hint or self._band_hint_lbl
        try:
            count = int(band_count)
        except (TypeError, ValueError):
            count = None

        if count is None:
            combo.config(state="readonly")
            hint.config(text=BAND_HINT_UNKNOWN)
        elif count >= 4:
            combo.config(state="readonly")
            hint.config(text=BAND_HINT_4BAND)
        else:
            var.set(BAND_KEEP)
            combo.config(state="disabled")
            hint.config(text=BAND_HINT_NOT4.format(count=count))

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

        lbl = ttk.Label(sec, text="Output-Ordner (1km-Tiles):", font=("Segoe UI", 9, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=(8, 3))
        self._out_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._out_var
                   ).grid(row=row, column=1, sticky="ew", padx=(8, 4), pady=(8, 3))
        ttk.Button(sec, text="Ordner…", command=self._browse_output
                    ).grid(row=row, column=2, pady=(8, 3))
        row += 1

        lbl = ttk.Label(sec, text="AOI Clip-Shape (gültige Fläche):", font=("Segoe UI", 9, "bold"))
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
        h = ttk.Label(sec, text="Feld 'NAME' = Tile-Bezeichnung  |  wird nach EPSG:2056 reprojiziert",
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
            text="Aus dem ersten Tile im Input-Ordner",
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
        h = ttk.Label(sec, text="Zwischenraster (VRT, geclipptes Mosaik)", font=("", 8))
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
            for canvas, frame in self._scroll_areas:
                if w in (canvas, frame):
                    return canvas
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
        kwargs = {"title": "AOI Clip-Shape (gültige Flaeche) auswaehlen",
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
        path = filedialog.askdirectory(title="Input-Ordner (.laz-Tiles) auswaehlen")
        if path:
            self._las_in_var.set(path.replace("/", "\\"))
            self._refresh_las_info()

    def _browse_las_output_laz(self):
        path = filedialog.askdirectory(title="Output-Ordner (LAZ-Tiles) auswaehlen")
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

    # ── Datei-/Ordner-Dialoge (LN02-Tab) ───────────────────────────────────────
    def _browse_ln02_input(self):
        path = filedialog.askdirectory(title="Input-Ordner (LN02-Tiles) auswaehlen")
        if path:
            self._ln02_in_var.set(path.replace("/", "\\"))
            self._refresh_ln02_info()

    def _browse_ln02_output_las(self):
        path = filedialog.askdirectory(title="Output-Ordner (Punktwolken-Tiles) auswaehlen")
        if path:
            self._ln02_out_las_var.set(path.replace("/", "\\"))

    def _browse_ln02_output_raster(self):
        path = filedialog.askdirectory(title="Output-Ordner (DSM-Raster) auswaehlen")
        if path:
            self._ln02_out_raster_var.set(path.replace("/", "\\"))

    def _browse_ln02_clip_shape(self):
        current   = self._ln02_clip_var.get().strip()
        start_dir = os.path.dirname(current) if current and os.path.isfile(current) \
                    else self._ln02_in_var.get().strip()
        kwargs = {"title": "Footprint / AOI-Shape auswaehlen",
                  "filetypes": [("Shapefile", "*.shp"), ("Alle Dateien", "*.*")]}
        if start_dir and os.path.isdir(start_dir):
            kwargs["initialdir"] = start_dir
        path = filedialog.askopenfilename(**kwargs)
        if path:
            self._ln02_clip_var.set(path.replace("/", "\\"))

    def _browse_ln02_staging(self):
        current = self._ln02_staging_var.get().strip()
        kwargs = {"title": "Staging-Ordner auswaehlen"}
        if current and os.path.isdir(current):
            kwargs["initialdir"] = current
        path = filedialog.askdirectory(**kwargs)
        if path:
            self._ln02_staging_var.set(path.replace("/", "\\"))

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
            self._apply_band_availability(None)

        if not src_dir or not os.path.isdir(src_dir):
            _reset()
            return

        tiles = sorted(
            {p for pat in ("*.tif", "*.tiff") for p in _glob.glob(os.path.join(src_dir, pat))}
        )
        if not tiles:
            _reset()
            self._info_bands.config(text="(keine Tiles gefunden)")
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
                self._apply_band_availability(info.get("bands"))
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

    # ── Datei-Info via pdal (beide LAS-Tabs) ───────────────────────────────────
    def _refresh_las_info(self):
        self._refresh_pointcloud_info(self._las_in_var.get().strip(), (
            ("_las_info_count",      _pc_count),
            ("_las_info_extent",     _pc_extent),
            ("_las_info_zrange",     _pc_zrange),
            ("_las_info_crs",        _pc_crs),
            ("_las_info_rgb",        _pc_rgb),
            ("_las_info_compressed", _pc_compressed),
            ("_las_info_size",       _pc_size),
        ))

    def _refresh_ln02_info(self):
        self._refresh_pointcloud_info(self._ln02_in_var.get().strip(), (
            ("_ln02_info_count",      _pc_count),
            ("_ln02_info_extent",     _pc_extent),
            ("_ln02_info_zrange",     _pc_zrange),
            ("_ln02_info_crs",        _pc_crs),
            ("_ln02_info_version",    _pc_version),
            ("_ln02_info_rgb",        _pc_rgb),
            ("_ln02_info_globalenc",  _pc_globalenc),
            ("_ln02_info_compressed", _pc_compressed),
            ("_ln02_info_size",       _pc_size),
        ))

    def _refresh_pointcloud_info(self, src_dir: str, fields: tuple):
        """Liest die Metadaten des ersten gefundenen Punktwolken-Tiles im Ordner
        ('pdal info --metadata', headerbasiert - kein Decompress der Punktdaten) und
        fuellt damit die uebergebenen Label-Felder. Gemeinsam genutzt von beiden
        LAS-Tabs, die sich nur in den angezeigten Feldern unterscheiden.

        fields = ((Label-Attributname, Formatierungsfunktion(meta, pfad)), ...)"""
        def _reset():
            for attr, _fn in fields:
                getattr(self, attr).config(text="–")

        if not src_dir or not os.path.isdir(src_dir):
            _reset()
            return

        tiles = sorted(
            {p for pat in ("*.laz", "*.las") for p in _glob.glob(os.path.join(src_dir, pat))}
        )
        if not tiles:
            _reset()
            getattr(self, fields[0][0]).config(text="(keine .laz/.las Tiles gefunden)")
            return
        sample = tiles[0]

        if not self._pdal_exe or not os.path.isfile(self._pdal_exe):
            _reset()
            getattr(self, fields[0][0]).config(
                text="pdal.exe nicht gefunden – bitte zum PATH hinzufuegen")
            return

        def ui_error(msg):
            try:
                from tkinter import messagebox
                messagebox.showerror("Datei-Info Fehler", msg, parent=self)
            except Exception:
                pass
            _reset()

        def ui_info(meta):
            # Jedes Feld einzeln absichern - ein fehlender Metadaten-Eintrag soll
            # nicht die ganze Anzeige leeren.
            for attr, fn in fields:
                try:
                    getattr(self, attr).config(text=fn(meta, sample))
                except Exception:
                    getattr(self, attr).config(text="–")

        def worker():
            try:
                result = subprocess.run([self._pdal_exe, "info", "--metadata", sample],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         universal_newlines=True)
                if result.returncode != 0:
                    self.after(0, ui_error,
                                (result.stderr or result.stdout or "unbekannter Fehler").strip())
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
        for canvas, _frame in self._scroll_areas:
            canvas.configure(bg=T["panel"], highlightbackground=T["sep"])

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

        if self._create_cog_var.get():
            try:
                quality = int(self._cog_quality_var.get().strip().rstrip("%"))
                if not 1 <= quality <= 100:
                    raise ValueError
            except Exception:
                errors.append("JPEG-Qualitaet ungueltig (ganze Zahl von 1 bis 100, z.B. 90).")

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
            errors.append("AOI Clip-Shape (gültige Fläche) fehlt.")
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
            "band_mode":        self._band_mode(),
            "create_cog":       bool(self._create_cog_var.get()),
            "cog_quality":      (int(self._cog_quality_var.get().strip().rstrip("%"))
                                 if self._create_cog_var.get() else 90),
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
            errors.append("Output-Ordner (LAZ-Tiles) fehlt.")

        if self._las_create_raster_var.get():
            out_dir_raster = self._las_out_raster_var.get().strip()
            if not out_dir_raster:
                errors.append("Output-Ordner (DSM-Raster) fehlt (da 'Create DSM-Raster from LAZ' aktiviert ist).")

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

    # ── Validierung (LN02-Tab) ────────────────────────────────────────────────
    def _validate_ln02(self):
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

        if self._ln02_create_copc_var.get() and (
                not self._untwine_exe or not os.path.isfile(self._untwine_exe)):
            errors.append(
                "untwine.exe wurde nicht gefunden (wird fuer 'Create COPC' gebraucht).\n"
                "Liegt normalerweise im bin-Ordner der QGIS-Installation - diesen zum "
                "System-PATH hinzufuegen oder die Option abwaehlen.")

        jahr = self._ln02_jahr_var.get().strip()
        if not jahr or not jahr.isdigit():
            errors.append("Jahr fehlt oder ist ungueltig (numerisch erwartet, z.B. 2026).")

        area = self._ln02_area_var.get().strip()
        if not area:
            errors.append("AREA / AOI - Name fehlt.")

        in_dir = self._ln02_in_var.get().strip()
        if not in_dir:
            errors.append("Input-Ordner fehlt.")
        elif not os.path.isdir(in_dir):
            errors.append(f"Input-Ordner nicht gefunden:\n  {in_dir}")

        out_dir = self._ln02_out_las_var.get().strip()
        if not out_dir:
            errors.append("Output-Ordner (Punktwolken-Tile) fehlt.")
        elif in_dir and os.path.isdir(in_dir) and os.path.isdir(out_dir) and \
                os.path.normcase(os.path.abspath(in_dir)) == os.path.normcase(os.path.abspath(out_dir)):
            # Sonst wuerde die Ausgabe je nach Benennung die Quelle ueberschreiben.
            errors.append("Input- und Output-Ordner sind identisch - bitte ein separates "
                           "Zielverzeichnis waehlen (die Quelldateien bleiben so unangetastet).")

        if self._ln02_create_raster_var.get():
            try:
                gsd = float(self._ln02_gsd_var.get().strip().replace("m", ""))
                if gsd <= 0:
                    raise ValueError
            except Exception:
                errors.append("Raster-Aufloesung (GSD) ungueltig (Zahl in Metern erwartet, z.B. 0.5).")

            if not self._ln02_out_raster_var.get().strip():
                errors.append("Output-Ordner (DSM-Raster) fehlt (da 'Create DSM-Raster' aktiviert ist).")

            clip = self._ln02_clip_var.get().strip()
            if not clip:
                errors.append("Footprint / AOI-Shape fehlt (wird fuer die Raster-Maskierung gebraucht).")
            elif not os.path.isfile(clip):
                errors.append(f"Footprint / AOI-Shape nicht gefunden:\n  {clip}")

        if not self._ln02_staging_var.get().strip():
            errors.append("Staging-Ordner fehlt.")

        try:
            if int(self._ln02_workers_var.get()) < 1:
                raise ValueError
        except Exception:
            errors.append("CPU-Kerne ungueltig.")

        if errors:
            from tkinter import messagebox
            messagebox.showerror("Eingabe-Fehler",
                                  "\n\n".join(f"• {e}" for e in errors), parent=self)
            return False
        return True

    # ── Konvertierung starten (LN02-Tab) ──────────────────────────────────────
    def _start_ln02(self):
        if self._running:
            return
        if not self._validate_ln02():
            return

        create_raster = bool(self._ln02_create_raster_var.get())
        cfg = {
            "action":             "process_las_ln02",
            "jahr":                self._ln02_jahr_var.get().strip(),
            "area":                self._ln02_area_var.get().strip(),
            "create_raster":       create_raster,
            "gsd":                 float(self._ln02_gsd_var.get().strip().replace("m", ""))
                                   if create_raster else None,
            "create_copc":         bool(self._ln02_create_copc_var.get()),
            "untwine_exe":         self._untwine_exe,
            "input_dir":           self._ln02_in_var.get().strip(),
            "output_dir_las":      self._ln02_out_las_var.get().strip(),
            "output_dir_raster":   self._ln02_out_raster_var.get().strip() if create_raster else None,
            "out_format":          self._ln02_out_format_var.get(),
            "clip_shape_path":     self._ln02_clip_var.get().strip() if create_raster else None,
            "staging_dir":         self._ln02_staging_var.get().strip(),
            "num_workers":         int(self._ln02_workers_var.get()),
            "keep_staging":        bool(self._ln02_keep_staging_var.get()),
            "pdal_exe":            self._pdal_exe,
        }

        self._running = True
        self._active_start_btn = self._start_btn_ln02
        self._start_btn_ln02.config(state="disabled")
        self._progress_frame.pack(fill="x", padx=12, pady=(0, 4), before=self._btn_row)
        self._progress_bar.start(10)
        self._clear_log()
        self._log("=== DMC LAS-Konvertierung [LN02] gestartet ===\n\n")

        log_stem = f"{cfg['jahr']}_{cfg['area']}_TIN_LN02"
        threading.Thread(
            target=self._run_thread, args=(cfg, log_stem, "DMC LAS-Konvertierung [LN02]"),
            daemon=True
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
                        continue  # Steuerzeile - nicht ins Log/GUI schreiben
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
