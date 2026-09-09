"""
_osgeo_runner.py - Wird via OSGeo4W Python aufgerufen (NICHT direkt starten).
Liest Parameter aus einer JSON-Datei und fuehrt GDAL-abhaengige Funktionen aus.
Ausgabe geht auf stdout -> wird vom GUI live im Log angezeigt.

Aktionen:
    info        - Metadaten aus Quelldatei lesen, Ergebnis als JSON auf stdout
    process     - DMC-TIFF-Pipeline (Tab "DMC - TIFFconverter"):
                  1) Mosaik der technischen 200m-Kacheln (bestehendes True_Ortho.vrt
                     wird uebernommen, falls vorhanden, sonst frisch aus *.tif gebaut)
                  2) Cutline-Clip auf die gueltige Flaeche (alles ausserhalb -> NoData)
                  3) Zuschnitt auf das 1km x 1km-Grid (Dateiname aus Attribut 'NAME'),
                     parallelisiert ueber mehrere Kerne, Zwischenergebnisse im
                     Staging-Ordner (z.B. Y:\\02_DMC_tempProcessingFolder)
    process_las - DMC-LAS-Pipeline (Tab "DMC - LASconverter [LHN95]"), siehe
                  Kommentarblock direkt ueber _process_las() weiter unten.
    process_las_ln02 - DMC-LAS-Pipeline LN02 (Tab "DMC - LASconverter [LN02]"),
                  siehe Kommentarblock direkt ueber _process_las_ln02() weiter unten.
    process_dsm - DSM + Hillshade aus einem beliebigen LAS/LAZ-Ordner
                  (Tab "Create DSM-Raster"), siehe Kommentarblock ueber _process_dsm().
"""

import sys
import os
import base64
import glob
import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# NoData-Sentinel des FERTIGEN Float32-DSM, analog GDWH-Konvention bei SB_DSM
# (Raster, nicht Hillshade). Exakt -FLT_MAX; gesetzt wird er ausschliesslich von
# GDAL (gdal.Warp dstNodata, siehe _mosaic_las_raster) - GDAL nimmt ihn anstandslos.
LAS_RASTER_NODATA = -3.4028234663852886e+38

# DSM: KLEINE NoData-Loecher werden interpoliert, GROSSE bleiben echtes NoData.
# Fachliche Trennung: Loecher bis LAS_FILL_MAX_HOLE_AREA_M2 sind Rauschen der
# Autokorrelation, dort liegt die Interpolation im Genauigkeitsbudget. Groessere
# Fehlstellen (Felswaende ohne Korrelation) bleiben als NoData stehen, damit im
# Endprodukt erkennbar bleibt, wo nicht gemessen werden konnte.
LAS_FILL_NODATA_HOLES       = True
LAS_FILL_MAX_HOLE_AREA_M2   = 900.0  # Flaechenschwelle, GSD-unabhaengig definiert
LAS_FILL_HOLE_CONNECTEDNESS = 8      # 8 = diagonal beruehrende Pixel sind EIN Loch
# Glaettungsdurchgaenge nach der Interpolation. 0 = keine: so ist ausgeschlossen, dass
# ein Glaettungsfilter GEMESSENE Werte antastet. Bei sichtbaren Scanline-Artefakten in
# gefuellten Flaechen auf 2-3 erhoehen (GDAL glaettet dabei laut Doku nur die
# interpolierten Pixel).
LAS_FILL_SMOOTHING_ITERATIONS = 0

# Hillshade (Byte): 255 bedeutet ausschliesslich "ausserhalb des AOI". Innerhalb des
# AOI traegt kein einziges Pixel 255 - weder ein voll beleuchtetes (gdaldem liefert
# 1..255) noch eines ueber einem NoData-Loch des DSM. Beide werden auf 254 gezogen,
# siehe _prepare_hillshade_values.
LAS_HILLSHADE_NODATA    = 255
LAS_HILLSHADE_VALID_MAX = 254

# NoData-Sentinel der ZWISCHEN-Zellraster aus writers.gdal (Staging-Ordner, Wegwerf-
# produkte). Bewusst NICHT -FLT_MAX: PDALs writers.gdal prueft den nodata-Wert gegen
# den Float32-Wertebereich und lehnt die Bereichsgrenze selbst ab -
#   "Invalid nodata value -3.402823466e+38 for output data_type 'float'"
# - und zwar deterministisch fuer jede Zelle, unabhaengig von Parallelisierung oder
# Speicher (empirisch: PDAL 2.x aus QGIS 3.42.1; der uebergebene Wert ist nachweislich
# bitgenau -FLT_MAX, die Pruefung ist an der Grenze exklusiv). Deshalb schreibt PDAL
# einen unverfaenglichen Sentinel, den gdal.Warp beim Mosaikieren auf
# LAS_RASTER_NODATA umsetzt. -9999 ist in Float32 exakt darstellbar und kann als
# Hoehenwert in der Schweiz nicht vorkommen.
LAS_CELL_NODATA = -9999.0

# Ausgabeformat der Punktwolken-Tiles im Tab [LHN95]. Diese Dateien sind die EINGABE
# fuer den GeoSuite/REFRAME-Batch (LHN95 -> LN02), deshalb wird der Header hier
# explizit gesetzt statt PDALs Defaults zu uebernehmen (PDAL entscheidet das Format
# sonst anhand der vorhandenen Dimensionen und koennte je nach Quelle wechseln).
#
# LAS 1.4 / Point Data Record Format 7 = PF6 + RGB. Damit bleibt die Farbe der
# DMC-Quelldaten (Reality Studio) ueber die GANZE Kette erhalten: die Zwischenstufe
# fuehrt sie mit, GeoSuite/REFRAME transformiert sie mit (seit dem Update vom
# 2026-09-09 liest und schreibt GeoSuite LAS 1.4/PF7), und der Tab [LN02] uebernimmt
# sie unveraendert ins GDWH-Produkt.
#
# HISTORIE (nicht wieder einbauen): vor dem GeoSuite-Update musste hier LAS 1.2 / PF1
# geschrieben werden, weil GeoSuite nur klassisches LAS (1.0-1.2, PF0-PF3) las und
# alles andere mit "ERROR: File format incorrect ... unknown or unsupported format"
# ablehnte. Die Farbe wurde damals ueber einen zweiten, farbfuehrenden "PF7-Master"
# und einen Index-Join im Tab [LN02] gerettet. Dieser Umweg ist mit dem Update
# hinfaellig und wurde ersatzlos entfernt.
LAS_OUT_MINOR_VERSION = 4
LAS_OUT_POINT_FORMAT  = 7

# Point Data Record Formats mit RGB-Feldern (LAS 1.4 R15, Tabellen 6-13). Aus dem
# Punktformat der Quelle laesst sich damit headerbasiert - also gratis - sagen, ob
# ueberhaupt Farbe vorhanden sein KANN.
PC_FORMATS_WITH_RGB = (2, 3, 5, 7, 8, 10)

# CRS-Tag der Zwischenausgabe: NUR horizontal (LV95). Der Hoehenbezug wird bewusst
# NICHT getaggt - REFRAME bekommt Ein- und Ausgangsrahmen ohnehin aus der Batch-
# Konfiguration, und ein VerticalCSTypeGeoKey (5729) in den GeoTIFF-Keys ist genau die
# Art Header-Zusatz, die die etablierten Quell-Tiles nicht haben. Den autoritativen
# LV95/LN02-Tag setzt erst der Tab [LN02] per byte-exakter VLR-Injektion.
LAS_OUT_SRS = "EPSG:2056"

# Erwartetes SRS der Input-.laz-Kacheln (LV95 + LHN95). Wird den Readern explizit
# aufgezwungen (override_srs), damit eine Kachel mit fehlendem/falschem SRS-Tag
# nicht still mit einer abweichenden Referenz in den Merge einfliesst.
LAS_INPUT_SRS = "EPSG:2056+5729"

# ─── Zielwerte fuer die GDWH-taugliche LAS-1.4-Ausgabe (Tab "DMC - LASconverter [LN02]") ──
# Identisch zu SB_DSM_PUNKTWOLKE (Projekt topo-importDATAtoGDWH-STAC, Skript
# 4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py), damit die DMC-Punktwolken strukturell
# kongruent zu swissSURFACE3D sind und in den GDWH importiert werden koennen.
# Abweichung zu SB_DSM_PUNKTWOLKE/swissSURFACE3D: dort PF6, hier PF7 (= PF6 + RGB,
# 36 statt 30 Byte). Die DMC-Daten fuehren Farbe, und sie soll bis ins GDWH-Produkt
# erhalten bleiben. Alles Uebrige (Version, global_encoding, header_size, scale,
# CRS-VLRs) ist identisch zur etablierten Lieferkette.
#
# Die von PF6/PF7 verlangte GpsTime bleibt leer: die Quelle ist PF2 und fuehrt gar
# keine GPS-Zeit (photogrammetrisch abgeleitete Punkte haben keinen Zeitstempel).
# global_encoding bleibt trotzdem 17 - siehe die Begruendung bei den GpsTime-
# Warnungen in _ln02_tile_worker.
LN02_MINOR_VERSION    = 4
LN02_POINT_FORMAT     = 7      # Point Data Record Format 7 (PF6 + RGB)
LN02_POINT_LENGTH     = 36
LN02_HEADER_SIZE      = 375
LN02_GLOBAL_ENCODING  = 17     # Bit 0 (Adjusted Standard GPS Time) + Bit 4 (WKT)
LN02_SCALE            = 0.01   # Schweizer Konvention (keine uebertriebene Praezision)
LN02_BBOX_TOLERANCE_M = 0.01   # zulaessige BBox-Abweichung Quelle vs. Ziel nach Requantisierung

# SRS der LN02-Kacheln (LV95 + LN02). Wird NUR den Readern der Raster-Pipeline
# aufgezwungen; die CRS-Tags der Punktwolken-Ausgabe kommen ausschliesslich aus den
# byte-exakten Referenz-VLRs (siehe _inject_reference_vlrs).
LAS_LN02_SRS = "EPSG:2056+5728"

# ─── Virtual Point Cloud (VPC) fuer QGIS ───────────────────────────────────────
# Eine .vpc ist das Punktwolken-Gegenstueck zum Raster-VRT: eine JSON-Datei (STAC-
# FeatureCollection), die alle Kacheln zu EINER Ebene zusammenfasst. QGIS liest das
# ab 3.32 nativ. Reines Ansichtsprodukt - es wird nichts kopiert und nichts
# umgerechnet, die Datei verweist nur relativ auf die Kacheln daneben.
#
# ArcGIS Pro liest KEIN VPC. Dafuer braucht es ein LAS-Dataset (.lasd), das nur
# arcpy erzeugen kann - hier bewusst nicht umgesetzt (kein arcpy im OSGeo4W-Python).
#
# Das Format ist an einer mit 'pdal_wrench build_vpc' erzeugten Referenz-VPC
# nachgemessen und gegen den QGIS-Provider gegengelesen (QGIS 3.44). Dabei gilt:
#   - 'proj:wkt2' MUSS gesetzt sein, sonst lehnt QGIS die Datei ab.
#   - 'geometry'/'bbox' sind WGS84 (STAC-Konvention), die Landeskoordinaten stehen
#     in 'proj:bbox'. Mit LV95 in 'bbox' laedt QGIS die Ebene zwar OHNE Fehler,
#     liefert aber einen unendlichen Extent - man sieht nichts. Genau deshalb steht
#     die Umrechnung nach WGS84 unten und nicht "spaeter vielleicht".
#   - 'pc:schemas', 'stac_extensions' und 'proj:geometry' sind optional (geprueft).
VPC_SUBDIR = "_vpc"
VPC_STAC_VERSION = "1.0.0"
VPC_STAC_EXTENSIONS = [
    "https://stac-extensions.github.io/pointcloud/v1.0.0/schema.json",
    "https://stac-extensions.github.io/projection/v1.1.0/schema.json",
]
# STAC-Pointcloud-Typ: 'eopc' = electro-optical point cloud. Fachlich korrekt fuer
# photogrammetrisch abgeleitete Wolken (DMC/Reality Studio) - 'lidar' waere falsch.
VPC_POINTCLOUD_TYPE = "eopc"

# Kachelname-Muster fuer die deterministische Bestimmung des Kachelursprungs
# (Offset), z.B. "2026_GUPPENFIRN_TIN_raw_2713_1206_LV95_LHN95.las" -> (2713, 1206).
# Der Ursprung wird bewusst aus dem NAMEN geparst, nicht aus dem Datenminimum -
# eine AOI-gecroppte Kachel faengt sonst irgendwo mitten in der Zelle an.
LN02_TILE_NAME_PATTERN = re.compile(
    r"(?:^|_)(\d{4})_(\d{4})_LV95_(?:LHN95|LN02)\.(?:las|laz)$", re.IGNORECASE)

# Thinning-Token im Quell-Dateinamen, z.B. "..._TIN_thinnedout04_raw_2713_1206_...".
# Ausgeduennt wird ausschliesslich im Tab [LHN95]; der Token gehoert damit zur Kachel und
# wird fuer die LN02-Benennung aus dem Quellnamen uebernommen, nicht neu erfragt.
LN02_THIN_TOKEN_PATTERN = re.compile(r"_(thinnedout\d+)_", re.IGNORECASE)

# Plausibilitaet der Kachelkoordinaten: Schweizer Landesgrenzen in km, LV95
LV95_EASTING_KM_RANGE  = (2480, 2840)
LV95_NORTHING_KM_RANGE = (1070, 1300)

# Byte-exakte VLR-Payloads aus der verifizierten swissSURFACE3D-Referenzkachel
# 2655_1272.laz (LV95/LN02, EPSG:2056 horizontal + EPSG:5728 vertikal).
# NICHT aus GeoTIFF-Keys/EPSG-Code neu berechnen (siehe _inject_reference_vlrs) -
# sondern unveraendert aus der Referenz uebernehmen.
REFERENCE_VLR_DESCRIPTION = "by LAStools of rapidlasso GmbH"

# VLRs, die einen RAUMBEZUG deklarieren. Alle davon muessen vor der Injektion raus -
# im Ziel darf es genau EINE Aussage zum CRS geben, naemlich die Referenz unten.
#
# 'LASF_Projection' ist laut LAS-Spezifikation fuer Projektionsangaben reserviert und
# wird komplett entfernt. 'liblas' ist der Grund, warum diese Liste ueberhaupt
# existiert: PDALs writers.las schreibt denselben WKT ein ZWEITES Mal unter
# user_id 'liblas', record_id 2112 ("OGR variant of OpenGIS WKT SRS") - an einer
# erzeugten Kachel nachgemessen. Dieser Zwilling steht in der Datei VOR der
# autoritativen Angabe und enthaelt nur den horizontalen Teil (LV95 ohne LN02). Ein
# Leser, der schlicht den ersten 2112er nimmt, bekaeme damit die falsche Aussage.
#
# 'laszip encoded' (22204) ist ausdruecklich NICHT betroffen - ohne diesen VLR laesst
# sich eine .laz nicht mehr dekomprimieren.
CRS_VLR_USER_IDS   = ("LASF_Projection", "liblas")
CRS_VLR_RECORD_IDS = (2111, 2112, 34735, 34736, 34737)


def _is_crs_vlr(user_id: str, record_id: int) -> bool:
    """True, wenn dieser VLR einen Raumbezug deklariert (siehe CRS_VLR_USER_IDS)."""
    if user_id == "LASF_Projection":
        return True
    return user_id in CRS_VLR_USER_IDS and record_id in CRS_VLR_RECORD_IDS
REFERENCE_VLR_34735_B64 = (
    "AQABAAAABQAABAAAAQABAAAMAAABAAgIBAwAAAEAKSMDEAAAAQApIwAQAAABAGAW"
)
REFERENCE_VLR_2112_B64 = (
    "Q09NUE9VTkRDUlNbIlByb2plY3RlZCBjb29yZGluYXRlIHN5c3RlbSB3aXRoIGVsZXZhdGlvbiIsUFJPSkNTWyJDSDE5MDMrIC8gTFY5"
    "NSIsR0VPR0NTWyJDSDE5MDMrIixEQVRVTVsiQ0gxOTAzKyIsU1BIRVJPSURbIkJlc3NlbCAxODQxIiw2Mzc3Mzk3LjE1NSwyOTkuMTUy"
    "ODEyOCxBVVRIT1JJVFlbIkVQU0ciLCI3MDA0Il1dLEFVVEhPUklUWVsiRVBTRyIsIjYxNTAiXV0sUFJJTUVNWyJHcmVlbndpY2giLDAs"
    "QVVUSE9SSVRZWyJFUFNHIiwiODkwMSJdXSxVTklUWyJkZWdyZWUiLDAuMDE3NDUzMjkyNTE5OTQzMyxBVVRIT1JJVFlbIkVQU0ciLCI5"
    "MTIyIl1dLEFVVEhPUklUWVsiRVBTRyIsIjQxNTAiXV0sUFJPSkVDVElPTlsiSG90aW5lX09ibGlxdWVfTWVyY2F0b3JfQXppbXV0aF9D"
    "ZW50ZXIiXSxQQVJBTUVURVJbImxhdGl0dWRlX29mX2NlbnRlciIsNDYuOTUyNDA1NTU1NTU1Nl0sUEFSQU1FVEVSWyJsb25naXR1ZGVf"
    "b2ZfY2VudGVyIiw3LjQzOTU4MzMzMzMzMzMzXSxQQVJBTUVURVJbImF6aW11dGgiLDkwXSxQQVJBTUVURVJbInJlY3RpZmllZF9ncmlk"
    "X2FuZ2xlIiw5MF0sUEFSQU1FVEVSWyJzY2FsZV9mYWN0b3IiLDFdLFBBUkFNRVRFUlsiZmFsc2VfZWFzdGluZyIsMjYwMDAwMF0sUEFS"
    "QU1FVEVSWyJmYWxzZV9ub3J0aGluZyIsMTIwMDAwMF0sVU5JVFsibWV0cmUiLDEsQVVUSE9SSVRZWyJFUFNHIiwiOTAwMSJdXSxBWElT"
    "WyJFYXN0aW5nIixFQVNUXSxBWElTWyJOb3J0aGluZyIsTk9SVEhdLEFVVEhPUklUWVsiRVBTRyIsIjIwNTYiXV0sVkVSVF9DU1siTE4w"
    "MiBoZWlnaHQiLFZFUlRfREFUVU1bIkxhbmRlc25pdmVsbGVtZW50IDE5MDIiLDIwMDUsQVVUSE9SSVRZWyJFUFNHIiwiNTEyNyJdXSxV"
    "TklUWyJtZXRyZSIsMSxBVVRIT1JJVFlbIkVQU0ciLCI5MDAxIl1dLEFYSVNbIkdyYXZpdHktcmVsYXRlZCBoZWlnaHQiLFVQXSxBVVRI"
    "T1JJVFlbIkVQU0ciLCI1NzI4Il1dXQA="
)


def _info(cfg: dict) -> None:
    """Liest Datei-Metadaten und gibt sie als JSON-Zeile auf stdout aus."""
    from osgeo import gdal
    gdal.UseExceptions()

    input_path = cfg["input_path"]
    ds = gdal.Open(input_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL konnte die Datei nicht oeffnen: {input_path}")

    bc  = ds.RasterCount
    rx  = ds.RasterXSize
    ry  = ds.RasterYSize
    gdt = ds.GetRasterBand(1).DataType
    dt  = gdal.GetDataTypeName(gdt)
    bits = gdal.GetDataTypeSize(gdt)
    srs = ds.GetSpatialRef()
    crs = srs.GetName() if srs else "nicht gesetzt"
    size = Path(input_path).stat().st_size / (1024 ** 2)

    ci_parts = []
    for i in range(1, bc + 1):
        band = ds.GetRasterBand(i)
        ci_parts.append(gdal.GetColorInterpretationName(band.GetColorInterpretation()))

    nd_raw = ds.GetRasterBand(1).GetNoDataValue()
    compression = ds.GetMetadataItem("COMPRESSION", "IMAGE_STRUCTURE") or "keine/unbekannt"
    blk_x, blk_y = ds.GetRasterBand(1).GetBlockSize()
    layout_str = f"Tiled TIFF ({blk_x}x{blk_y})" if blk_x < rx else "Striped TIFF"

    ds = None
    result = {
        "bands":       bc,
        "colorinterp": ci_parts,
        "width":       rx,
        "height":      ry,
        "dtype":       dt,
        "bitdepth":    bits,
        "crs":         crs,
        "size_mb":     round(size, 1),
        "nodata":      nd_raw,
        "compression": compression,
        "layout":      layout_str,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _tile_is_empty(ds) -> bool:
    """Prueft, ob eine Kachel keine verwertbaren Bildinformationen enthaelt
    (alle Baender bestehen aus genau einem konstanten Wert, i.d.R. reines NoData
    ausserhalb der gueltigen Flaeche bzw. ausserhalb des Befliegungsgebiets)."""
    for i in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(i)
        try:
            bmin, bmax = band.ComputeRasterMinMax(0)
        except RuntimeError:
            continue
        if bmin != bmax:
            return False
    return True


def _delete_tile_files(tif_path: str) -> None:
    p = Path(tif_path)
    for candidate in (p, p.with_suffix(".tfw"), Path(str(p) + ".aux.xml")):
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


# ─── Band-Ausgabe des TIFFconverters ───────────────────────────────────────────
# Die DMC-Ausgangsdaten sind praktisch immer 4-Band (RGBN: Rot, Gruen, Blau, NIR).
# Fuer die Publikation wird daraus je nach Produkt ein 3-Band-Auszug gebildet:
#   rgb  -> Quellbaender 1,2,3  (Echtfarbe)
#   nrg  -> Quellbaender 4,1,2  (Falschfarben-Infrarot, Standard-CIR-Reihenfolge)
#   keep -> keine Auswahl, alle Baender der Quelle bleiben erhalten (Default)
BAND_MODES = {
    "keep": None,
    "rgb":  [1, 2, 3],
    "nrg":  [4, 1, 2],
}
BAND_MODE_LABELS = {
    "keep": "4-Band (RGBN, unveraendert)",
    "rgb":  "RGBN -> RGB (3-Band, Echtfarbe)",
    "nrg":  "RGBN -> NRG (3-Band, Falschfarben-Infrarot)",
}


# --- Schritt 1: Mosaik-Quelle ermitteln (bestehendes VRT oder frisch bauen) ---

def _resolve_mosaic_source(input_dir: str, staging_run_dir: Path, log) -> str:
    from osgeo import gdal

    existing_vrt = sorted(glob.glob(os.path.join(input_dir, "*.vrt")))
    if existing_vrt:
        log(f"Verwende vorhandenes Mosaik-VRT: {existing_vrt[0]}")
        return existing_vrt[0]

    tiles = sorted(
        {p for pat in ("*.tif", "*.tiff") for p in glob.glob(os.path.join(input_dir, pat))}
    )
    if not tiles:
        raise FileNotFoundError(f"Keine .tif/.tiff Kacheln und kein .vrt gefunden in: {input_dir}")

    vrt_path = staging_run_dir / "01_input_mosaic.vrt"
    log(f"Kein VRT im Input-Ordner gefunden - baue neues Mosaik-VRT aus {len(tiles)} Kachel(n): {vrt_path}")
    vrt_ds = gdal.BuildVRT(str(vrt_path), tiles)
    if vrt_ds is None:
        raise RuntimeError("gdal.BuildVRT hat None zurueckgegeben - VRT-Erstellung fehlgeschlagen.")
    vrt_ds.FlushCache()
    vrt_ds = None
    return str(vrt_path)


# --- Schritt 1b: Band-Auswahl (nur bei 4-Band-Input RGBN) ---

def _select_bands(mosaic_src: str, band_mode: str, staging_run_dir: Path, log) -> str:
    """Reduziert eine 4-Band-Quelle (RGBN) per VRT auf drei Baender.

    'rgb' -> Quellbaender 1,2,3 (echtfarbig)
    'nrg' -> Quellbaender 4,1,2 (Falschfarben-Infrarot: NIR/Rot/Gruen)
    'keep' -> unveraendert (Rueckgabe der Original-Quelle)

    Der Auszug passiert bewusst VOR dem Cutline-Clip: der Warp-Schritt und alle
    Kachel-Schreibvorgaenge arbeiten dadurch auf 3 statt 4 Baendern (rund ein
    Viertel weniger I/O). Ein VRT ist dafuer kostenlos - es kopiert keine Pixel.
    Die Farbinterpretation wird im VRT explizit auf Rot/Gruen/Blau gesetzt, damit
    das Band 4 der Quelle (haeufig als 'Alpha' oder 'Undefined' getaggt) im
    NRG-Auszug nicht als Transparenzkanal missverstanden wird.
    """
    from osgeo import gdal

    if band_mode not in BAND_MODES:
        raise ValueError(f"Unbekannte Band-Ausgabe: {band_mode!r} "
                         f"(erlaubt: {', '.join(sorted(BAND_MODES))})")
    band_list = BAND_MODES[band_mode]
    if band_list is None:
        log("\nBand-Ausgabe        : 4-Band unveraendert (keine Bandauswahl)")
        return mosaic_src

    src_ds = gdal.Open(mosaic_src, gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"Konnte Mosaik-Quelle fuer die Bandauswahl nicht oeffnen: {mosaic_src}")
    band_count = src_ds.RasterCount
    src_ds = None

    if band_count < 4:
        raise RuntimeError(
            f"Band-Ausgabe '{BAND_MODE_LABELS[band_mode]}' verlangt einen 4-Band-Input (RGBN), "
            f"die Quelle hat aber {band_count} Band/Baender.\n"
            f"Bitte im Tab 'DMC - TIFFconverter' die Band-Ausgabe auf "
            f"'{BAND_MODE_LABELS['keep']}' stellen."
        )
    if band_count > 4:
        log(f"  WARNUNG          : Quelle hat {band_count} Baender - "
            f"Baender 1-4 werden als R,G,B,N interpretiert.")

    vrt_path = staging_run_dir / f"01b_bands_{band_mode}.vrt"
    log(f"\nBand-Ausgabe        : {BAND_MODE_LABELS[band_mode]}")
    log(f"  Quellbaender     : {' '.join(str(b) for b in band_list)} von {band_count}")
    log(f"  Band-VRT         : {vrt_path}")

    vrt_ds = gdal.Translate(str(vrt_path), mosaic_src,
                            options=gdal.TranslateOptions(format="VRT", bandList=band_list))
    if vrt_ds is None:
        raise RuntimeError("gdal.Translate hat None zurueckgegeben - Bandauswahl fehlgeschlagen.")
    try:
        for idx, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand), start=1):
            vrt_ds.GetRasterBand(idx).SetColorInterpretation(ci)
    except Exception as e:
        log(f"  WARNUNG          : ColorInterp im Band-VRT nicht setzbar ({e}) - "
            f"die Ausgabe wird ueber PHOTOMETRIC=RGB dennoch korrekt getaggt.")
    vrt_ds.FlushCache()
    vrt_ds = None
    return str(vrt_path)


def _detect_source_compression(input_dir: str, log) -> str:
    """Liest die Kompression der ersten gefundenen Quellkachel und waehlt daraus
    einen verlustfreien COMPRESS-Wert fuer die Ausgabe (nie JPEG/verlustbehaftet -
    die Ausgabe soll nie schlechter sein als der Input, auch wenn dieser bereits
    verlustbehaftet komprimiert war)."""
    from osgeo import gdal

    tiles = sorted(
        {p for pat in ("*.tif", "*.tiff") for p in glob.glob(os.path.join(input_dir, pat))}
    )
    if not tiles:
        return "NONE"

    ds = gdal.Open(tiles[0], gdal.GA_ReadOnly)
    if ds is None:
        return "NONE"
    raw = (ds.GetMetadataItem("COMPRESSION", "IMAGE_STRUCTURE") or "").upper()
    ds = None

    lossless = {"LZW", "DEFLATE", "ZSTD", "PACKBITS"}
    if raw in lossless:
        compress = raw
    elif raw in ("", "NONE"):
        compress = "NONE"
    else:
        compress = "LZW"  # z.B. JPEG oder unbekannt - nie verlustbehaftet uebernehmen

    log(f"Kompression Input-Kacheln : {raw or 'keine'}  ->  Output-Kompression: {compress}")
    return compress


def _check_pixel_alignment(path: str, log) -> tuple:
    """Liest Pixelgroesse + Ursprung des Mosaiks und warnt, falls der Ursprung
    nicht auf ein sauberes Vielfaches der Pixelgroesse faellt (dann wuerden
    spaetere Fenster-Ausschnitte - z.B. auf das 1km-Grid - nicht exakt auf
    bestehende Pixelkanten treffen, sondern leicht versetzt gerundet)."""
    from osgeo import gdal

    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Konnte Mosaik nicht oeffnen fuer Pixel-Check: {path}")
    gt = ds.GetGeoTransform()
    ds = None
    px_w, px_h = gt[1], abs(gt[5])

    def _rel_offset(origin: float, size: float) -> float:
        if size <= 0:
            return 0.0
        rem = origin % size
        return min(rem, size - rem)

    off_x = _rel_offset(gt[0], px_w)
    off_y = _rel_offset(gt[3], px_h)
    tol = 0.001  # 1mm Toleranz fuer Rundung/Fliesskomma
    if off_x > tol or off_y > tol:
        log(f"  WARNUNG: Pixelursprung des Mosaiks liegt nicht exakt auf einem "
            f"Vielfachen der Pixelgroesse ({px_w:g} x {px_h:g} m) - Versatz "
            f"X={off_x:.4f}m, Y={off_y:.4f}m. 1km-Grid-Kacheln koennten dadurch "
            f"minimal (< 1 Pixel) vom exakten Kilometer-Raster abweichen.")
    else:
        log(f"  Pixelraster-Check OK: Ursprung faellt exakt auf ein Vielfaches "
            f"der Pixelgroesse ({px_w:g} x {px_h:g} m).")
    return px_w, px_h


# --- Schritt 2: Cutline-Clip auf die gueltige Flaeche ---

def _clip_to_valid_area(mosaic_src: str, clip_shape_path: str, staged_path: Path,
                         nodata_val: float, px_w: float, px_h: float,
                         num_threads: str, log, progress) -> None:
    from osgeo import gdal

    log(f"\nClippe Mosaik auf gueltige Flaeche (Cutline): {clip_shape_path}")
    log(f"  Ausserhalb des Shapes -> NoData = {nodata_val:g}  (alle Baender)")
    warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=clip_shape_path,
        cropToCutline=False,
        xRes=px_w, yRes=px_h,  # Quell-Pixelraster exakt beibehalten (kein implizites Resampling)
        srcNodata=nodata_val,
        dstNodata=nodata_val,
        multithread=True,
        warpOptions=[f"NUM_THREADS={num_threads}"],
        creationOptions=[
            "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
            "COMPRESS=LZW", "PREDICTOR=2", "BIGTIFF=YES",
        ],
        callback=progress,
    )
    out_ds = gdal.Warp(str(staged_path), mosaic_src, options=warp_options)
    if out_ds is None:
        raise RuntimeError("gdal.Warp hat None zurueckgegeben - Clip fehlgeschlagen.")
    out_ds.FlushCache()
    out_ds = None
    log(f"  Zwischenraster (geclippt): {staged_path}")


# --- Schritt 3: Zuschnitt auf 1km-Grid (parallelisiert) ---

def _grid_tile_worker(args) -> tuple:
    """Wird in einem eigenen Prozess ausgefuehrt (ProcessPoolExecutor) - oeffnet das
    geclippte Zwischenraster read-only und schreibt genau eine Grid-Kachel."""
    (staged_path, minx, maxy, maxx, miny, out_path,
     compress, blocksize, nodata_val, photometric) = args
    from osgeo import gdal
    gdal.UseExceptions()

    src_ds = gdal.Open(staged_path, gdal.GA_ReadOnly)
    if src_ds is None:
        return ("error", out_path, f"Konnte Zwischenraster nicht oeffnen: {staged_path}")

    creation_options = [
        "TILED=YES", f"BLOCKXSIZE={blocksize}", f"BLOCKYSIZE={blocksize}",
        f"COMPRESS={compress}", "TFW=YES",
    ]
    if compress in ("LZW", "DEFLATE", "ZSTD"):
        creation_options.append("PREDICTOR=2")
    if photometric:
        # Nur bei aktiver Bandauswahl gesetzt: erzwingt Rot/Gruen/Blau statt einer
        # aus der Quelle geerbten Interpretation (Band 4 eines RGBN-TIFF ist
        # haeufig als 'Alpha' getaggt und wuerde sonst als Transparenz wandern).
        creation_options.append(f"PHOTOMETRIC={photometric}")

    try:
        translate_options = gdal.TranslateOptions(
            format="GTiff",
            outputSRS="EPSG:2056",
            projWin=[minx, maxy, maxx, miny],
            noData=nodata_val,
            creationOptions=creation_options,
        )
        out_ds = gdal.Translate(out_path, src_ds, options=translate_options)
        if out_ds is None:
            return ("error", out_path, "gdal.Translate hat None zurueckgegeben")
        out_ds.FlushCache()

        if _tile_is_empty(out_ds):
            out_ds = None
            src_ds = None
            _delete_tile_files(out_path)
            return ("empty", out_path, None)

        out_ds = None
        src_ds = None
        return ("written", out_path, None)
    except Exception as e:
        src_ds = None
        return ("error", out_path, str(e))


def _process(cfg: dict) -> None:
    from osgeo import gdal, ogr, osr

    jahr             = str(cfg["jahr"]).strip()
    area             = str(cfg["area"]).strip()
    gsd              = str(cfg["gsd"]).strip()
    input_dir        = cfg["input_dir"]
    output_dir       = cfg["output_dir"]
    clip_shape_path  = cfg["clip_shape_path"]
    grid_shape_path  = cfg["grid_shape_path"]
    staging_dir      = cfg["staging_dir"]
    num_workers      = int(cfg.get("num_workers", 6))
    blocksize        = cfg.get("blocksize", "256")
    nodata_val       = float(cfg.get("nodata", "0"))
    keep_staging     = bool(cfg.get("keep_staging", False))
    band_mode        = str(cfg.get("band_mode", "keep")).strip().lower() or "keep"

    def _log(msg: str) -> None:
        print(msg, flush=True)

    if band_mode not in BAND_MODES:
        raise ValueError(f"Unbekannte Band-Ausgabe: {band_mode!r} "
                         f"(erlaubt: {', '.join(sorted(BAND_MODES))})")

    gdal.UseExceptions()
    ogr.UseExceptions()

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    run_dir = Path(staging_dir) / f"{area}_{jahr}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Staging-Ordner: {run_dir}")

    last_emit = {"t": 0.0, "p": -1.0}

    def _progress(complete, message, unknown=None):
        try:
            if complete is None:
                return 1
            pct = float(complete)
            now = time.time()
            if (now - last_emit["t"]) >= 1.0 or (pct - last_emit["p"]) >= 0.005:
                print(f"PROGRESS:{pct:.6f}", flush=True)
                last_emit["t"] = now
                last_emit["p"] = pct
        except Exception:
            pass
        return 1

    # --- Schritt 1: Mosaik-Quelle + Kompression von den Input-Kacheln uebernehmen ---
    compress = _detect_source_compression(input_dir, _log)
    mosaic_src = _resolve_mosaic_source(input_dir, run_dir, _log)
    mosaic_src = _select_bands(mosaic_src, band_mode, run_dir, _log)
    px_w, px_h = _check_pixel_alignment(mosaic_src, _log)

    # --- Schritt 2: Cutline-Clip ---
    staged_path = run_dir / "02_clipped_mosaic.tif"
    _clip_to_valid_area(mosaic_src, clip_shape_path, staged_path, nodata_val,
                         px_w, px_h, str(num_workers), _log, _progress)

    # --- Schritt 3: Grid vorbereiten ---
    _log(f"\nOeffne Grid-Shape: {grid_shape_path}")
    shp_ds = ogr.Open(grid_shape_path, 0)
    if shp_ds is None:
        raise FileNotFoundError(f"OGR konnte das Grid-Shape nicht oeffnen: {grid_shape_path}")
    layer = shp_ds.GetLayer()

    name_field = "NAME"
    field_idx = layer.GetLayerDefn().GetFieldIndex(name_field)
    if field_idx < 0:
        fields = [layer.GetLayerDefn().GetFieldDefn(i).GetName()
                  for i in range(layer.GetLayerDefn().GetFieldCount())]
        raise ValueError(f"Grid-Shape enthaelt kein Feld '{name_field}' - vorhandene Felder: {fields}")

    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(2056)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    src_layer_srs = layer.GetSpatialRef()
    transform = None
    if src_layer_srs is None:
        _log("  WARNUNG        : Grid-Shape hat kein Koordinatensystem gesetzt - wird als EPSG:2056 angenommen.")
    elif not src_layer_srs.IsSame(target_srs):
        src_layer_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(src_layer_srs, target_srs)
        _log(f"  Grid-Shape CRS : {src_layer_srs.GetName()} -> wird nach EPSG:2056 reprojiziert")
    else:
        _log("  Grid-Shape CRS : EPSG:2056 (passend)")

    clipped_ds = gdal.Open(str(staged_path), gdal.GA_ReadOnly)
    gt = clipped_ds.GetGeoTransform()
    rx, ry = clipped_ds.RasterXSize, clipped_ds.RasterYSize
    src_minx = gt[0]
    src_maxx = gt[0] + rx * gt[1]
    src_maxy = gt[3]
    src_miny = gt[3] + ry * gt[5]
    clipped_ds = None

    if transform is not None:
        inv_transform = osr.CoordinateTransformation(target_srs, src_layer_srs)
        xs, ys = [], []
        for cx, cy in ((src_minx, src_miny), (src_minx, src_maxy),
                       (src_maxx, src_miny), (src_maxx, src_maxy)):
            px, py, _ = inv_transform.TransformPoint(cx, cy)
            xs.append(px)
            ys.append(py)
        layer.SetSpatialFilterRect(min(xs), min(ys), max(xs), max(ys))
    else:
        layer.SetSpatialFilterRect(src_minx, src_miny, src_maxx, src_maxy)

    layer.ResetReading()
    total = layer.GetFeatureCount()
    _log(f"\nGefundene Grid-Kacheln (ueberlappend mit geclipptem Mosaik): {total}")
    _log(f"Ausgabe-Benennung   : {jahr}_{area}_DOP_{gsd}_<NAME>_LV95.tif")
    _log(f"Band-Ausgabe        : {BAND_MODE_LABELS[band_mode]}")
    _log(f"Kompression         : {compress} (von Input-Kacheln uebernommen, verlustfrei)")
    _log(f"Blockgroesse        : {blocksize}")
    _log(f"Parallele Prozesse  : {num_workers}")

    # PHOTOMETRIC nur bei aktiver Bandauswahl erzwingen - ohne Auswahl bleibt die
    # Ausgabe exakt so getaggt wie die Quelle.
    photometric = "RGB" if BAND_MODES[band_mode] else None

    jobs = []
    skipped = 0
    for i, feature in enumerate(layer, 1):
        name_val = feature.GetField(name_field)
        if name_val is None or str(name_val).strip() == "":
            skipped += 1
            continue
        tile_name = f"{jahr}_{area}_DOP_{gsd}_{str(name_val).strip()}_LV95.tif"

        geom = feature.GetGeometryRef()
        if geom is None:
            skipped += 1
            continue
        geom = geom.Clone()
        if transform is not None:
            geom.Transform(transform)

        minx, maxx, miny, maxy = geom.GetEnvelope()
        if maxx <= src_minx or minx >= src_maxx or maxy <= src_miny or miny >= src_maxy:
            skipped += 1
            continue

        out_path = str(Path(output_dir) / tile_name)
        jobs.append((str(staged_path), minx, maxy, maxx, miny, out_path,
                     compress, blocksize, nodata_val, photometric))

    shp_ds = None

    if not jobs:
        raise RuntimeError(
            "Keine Grid-Kachel ueberlappt das geclippte Mosaik - Grid-Shape/Clip-Shape und Extent pruefen."
        )

    _log(f"\nStarte parallele Verarbeitung: {len(jobs)} Kachel(n) auf {num_workers} Prozess(en)\n")

    written = 0
    empty_deleted = 0
    errors = 0
    done = 0
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_grid_tile_worker, job): job for job in jobs}
        for future in as_completed(futures):
            done += 1
            status, out_path, err = future.result()
            tile_name = Path(out_path).name
            if status == "written":
                written += 1
                _log(f"  [{done}/{len(jobs)}] {tile_name}")
            elif status == "empty":
                empty_deleted += 1
                _log(f"  [{done}/{len(jobs)}] {tile_name} - GELOESCHT (100% NoData)")
            else:
                errors += 1
                _log(f"  [{done}/{len(jobs)}] FEHLER bei {tile_name}: {err}")
            print(f"PROGRESS:{done/len(jobs):.6f}", flush=True)

    if not keep_staging:
        _log(f"\nRaeume Staging-Ordner auf: {run_dir}")
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass
    else:
        _log(f"\nStaging-Dateien bleiben erhalten: {run_dir}")

    _log(f"\nFertig. {written} Kachel(n) geschrieben, {skipped} uebersprungen, "
         f"{empty_deleted} leere Kachel(n) geloescht, {errors} Fehler.")
    if written == 0:
        raise RuntimeError("Keine Kachel wurde geschrieben.")
    if errors:
        raise RuntimeError(f"{errors} Kachel(n) konnten nicht geschrieben werden - siehe Log.")


# ─── DMC LASconverter (PDAL-basiert) ───────────────────────────────────────────
#
# Ablauf:
#   1) Metadaten (Bounding Box) aller Input-.laz/.las-Kacheln parallel einlesen
#      (pdal info --metadata, headerbasiert, kein Decompress der Punktdaten)
#   2) Punktwolken-Kacheln (pro 1km-Grid-Kachel):
#      pro Grid-Zelle die ueberlappenden Input-Kacheln mergen, per AOI-Polygon
#      croppen, optional thinnen, als .las oder .laz schreiben (out_format,
#      Default .las - wird u.a. fuer GeoSuite-Reframe LHN95->LN02 benoetigt)
#   3) DSM-Zellen (nur falls "Create Raster" aktiv), ebenfalls pro 1km-Grid-Kachel:
#      gleiche Zelle, aber mit Puffer croppen (vollstaendige IDW-Nachbarschaft am
#      Zellrand), optional thinnen und als Float32-Raster rastern (PDAL
#      writers.gdal, IDW), Pixelursprung auf ein GSD-Vielfaches gesnappt
#      Schritt 2) und 3) laufen als EIN gemeinsamer Job-Pool ueber mehrere
#      Prozesse - bewusst zellweise statt als ein Gesamt-Merge, der bei grossen
#      Projekten den Arbeitsspeicher sprengt (pdal.exe-Absturz, Code 0xC0000409)
#   4) Gesamt-Raster: Zell-Raster als VRT mosaikieren, per AOI-Shape maskieren
#      (gdal.Warp Cutline, NoData ausserhalb), daraus den Hillshade rechnen und
#      ebenfalls maskieren (NoData=255 nur ausserhalb des AOI)
#
# Hoehensystem: Input-Kacheln sind LHN95, Output bleibt LHN95 (kein Reframe
# nach LN02 - swisstopo selbst beschreibt diese Transformation als Naeherung
# ohne exakte Loesung; falls spaeter benoetigt, separat/extern klaeren).


def _pdal_info_metadata(pdal_exe: str, path: str) -> dict:
    result = subprocess.run([pdal_exe, "info", "--metadata", path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"pdal info beendet mit Exit-Code {result.returncode}"
                            + (f": {msg}" if msg else " (kein stdout/stderr - moeglicher Absturz)"))
    return json.loads(result.stdout)["metadata"]


def _pdal_dimension_names(pdal_exe: str, path: str) -> set:
    """Namen aller Dimensionen einer Punktwolken-Datei ('pdal info --schema')."""
    result = subprocess.run([pdal_exe, "info", "--schema", path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"pdal info --schema beendet mit Exit-Code "
                            f"{result.returncode}" + (f": {msg}" if msg else ""))
    schema = json.loads(result.stdout).get("schema", {}) or {}
    return {d.get("name") for d in (schema.get("dimensions") or [])}


def _tile_bbox_worker(args) -> tuple:
    """Bounding Box UND global_encoding einer Quell-Kachel (headerbasiert).

    Das global_encoding wird mitgelesen, weil Bit 0 (GPS-Time-Typ) eine Eigenschaft der
    DATEN ist: es sagt, wie die GpsTime-Werte zu lesen sind (0 = GPS Week Time,
    1 = Adjusted Standard GPS Time). Ohne Uebernahme ginge die Angabe beim Schreiben der
    Zwischen-Tiles verloren (PDAL-Default 0), und im Tab [LN02] liesse sich nicht mehr
    feststellen, was die Quelle deklariert hatte."""
    pdal_exe, path = args
    try:
        meta = _pdal_info_metadata(pdal_exe, path)
        return (path, meta["minx"], meta["miny"], meta["maxx"], meta["maxy"],
                int(meta.get("global_encoding", 0) or 0), None)
    except Exception as e:
        return (path, None, None, None, None, None, str(e))


def _run_pdal_pipeline(pdal_exe: str, pipeline_path: Path, metadata_path=None):
    """Fuehrt eine PDAL-Pipeline aus.

    Mit metadata_path wird zusaetzlich '--metadata <pfad>' uebergeben und die
    geparste Pipeline-Metadata als Dict zurueckgegeben (sonst None). Damit lassen
    sich Ergebnisse einer angehaengten 'filters.stats'-Stage auslesen, OHNE die
    Datei ein zweites Mal komplett einzulesen."""
    cmd = [pdal_exe, "pipeline", str(pipeline_path)]
    if metadata_path is not None:
        cmd += ["--metadata", str(metadata_path)]
    result = subprocess.run(cmd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        rc = result.returncode
        rc_hex = f" (0x{rc & 0xFFFFFFFF:08X})" if rc < 0 else ""
        hint = ""
        if not msg:
            hint = (" - kein stdout/stderr trotz Fehlercode: deutet auf einen abrupten "
                    "Prozessabsturz hin (z.B. zu wenig RAM bei vielen parallelen "
                    "pdal.exe-Prozessen), nicht auf einen regulaeren PDAL-Fehler.")
        raise RuntimeError(f"pdal pipeline beendet mit Exit-Code {rc}{rc_hex}{hint}"
                            + (f": {msg}" if msg else ""))
    if metadata_path is None:
        return None
    try:
        with open(str(metadata_path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _discard_partial(path: str) -> None:
    """Loescht eine angefangene Ausgabedatei nach einem Fehler.

    Ein abgestuerzter pdal.exe (z.B. Speichermangel) hinterlaesst sonst eine
    abgeschnittene Datei mit unbrauchbarem Header, die im Output-Ordner nicht von
    einer vollstaendigen Kachel zu unterscheiden ist."""
    try:
        os.remove(path)
    except OSError:
        pass


def _las_cell_worker(args) -> tuple:
    """Wird in einem eigenen Prozess ausgefuehrt - mergt die Input-Kacheln einer
    1km-Grid-Zelle, croppt/thinnt optional, schreibt eine Punktwolken-Kachel
    (.las oder .laz, siehe out_format) in LAS 1.4 / PF7.

    Der Writer bekommt scale UND offset explizit (Offset = Kachelursprung, aus dem
    Dateinamen geparst wie im Tab [LN02]). Damit liegt die Zwischenstufe schon auf
    demselben Ganzzahl-Gitter wie das spaetere GDWH-Produkt - die Requantisierung im
    Tab [LN02] ist dann eine Identitaet und verschiebt keine Koordinaten."""
    (job, run_dir_str, output_dir_laz, pdal_exe, clip_wkt, thin_m, out_format,
     gps_time_bit) = args

    stem = job["stem"]
    cminx, cminy, cmaxx, cmaxy = job["cell_bounds"]
    tiles = job["tiles"]

    run_dir = Path(run_dir_str)
    pipeline_path = run_dir / f"pipeline_{stem}.json"
    laz_out = str(Path(output_dir_laz) / f"{stem}.{out_format}")

    origin_e_km, origin_n_km = _parse_tile_origin(f"{stem}.{out_format}")
    origin_x, origin_y = origin_e_km * 1000.0, origin_n_km * 1000.0

    stages = []
    tags = []
    for i, t in enumerate(tiles):
        tag = f"r{i}"
        stages.append({"type": "readers.las", "filename": t, "tag": tag,
                        "override_srs": LAS_INPUT_SRS})
        tags.append(tag)
    stages.append({"type": "filters.merge", "inputs": tags})

    bounds_str = f"([{cminx:.3f},{cmaxx:.3f}],[{cminy:.3f},{cmaxy:.3f}])"
    stages.append({"type": "filters.crop", "bounds": bounds_str})
    stages.append({"type": "filters.crop", "polygon": clip_wkt})

    if thin_m:
        stages.append({"type": "filters.sample", "radius": float(thin_m)})

    stages.append({"type": "writers.las", "filename": laz_out,
                    "minor_version": LAS_OUT_MINOR_VERSION,
                    "dataformat_id": LAS_OUT_POINT_FORMAT,
                    "a_srs": LAS_OUT_SRS,
                    # Bit 0 (GPS-Time-Typ) aus der Quelle uebernommen, Bit 4 (WKT)
                    # gesetzt: LAS 1.4 mit PF >= 6 verlangt die WKT-Variante des
                    # CRS-Tags (LAS 1.4 R15, Kap. 2.1), und PDAL schreibt dafuer
                    # ohnehin OGC-WKT-VLRs statt GeoTIFF-Keys.
                    "global_encoding": 0x10 | int(gps_time_bit),
                    "scale_x": 0.01, "scale_y": 0.01, "scale_z": 0.01,
                    "offset_x": origin_x, "offset_y": origin_y, "offset_z": 0})

    try:
        with open(pipeline_path, "w", encoding="utf-8") as f:
            json.dump({"pipeline": stages}, f)
        _run_pdal_pipeline(pdal_exe, pipeline_path)

        if not os.path.isfile(laz_out):
            return ("empty", stem, None)

        meta = _pdal_info_metadata(pdal_exe, laz_out)
        if int(meta.get("count", 0)) == 0:
            _discard_partial(laz_out)
            return ("empty", stem, None)

        # Kontrolle statt Annahme: die Metadaten sind hier ohnehin schon gelesen.
        # Stimmt der Header nicht, ist die Kachel fuer den GeoSuite-Reframe unbrauchbar
        # und soll gar nicht erst im Output-Ordner liegen bleiben.
        if (meta.get("minor_version") != LAS_OUT_MINOR_VERSION or
                meta.get("dataformat_id") != LAS_OUT_POINT_FORMAT):
            _discard_partial(laz_out)
            return ("error", stem,
                    f"Header ist LAS 1.{meta.get('minor_version')}/"
                    f"PF{meta.get('dataformat_id')}, erwartet LAS "
                    f"1.{LAS_OUT_MINOR_VERSION}/PF{LAS_OUT_POINT_FORMAT} - ohne PF7 "
                    f"fehlt der Kachel das Farbfeld.")

        return ("written", stem, None)
    except Exception as e:
        _discard_partial(laz_out)
        return ("error", stem, str(e))
    finally:
        try:
            pipeline_path.unlink(missing_ok=True)
        except Exception:
            pass


def _raster_cell_buffer(gsd: float, thin_m) -> float:
    """Puffer um eine DSM-Zelle, damit die IDW-Nachbarschaft am Zellrand vollstaendig
    ist (writers.gdal-Default-Radius = resolution * sqrt(2)) bzw. das Thinning
    randunabhaengig bleibt. Wird sowohl beim Crop im Worker als auch beim Auswaehlen
    der beitragenden Input-Kacheln gebraucht - deshalb nur EINE Definition."""
    return max(3.0 * float(gsd), 5.0 * float(thin_m) if thin_m else 0.0, 2.0)


def _raster_cell_worker(args) -> tuple:
    """Rastert EINE 1km-Grid-Zelle (PDAL, IDW) - Gegenstueck zu _las_cell_worker.

    Bewusst KEIN einzelner Gesamt-Merge ueber alle Input-Kacheln: filters.merge
    (und erst recht filters.sample mit seinem KD-Baum) haelt die komplette
    Punktwolke im Arbeitsspeicher. Bei grossen Projekten (>1000 Input-Kacheln)
    fuehrt das zum harten Absturz von pdal.exe (Windows-Exitcode 0xC0000409 =
    Fail-Fast/abort, typischerweise aus einem bad_alloc). Zellweise bleibt der
    Speicherbedarf begrenzt (nur die Kacheln EINER Zelle) und die Arbeit laesst
    sich ueber alle Kerne verteilen.

    Der Crop erfolgt mit einem Puffer um die Zelle, damit die IDW-Nachbarschaft
    an den Zellraendern vollstaendig ist; geschrieben wird exakt der auf das GSD
    gesnappte Zellausschnitt, sodass sich die Zell-Raster luecken- und
    ueberlappungsfrei zu einem Mosaik zusammensetzen."""
    (job, run_dir_str, cells_dir, pdal_exe, thin_m, gsd) = args

    cell = job["cell"]
    r_minx, r_miny, r_maxx, r_maxy = job["raster_bounds"]
    tiles = job["tiles"]

    run_dir = Path(run_dir_str)
    pipeline_path = run_dir / f"pipeline_dsm_{cell}.json"
    tif_out = str(Path(cells_dir) / f"dsm_{cell}.tif")

    buf = _raster_cell_buffer(gsd, thin_m)
    # SRS der Input-Kacheln: LHN95 (Tab "LASconverter [LHN95]") bzw. LN02, wenn der
    # Job-Aufbau es explizit setzt (Tab "LASconverter [LN02]").
    srs = job.get("srs", LAS_INPUT_SRS)

    stages = []
    tags = []
    for i, t in enumerate(tiles):
        tag = f"r{i}"
        stages.append({"type": "readers.las", "filename": t, "tag": tag,
                        "override_srs": srs})
        tags.append(tag)
    stages.append({"type": "filters.merge", "inputs": tags})
    stages.append({"type": "filters.crop",
                    "bounds": f"([{r_minx - buf:.3f},{r_maxx + buf:.3f}],"
                              f"[{r_miny - buf:.3f},{r_maxy + buf:.3f}])"})

    if thin_m:
        stages.append({"type": "filters.sample", "radius": float(thin_m)})

    stages.append({
        "type": "writers.gdal",
        "filename": tif_out,
        "resolution": float(gsd),
        "output_type": "idw",
        "gdaldriver": "GTiff",
        "data_type": "float32",
        "bounds": f"([{r_minx:.3f},{r_maxx:.3f}],[{r_miny:.3f},{r_maxy:.3f}])",
        "nodata": LAS_CELL_NODATA,
    })

    try:
        with open(pipeline_path, "w", encoding="utf-8") as f:
            json.dump({"pipeline": stages}, f)
        _run_pdal_pipeline(pdal_exe, pipeline_path)

        if not os.path.isfile(tif_out):
            return ("empty", cell, None)
        return ("written", cell, None)
    except Exception as e:
        _discard_partial(tif_out)
        return ("error", cell, str(e))
    finally:
        try:
            pipeline_path.unlink(missing_ok=True)
        except Exception:
            pass


def _fill_raster_nodata(vrt_path: Path, run_dir: Path, gsd: float, num_threads: str,
                         log) -> tuple:
    """Interpoliert die NoData-Loecher des DSM-Mosaiks und liefert ZWEI Varianten:

      (dsm_pfad, hillshade_pfad)

    - dsm_pfad: nur die KLEINEN Loecher sind interpoliert, die grossen stehen wieder
      als echtes NoData drin. Das ist das auszuliefernde Hoehenmodell - dort bleibt
      erkennbar, wo die Autokorrelation nichts messen konnte.
    - hillshade_pfad: ALLE erreichbaren Loecher sind gefuellt. Der Hillshade ist ein
      reines Visualisierungsprodukt; aus dieser Variante gerechnet bekommen die
      Felswaende eine plausible Schattierung statt einer weissen Flaeche, ohne dass
      das ausgelieferte DSM seine ehrlichen Luecken verliert.

    Ablauf:
      1. VRT materialisieren (gdal.FillNodata braucht ein beschreibbares Band)
      2. Maske aller NoData-Loecher sichern - VOR dem Fuellen
      3. gdal.SieveFilter entfernt aus dieser Maske alle Loecher unterhalb der
         Flaechenschwelle; uebrig bleiben die GROSSEN Loecher
      4. gdal.FillNodata fuellt zunaechst alles (IDW aus den naechstgelegenen
         gueltigen Nachbarn je Quadrant)
      5. dieser Stand wird als Hillshade-Quelle weggeschrieben
      6. im DSM werden die grossen Loecher wieder auf NoData gesetzt

    Schritt 4 vor 6 und nicht umgekehrt: gdal.FillNodata kennt keine Moeglichkeit,
    Pixel gleichzeitig ungefuellt zu lassen UND von der Interpolation auszunehmen -
    die grossen Loecher wuerden sonst mit ihrem NoData-Wert in die Nachbarschaft der
    kleinen einfliessen.

    Gefuellt wird VOR dem AOI-Clip: danach ist ausserhalb des AOI ebenfalls NoData und
    die Interpolation liesse sich nicht mehr aufs Innere beschraenken. Die Flaeche
    ausserhalb der Daten ist ein einziges riesiges Loch, liegt damit weit ueber der
    Schwelle und bleibt unangetastet."""
    from osgeo import gdal
    import numpy as np

    filled_path = run_dir / "04_raster_dsm.tif"          # kleine Loecher gefuellt
    hs_src_path = run_dir / "04_raster_filled_all.tif"   # alle Loecher gefuellt
    mask_path   = run_dir / "04_holes_mask.tif"
    sieve_path  = run_dir / "04_holes_large.tif"

    max_hole_px = max(1, int(round(LAS_FILL_MAX_HOLE_AREA_M2 / (gsd * gsd))))
    log("")
    log(f"Fuelle kleine NoData-Loecher im Mosaik (vor dem AOI-Clip): {filled_path}")
    log(f"  Schwelle: {LAS_FILL_MAX_HOLE_AREA_M2:g} m2 = {max_hole_px} Pixel bei "
        f"{gsd:g} m GSD - groessere Loecher bleiben echtes NoData")

    base_co = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
               "COMPRESS=LZW", "BIGTIFF=YES", f"NUM_THREADS={num_threads}"]

    trans_ds = gdal.Translate(
        str(filled_path), str(vrt_path),
        options=gdal.TranslateOptions(format="GTiff", noData=LAS_CELL_NODATA,
                                       creationOptions=base_co + ["PREDICTOR=3"]),
    )
    if trans_ds is None:
        raise RuntimeError("gdal.Translate hat None zurueckgegeben - Mosaik nicht "
                            "materialisierbar, NoData-Fuellung nicht moeglich.")
    trans_ds.FlushCache()
    trans_ds = None

    ds = mask_ds = sieve_ds = None
    try:
        ds = gdal.Open(str(filled_path), gdal.GA_Update)
        if ds is None:
            raise RuntimeError(f"Mosaik nicht zum Schreiben zu oeffnen: {filled_path}")
        band = ds.GetRasterBand(1)
        xs, ys = band.XSize, band.YSize
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, xs * 4))

        def _byte_raster(path):
            out = gdal.GetDriverByName("GTiff").Create(
                str(path), xs, ys, 1, gdal.GDT_Byte, options=base_co + ["PREDICTOR=2"])
            if out is None:
                raise RuntimeError(f"Hilfsraster nicht anzulegen: {path}")
            out.SetGeoTransform(ds.GetGeoTransform())
            out.SetProjection(ds.GetProjection())
            return out

        # --- Loch-Maske sichern, solange die Loecher noch da sind ---
        mask_ds = _byte_raster(mask_path)
        mask_band = mask_ds.GetRasterBand(1)
        holes_total = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            hole = (band.ReadAsArray(0, y0, xs, rows) == LAS_CELL_NODATA)
            holes_total += int(np.count_nonzero(hole))
            mask_band.WriteArray(hole.astype("uint8"), 0, y0)
        mask_band.FlushCache()

        # --- Kleine Loecher aus der Maske sieben -> uebrig bleiben die grossen ---
        # SieveFilter entfernt Polygone KLEINER als die Schwelle. Deshalb +1, damit ein
        # Loch von exakt max_hole_px noch gefuellt wird ("bis zu" inklusive).
        sieve_ds = _byte_raster(sieve_path)
        sieve_band = sieve_ds.GetRasterBand(1)
        gdal.SieveFilter(mask_band, None, sieve_band, max_hole_px + 1,
                          LAS_FILL_HOLE_CONNECTEDNESS)
        sieve_band.FlushCache()

        # --- Fuellen ---
        # Suchdistanz max_hole_px ist eine harte obere Schranke: ein Loch von A Pixeln
        # kann kein Pixel enthalten, das weiter als A Pixel von gueltigen Daten entfernt
        # liegt. Alle zu fuellenden Loecher sind damit sicher erreicht, ohne dass die
        # Interpolation ueber die riesige Flaeche ausserhalb der Daten laeuft.
        old_tmpdir = gdal.GetConfigOption("CPL_TMPDIR")
        gdal.SetConfigOption("CPL_TMPDIR", str(run_dir))
        try:
            gdal.FillNodata(band, None, float(max_hole_px),
                             LAS_FILL_SMOOTHING_ITERATIONS)
        finally:
            gdal.SetConfigOption("CPL_TMPDIR", old_tmpdir)
        band.FlushCache()

        # --- Diesen Stand (alles gefuellt) als Hillshade-Quelle sichern ---
        # Muss VOR dem Ruecksetzen passieren - danach ist er nicht mehr rekonstruierbar,
        # ohne die ganze Interpolation zu wiederholen.
        ds.FlushCache()
        hs_copy = gdal.GetDriverByName("GTiff").CreateCopy(
            str(hs_src_path), ds, options=base_co + ["PREDICTOR=3"])
        if hs_copy is None:
            raise RuntimeError(f"Hillshade-Quelle nicht zu schreiben: {hs_src_path}")
        hs_copy.FlushCache()
        hs_copy = None

        # --- Grosse Loecher wieder auf NoData (nur im DSM) ---
        # Das UND mit der Originalmaske sichert dagegen ab, dass SieveFilter kleine
        # GUELTIGE Inseln inmitten eines grossen Lochs mitverschluckt: zurueckgesetzt
        # wird nur, was vorher schon NoData war.
        kept = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            big = ((sieve_band.ReadAsArray(0, y0, xs, rows) != 0) &
                   (mask_band.ReadAsArray(0, y0, xs, rows) != 0))
            n = int(np.count_nonzero(big))
            if n:
                arr = band.ReadAsArray(0, y0, xs, rows)
                arr[big] = LAS_CELL_NODATA
                band.WriteArray(arr, 0, y0)
                kept += n
        band.FlushCache()

        # --- Kontrolle statt Annahme: uebrig sein duerfen nur die grossen Loecher ---
        remaining = 0
        for y0 in range(0, ys, rows_per_chunk):
            rows = min(rows_per_chunk, ys - y0)
            arr = band.ReadAsArray(0, y0, xs, rows)
            remaining += int(np.count_nonzero(arr == LAS_CELL_NODATA))
    finally:
        ds = mask_ds = sieve_ds = None

    log(f"  Interpoliert (Loecher bis {LAS_FILL_MAX_HOLE_AREA_M2:g} m2): "
        f"{holes_total - kept} Pixel")
    log(f"  Als NoData belassen (groessere Loecher, inkl. Flaeche ausserhalb der "
        f"Daten): {kept} Pixel")
    if remaining != kept:
        log(f"  WARNUNG: {remaining - kept} Pixel blieben NoData, obwohl ihr Loch unter "
            f"der Schwelle liegt - kein gueltiger Nachbar in Reichweite.")
    else:
        log("  Kontrolle OK: es ist genau das NoData uebrig, das stehen bleiben soll.")
    log(f"  Hillshade-Quelle (alle Loecher gefuellt): {hs_src_path}")
    return (str(filled_path), str(hs_src_path))


def _prepare_hillshade_values(path: str) -> tuple:
    """Bereitet den rohen Hillshade so vor, dass 255 im Endprodukt ausschliesslich
    "ausserhalb des AOI" bedeutet (in-place). Gibt (gedeckelt, gefuellt) zurueck.

    Zwei Quellen fuer ein 255 innerhalb des AOI, beide werden auf 254 gezogen:
      - gdaldem liefert 1..255; ein voll beleuchtetes Pixel erreicht legitim 255 und
        waere nach dem Clip nicht mehr von der NoData-Maske zu unterscheiden.
      - Wo die Quelle NoData traegt, schreibt gdaldem seinen eigenen NoData-Wert (0).
        Weil der Hillshade aus dem VOLLSTAENDIG gefuellten Mosaik gerechnet wird,
        betrifft das nur noch Flaechen ausserhalb der Daten - die setzt der folgende
        Warp ohnehin wieder auf 255. Das Umsetzen ist dort wirkungslos, nicht falsch,
        und bleibt als Netz fuer den Fall, dass doch eine Fehlstelle durchkommt.

    Der NoData-Eintrag des Bandes bleibt stehen: nach dem Umsetzen traegt ihn kein
    Pixel mehr, der Warp findet also nichts zu maskieren und setzt 255 nur noch
    ausserhalb der Cutline. Ein Grauwert von 255 weniger ist visuell nicht wahrnehmbar.

    Laeuft blockweise (Zeilenpakete von rund 64 MB), damit auch grosse Mosaike nicht
    komplett in den Arbeitsspeicher muessen."""
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(path, gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"Hillshade nicht zum Schreiben zu oeffnen: {path}")
    try:
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        nd = None if nd is None else int(nd)
        rows_per_chunk = max(1, (64 * 1024 * 1024) // max(1, band.XSize))
        clamped = filled = 0
        for y0 in range(0, band.YSize, rows_per_chunk):
            rows = min(rows_per_chunk, band.YSize - y0)
            arr = band.ReadAsArray(0, y0, band.XSize, rows)
            hit_max = (arr == LAS_HILLSHADE_NODATA)
            hit_nd = ((arr == nd) if nd is not None and nd != LAS_HILLSHADE_NODATA
                      else np.zeros_like(hit_max))
            n_max, n_nd = int(np.count_nonzero(hit_max)), int(np.count_nonzero(hit_nd))
            if n_max or n_nd:
                arr[hit_max | hit_nd] = LAS_HILLSHADE_VALID_MAX
                band.WriteArray(arr, 0, y0)
                clamped += n_max
                filled += n_nd
        band.FlushCache()
    finally:
        ds = None
    return (clamped, filled)


def _mosaic_las_raster(cell_rasters, run_dir: Path, output_path: str,
                        hillshade_output_path: str, gsd: float, clip_shape_path: str,
                        snap_bounds: tuple, num_threads: str, log, progress) -> None:
    """Setzt die Zell-Raster zum Gesamt-DSM zusammen (VRT-Mosaik), interpoliert kleine
    NoData-Loecher (LAS_FILL_NODATA_HOLES, grosse bleiben NoData), maskiert per
    AOI-Shape (gdal.Warp Cutline, NoData ausserhalb) und rechnet den Hillshade.

    Der Hillshade kommt NICHT aus dem geclippten DSM, sondern aus dem vollstaendig
    gefuellten, ungeclippten Mosaik: so bekommen die grossen DSM-Loecher eine
    plausible Schattierung statt einer weissen Flaeche, und am AOI-Rand entsteht kein
    NoData-Saum (gdaldem sieht dort sonst die Cutline-Kante als Datenrand). Innerhalb
    des AOI ist der Hillshade damit lochfrei - 255 steht dort ausschliesslich fuer
    "ausserhalb des AOI". Beide Raster liegen auf demselben Gitter."""
    from osgeo import gdal
    gdal.UseExceptions()

    snap_minx, snap_miny, snap_maxx, snap_maxy = snap_bounds

    # Verifizieren, dass PDAL die angeforderten "bounds" tatsaechlich respektiert hat
    # (bounds-Unterstuetzung in writers.gdal ist PDAL-versionsabhaengig).
    check_ds = gdal.Open(cell_rasters[0], gdal.GA_ReadOnly)
    check_gt = check_ds.GetGeoTransform()
    check_ds = None
    dx = (check_gt[0] - snap_minx) / gsd
    dy = (check_gt[3] - snap_maxy) / gsd
    origin_off = max(abs(dx - round(dx)), abs(dy - round(dy))) * gsd
    if origin_off > 0.001:
        log(f"  WARNUNG: Ursprung der Zell-Raster ({check_gt[0]:.3f}, {check_gt[3]:.3f}) liegt nicht "
            f"auf dem gesnappten {gsd:g}m-Raster (Versatz {origin_off:.3f}m) - 'bounds' wird von "
            f"dieser PDAL-Version in writers.gdal evtl. nicht wie erwartet unterstuetzt. Das Mosaik "
            f"wird beim Clip auf das Zielraster resampled. Bitte pruefen.")
    else:
        log(f"  Pixelraster-Check OK: Zell-Raster liegen auf dem gesnappten {gsd:g}m-Raster.")

    vrt_path = run_dir / "03_raster_merged_raw.vrt"
    log(f"\nSetze {len(cell_rasters)} Zell-Raster zum Gesamt-Mosaik zusammen: {vrt_path}")
    vrt_ds = gdal.BuildVRT(
        str(vrt_path), cell_rasters,
        options=gdal.BuildVRTOptions(srcNodata=LAS_CELL_NODATA,
                                      VRTNodata=LAS_CELL_NODATA),
    )
    if vrt_ds is None:
        raise RuntimeError("gdal.BuildVRT hat None zurueckgegeben - Raster-Mosaik fehlgeschlagen.")
    vrt_ds.FlushCache()
    vrt_ds = None

    warp_source = hillshade_source = str(vrt_path)
    if LAS_FILL_NODATA_HOLES:
        warp_source, hillshade_source = _fill_raster_nodata(
            vrt_path, run_dir, gsd, num_threads, log)

    log(f"\nClippe Raster auf AOI (Cutline): {clip_shape_path}")
    log(f"  Ausserhalb -> NoData = {LAS_RASTER_NODATA:g}")
    log(f"  Zellraster-NoData {LAS_CELL_NODATA:g} (PDAL) -> {LAS_RASTER_NODATA:g} (GDWH-Sentinel)")
    log(f"  Ziel-Grid (auf {gsd:g}m gesnapped): {snap_minx:.2f}, {snap_miny:.2f} - "
        f"{snap_maxx:.2f}, {snap_maxy:.2f}")
    warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=clip_shape_path,
        cropToCutline=False,
        outputBounds=(snap_minx, snap_miny, snap_maxx, snap_maxy),
        xRes=gsd, yRes=gsd,  # Raster-Grid exakt beibehalten (kein implizites Resampling)
        # QUELLE dieses Warps sind NICHT die Input-Punktwolken, sondern die Zell-Raster
        # im Staging-Ordner (Wegwerfprodukte, von PDAL geschrieben) - die tragen
        # LAS_CELL_NODATA. Explizit angegeben statt GDAL die Band-Metadaten der
        # Zell-Raster raten zu lassen: wuerde der Sentinel dort nicht erkannt, kaemen
        # die NoData-Pixel als ECHTE Hoehenwerte (-9999) ins DSM.
        srcNodata=LAS_CELL_NODATA,
        # ZIEL ist das Endprodukt - hier steht der GDWH-Sentinel, den 'gdalinfo' meldet.
        dstNodata=LAS_RASTER_NODATA,
        multithread=True,
        warpOptions=[f"NUM_THREADS={num_threads}"],
        # gdal.Warp kennt kein "outputSRS" (das gehoert zu gdal.Translate) - hier srcSRS/dstSRS.
        # srcSRS explizit, damit der Clip auch dann laeuft, wenn das VRT/PDAL-Zellraster
        # kein CRS-Tag traegt; da Quelle == Ziel wird nichts reprojiziert.
        srcSRS="EPSG:2056",
        dstSRS="EPSG:2056",
        creationOptions=[
            "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
            "COMPRESS=LZW", "PREDICTOR=3", "BIGTIFF=YES", "TFW=YES",
        ],
        callback=progress,
    )
    out_ds = gdal.Warp(output_path, warp_source, options=warp_options)
    if out_ds is None:
        raise RuntimeError("gdal.Warp hat None zurueckgegeben - Raster-Clip fehlgeschlagen.")
    out_ds.FlushCache()
    out_ds = None
    log(f"  Gesamt-Raster (DSM) geschrieben: {output_path}")

    # Kontrolle statt Annahme: der GDWH-Sentinel MUSS bitgenau im Header stehen (das,
    # was 'gdalinfo' meldet). Verglichen wird der in Float32 gespeicherte Wert - GTiff
    # legt NoData als ASCII-Tag ab, die Textform darf also abweichen, der Zahlenwert
    # nicht. Stimmt er nicht, ist das DSM nicht auslieferbar -> harter Abbruch.
    check_ds = gdal.Open(output_path, gdal.GA_ReadOnly)
    written_nd = check_ds.GetRasterBand(1).GetNoDataValue()
    check_ds = None
    as_float32 = (struct.unpack("<f", struct.pack("<f", written_nd))[0]
                  if written_nd is not None else None)
    if as_float32 != LAS_RASTER_NODATA:
        raise RuntimeError(
            f"NoData im DSM-Header ist {written_nd!r}, erwartet {LAS_RASTER_NODATA!r} "
            f"(GDWH-Konvention SB_DSM) - Raster nicht auslieferbar.")
    log(f"  NoData-Kontrolle OK: Header traegt {written_nd!r}")

    # --- Hillshade aus dem vollstaendig gefuellten, ungeclippten Mosaik rechnen ---
    log("\nErzeuge Hillshade...")
    log(f"  Quelle: {hillshade_source}")
    raw_hillshade_path = run_dir / "05_hillshade_raw.tif"
    hs_ds = gdal.DEMProcessing(
        str(raw_hillshade_path), hillshade_source, "hillshade",
        options=gdal.DEMProcessingOptions(computeEdges=True),
    )
    if hs_ds is None:
        raise RuntimeError("gdal.DEMProcessing hat None zurueckgegeben - Hillshade fehlgeschlagen.")
    hs_ds.FlushCache()
    hs_ds = None

    # Muss VOR dem Clip passieren: danach ist 255 die NoData-Maske und weder ein
    # gueltiges 255er-Pixel noch ein Loch waere davon noch zu trennen.
    n_clamped, n_filled = _prepare_hillshade_values(str(raw_hillshade_path))
    log(f"  Voll beleuchtete Pixel {LAS_HILLSHADE_NODATA} -> "
        f"{LAS_HILLSHADE_VALID_MAX}: {n_clamped}")
    log(f"  NoData-Pixel -> {LAS_HILLSHADE_VALID_MAX}: {n_filled} (Flaeche ausserhalb "
        f"der Daten - der Clip setzt sie gleich wieder auf {LAS_HILLSHADE_NODATA})")
    log(f"  Innerhalb des AOI bleibt damit kein Pixel mit {LAS_HILLSHADE_NODATA}.")

    log(f"  Clippe Hillshade auf AOI (Cutline): {clip_shape_path}  "
        f"(NoData ausserhalb = {LAS_HILLSHADE_NODATA})")
    hs_warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=clip_shape_path,
        cropToCutline=False,
        # Quelle ist jetzt das Mosaik, nicht mehr das fertige DSM - Ausschnitt und
        # Aufloesung muessen deshalb explizit auf das Zielgitter gezwungen werden,
        # damit DSM und Hillshade pixelgenau uebereinanderliegen.
        outputBounds=(snap_minx, snap_miny, snap_maxx, snap_maxy),
        xRes=gsd, yRes=gsd,
        dstNodata=LAS_HILLSHADE_NODATA,
        multithread=True,
        warpOptions=[f"NUM_THREADS={num_threads}"],
        srcSRS="EPSG:2056",
        dstSRS="EPSG:2056",
        creationOptions=[
            "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
            "COMPRESS=LZW", "PREDICTOR=2", "BIGTIFF=YES", "TFW=YES",
        ],
    )
    hs_out_ds = gdal.Warp(hillshade_output_path, str(raw_hillshade_path), options=hs_warp_options)
    if hs_out_ds is None:
        raise RuntimeError("gdal.Warp hat None zurueckgegeben - Hillshade-Clip fehlgeschlagen.")
    hs_out_ds.FlushCache()
    hs_out_ds = None

    # Kontrolle statt Annahme: DSM und Hillshade muessen deckungsgleich sein, sonst
    # passen sie im GIS nicht uebereinander.
    dsm_chk = gdal.Open(output_path, gdal.GA_ReadOnly)
    hs_chk = gdal.Open(hillshade_output_path, gdal.GA_ReadOnly)
    dsm_geom = (dsm_chk.RasterXSize, dsm_chk.RasterYSize, dsm_chk.GetGeoTransform())
    hs_geom = (hs_chk.RasterXSize, hs_chk.RasterYSize, hs_chk.GetGeoTransform())
    dsm_chk = hs_chk = None
    if dsm_geom != hs_geom:
        raise RuntimeError(f"DSM und Hillshade liegen nicht auf demselben Gitter: "
                            f"{dsm_geom} vs. {hs_geom}")
    log("  Gitter-Kontrolle OK: DSM und Hillshade sind deckungsgleich.")
    log(f"  Hillshade geschrieben: {hillshade_output_path}")


def _process_las(cfg: dict) -> None:
    from osgeo import gdal, ogr, osr

    jahr             = str(cfg["jahr"]).strip()
    area             = str(cfg["area"]).strip()
    create_raster    = bool(cfg.get("create_raster", False))
    gsd_raster       = float(cfg["gsd"]) if create_raster else None
    input_dir        = cfg["input_dir"]
    output_dir_laz     = cfg["output_dir_laz"]
    output_dir_raster  = cfg.get("output_dir_raster")
    out_format       = cfg.get("out_format", "las")
    clip_shape_path  = cfg["clip_shape_path"]
    grid_shape_path  = cfg["grid_shape_path"]
    staging_dir      = cfg["staging_dir"]
    num_workers      = int(cfg.get("num_workers", 6))
    keep_staging     = bool(cfg.get("keep_staging", False))
    thin_m           = cfg.get("thin_m")
    pdal_exe         = cfg["pdal_exe"]

    def _log(msg: str) -> None:
        print(msg, flush=True)

    if not pdal_exe or not os.path.isfile(pdal_exe):
        raise FileNotFoundError(
            "pdal.exe wurde nicht gefunden. Bitte pdal (Teil von OSGeo4W/QGIS) "
            "zum System-PATH hinzufuegen."
        )

    gdal.UseExceptions()
    ogr.UseExceptions()

    Path(output_dir_laz).mkdir(parents=True, exist_ok=True)
    if create_raster:
        Path(output_dir_raster).mkdir(parents=True, exist_ok=True)
    run_dir = Path(staging_dir) / f"{area}_{jahr}_LAS"
    run_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Staging-Ordner: {run_dir}")
    _log(f"PDAL           : {pdal_exe}")

    last_emit = {"t": 0.0, "p": -1.0}

    def _progress(complete, message, unknown=None):
        try:
            if complete is None:
                return 1
            pct = float(complete)
            now = time.time()
            if (now - last_emit["t"]) >= 1.0 or (pct - last_emit["p"]) >= 0.005:
                print(f"PROGRESS:{0.90 + pct * 0.10:.6f}", flush=True)
                last_emit["t"] = now
                last_emit["p"] = pct
        except Exception:
            pass
        return 1

    # --- Schritt 1: Input-Kacheln + Bounding Boxes (parallel) ---
    tiles = sorted(glob.glob(os.path.join(input_dir, "*.laz")) +
                   glob.glob(os.path.join(input_dir, "*.las")))
    if not tiles:
        raise FileNotFoundError(f"Keine .laz/.las Kacheln gefunden in: {input_dir}")
    _log(f"\nGefundene Input-Kacheln: {len(tiles)}")

    _log("Lese Metadaten (Bounding Box) aller Kacheln...")
    tile_bboxes = []
    tile_gps_bits = []
    meta_errors = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_tile_bbox_worker, (pdal_exe, t)) for t in tiles]
        for i, fut in enumerate(as_completed(futures), 1):
            path, minx, miny, maxx, maxy, genc, err = fut.result()
            if err:
                meta_errors.append((path, err))
            else:
                tile_bboxes.append((path, minx, miny, maxx, maxy))
                tile_gps_bits.append(genc & 0x01)
            if i == len(tiles) or i % max(1, len(tiles) // 100) == 0:
                print(f"PROGRESS:{(i / len(tiles)) * 0.10:.6f}", flush=True)

    for p, e in meta_errors:
        _log(f"  WARNUNG: Metadaten von {Path(p).name} nicht lesbar: {e}")
    if not tile_bboxes:
        raise RuntimeError("Keine gueltigen Kachel-Metadaten gefunden.")

    all_minx = min(b[1] for b in tile_bboxes)
    all_miny = min(b[2] for b in tile_bboxes)
    all_maxx = max(b[3] for b in tile_bboxes)
    all_maxy = max(b[4] for b in tile_bboxes)
    _log(f"  Gesamt-Extent Input: {all_minx:.1f}, {all_miny:.1f} - {all_maxx:.1f}, {all_maxy:.1f}")

    thin_token = f"thinnedout{round(thin_m * 10):02d}_" if thin_m else ""
    _log(f"\nThinning            : {(str(thin_m) + ' m') if thin_m else 'inaktiv'}")
    _log(f"Raster erstellen    : {'AKTIV (GSD ' + format(gsd_raster, 'g') + ' m)' if create_raster else 'inaktiv'}")
    # GPS-Time-Typ (global_encoding Bit 0) aus der Quelle uebernehmen statt ihn zu
    # erfinden. Nur wenn ALLE Quell-Tiles ihn setzen, wird er auch gesetzt - eine Kachel
    # ohne die Angabe darf nicht dazu fuehren, dass die Ausgabe etwas behauptet, was in
    # der Quelle nicht belegt ist.
    src_gps_bit = 1 if (tile_gps_bits and all(tile_gps_bits)) else 0
    if any(tile_gps_bits) and not src_gps_bit:
        _log(f"  WARNUNG: GPS-Time-Typ in der Quelle uneinheitlich "
             f"({sum(tile_gps_bits)} von {len(tile_gps_bits)} Tiles mit global_encoding "
             f"Bit 0) - die Ausgabe wird konservativ mit Bit 0 = 0 geschrieben.")

    _log(f"Punktwolken-Format  : .{out_format}  (LAS 1.{LAS_OUT_MINOR_VERSION} / "
         f"PF{LAS_OUT_POINT_FORMAT} mit RGB, CRS-Tag {LAS_OUT_SRS}, global_encoding "
         f"{0x10 | src_gps_bit} - von GeoSuite/REFRAME lesbar)")
    _log(f"Benennung           : {jahr}_{area}_TIN_{thin_token}raw_<NAME>_LV95_LHN95.{out_format}")

    # PF7 fuehrt Farbe - aber nur, wenn die Quelle welche hat. Nur hinweisen, nicht
    # abbrechen: eine farblose Lieferung ist fachlich moeglich, sie soll bloss nicht
    # unbemerkt entstehen. Eine Stichprobe auf der ersten Kachel genuegt.
    try:
        dims = _pdal_dimension_names(pdal_exe, tile_bboxes[0][0])
        if {"Red", "Green", "Blue"} & dims:
            _log(f"  Farbe               : Die Quell-Tiles fuehren RGB - PF"
                 f"{LAS_OUT_POINT_FORMAT} traegt sie durch GeoSuite/REFRAME hindurch bis "
                 f"ins Endprodukt des Tabs [LN02].")
        else:
            _log(f"  WARNUNG: Die Quell-Tiles fuehren KEINE Farbwerte (RGB). Die Ausgabe "
                 f"wird trotzdem PF{LAS_OUT_POINT_FORMAT} geschrieben, die Farbfelder "
                 f"bleiben aber 0.")
    except Exception as e:
        _log(f"  WARNUNG: Dimensionen der Quelle nicht lesbar ({e}) - RGB-Hinweis uebersprungen.")

    # --- Schritt 2: Zielnamen + Zielraster fuer DSM/Hillshade, nur falls aktiviert ---
    # Das Raster wird NICHT mehr in einem einzigen PDAL-Lauf ueber alle Input-
    # Kacheln gebaut (ein Merge der kompletten Punktwolke sprengt bei grossen
    # Projekten den Arbeitsspeicher), sondern zellweise zusammen mit den
    # Punktwolken-Kacheln (Schritt 5) und danach mosaikiert (Schritt 6).
    raster_name = None
    hillshade_name = None
    raster_out_path = None
    hillshade_out_path = None
    cells_dir = None
    snap_bounds = None
    if create_raster:
        gsd_label = f"{round(gsd_raster * 100)}cm"
        raster_name = f"{jahr}_{area}_DSM_{gsd_label}_LV95_LHN95.tif"
        hillshade_name = f"{jahr}_{area}_hillshade_{gsd_label}_LV95_LHN95.tif"
        raster_out_path = str(Path(output_dir_raster) / raster_name)
        hillshade_out_path = str(Path(output_dir_raster) / hillshade_name)
        # Pixelursprung auf ein sauberes GSD-Vielfaches snappen (keine AOI-Kante im Grid)
        snap_bounds = ((all_minx // gsd_raster) * gsd_raster,
                       (all_miny // gsd_raster) * gsd_raster,
                       math.ceil(all_maxx / gsd_raster) * gsd_raster,
                       math.ceil(all_maxy / gsd_raster) * gsd_raster)
        cells_dir = run_dir / "03_raster_cells"
        cells_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Raster-Benennung    : {raster_name}  (+ .tfw)")
        _log(f"Hillshade-Benennung : {hillshade_name}  (+ .tfw)")

    # --- Schritt 3: Clip-Shape fuer die LAZ-Ausgabe als WKT einlesen ---
    _log(f"\nLese Clip-Shape (fuer LAZ-Crop): {clip_shape_path}")
    clip_ds = ogr.Open(clip_shape_path, 0)
    if clip_ds is None:
        raise FileNotFoundError(f"OGR konnte das Clip-Shape nicht oeffnen: {clip_shape_path}")
    clip_layer = clip_ds.GetLayer()
    clip_geom = None
    for feat in clip_layer:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        clip_geom = g.Clone() if clip_geom is None else clip_geom.Union(g)
    if clip_geom is None:
        raise ValueError("Clip-Shape enthaelt keine Geometrien.")
    clip_wkt = clip_geom.ExportToWkt()
    clip_ds = None

    # --- Schritt 4: Grid-Shape vorbereiten (analog TIFFconverter) ---
    _log(f"\nOeffne Grid-Shape: {grid_shape_path}")
    shp_ds = ogr.Open(grid_shape_path, 0)
    if shp_ds is None:
        raise FileNotFoundError(f"OGR konnte das Grid-Shape nicht oeffnen: {grid_shape_path}")
    layer = shp_ds.GetLayer()

    name_field = "NAME"
    field_idx = layer.GetLayerDefn().GetFieldIndex(name_field)
    if field_idx < 0:
        fields = [layer.GetLayerDefn().GetFieldDefn(i).GetName()
                  for i in range(layer.GetLayerDefn().GetFieldCount())]
        raise ValueError(f"Grid-Shape enthaelt kein Feld '{name_field}' - vorhandene Felder: {fields}")

    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(2056)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    src_layer_srs = layer.GetSpatialRef()
    transform = None
    if src_layer_srs is None:
        _log("  WARNUNG        : Grid-Shape hat kein Koordinatensystem gesetzt - wird als EPSG:2056 angenommen.")
    elif not src_layer_srs.IsSame(target_srs):
        src_layer_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(src_layer_srs, target_srs)
        _log(f"  Grid-Shape CRS : {src_layer_srs.GetName()} -> wird nach EPSG:2056 reprojiziert")
    else:
        _log("  Grid-Shape CRS : EPSG:2056 (passend)")

    if transform is not None:
        inv_transform = osr.CoordinateTransformation(target_srs, src_layer_srs)
        xs, ys = [], []
        for cx, cy in ((all_minx, all_miny), (all_minx, all_maxy),
                       (all_maxx, all_miny), (all_maxx, all_maxy)):
            px, py, _ = inv_transform.TransformPoint(cx, cy)
            xs.append(px)
            ys.append(py)
        layer.SetSpatialFilterRect(min(xs), min(ys), max(xs), max(ys))
    else:
        layer.SetSpatialFilterRect(all_minx, all_miny, all_maxx, all_maxy)

    layer.ResetReading()
    total = layer.GetFeatureCount()
    _log(f"\nGefundene Grid-Kacheln (ueberlappend mit Input-Extent): {total}")
    _log(f"Parallele Prozesse  : {num_workers}")

    jobs = []
    skipped = 0
    for feature in layer:
        name_val = feature.GetField(name_field)
        if name_val is None or str(name_val).strip() == "":
            skipped += 1
            continue
        geom = feature.GetGeometryRef()
        if geom is None:
            skipped += 1
            continue
        geom = geom.Clone()
        if transform is not None:
            geom.Transform(transform)
        cminx, cmaxx, cminy, cmaxy = geom.GetEnvelope()
        if cmaxx <= all_minx or cminx >= all_maxx or cmaxy <= all_miny or cminy >= all_maxy:
            skipped += 1
            continue

        cell_tiles = [t[0] for t in tile_bboxes
                      if not (t[3] <= cminx or t[1] >= cmaxx or t[4] <= cminy or t[2] >= cmaxy)]
        if not cell_tiles:
            skipped += 1
            continue

        stem = f"{jahr}_{area}_TIN_{thin_token}raw_{str(name_val).strip()}_LV95_LHN95"
        job = {"cell_bounds": (cminx, cminy, cmaxx, cmaxy),
               "tiles": cell_tiles, "stem": stem, "cell": str(name_val).strip()}
        if create_raster:
            # Zellausschnitt auf das globale, gesnappte GSD-Raster legen (Ursprung
            # snap_bounds), damit sich die Zell-Raster luecken- und ueberlappungsfrei
            # mosaikieren lassen und exakt auf dem Ziel-Grid liegen. Das Epsilon faengt
            # Float-Rauschen ab (sonst gelegentlich eine Pixelspalte Ueberlappung).
            ox, oy = snap_bounds[0], snap_bounds[1]
            job["raster_bounds"] = (
                ox + math.floor((cminx - ox) / gsd_raster + 1e-6) * gsd_raster,
                oy + math.floor((cminy - oy) / gsd_raster + 1e-6) * gsd_raster,
                ox + math.ceil((cmaxx - ox) / gsd_raster - 1e-6) * gsd_raster,
                oy + math.ceil((cmaxy - oy) / gsd_raster - 1e-6) * gsd_raster,
            )
        jobs.append(job)

    shp_ds = None

    if not jobs:
        raise RuntimeError(
            "Keine Grid-Kachel ueberlappt die Input-Kacheln - Grid-Shape/Input pruefen."
        )

    # --- Schritt 5: Punktwolken-Kacheln (+ DSM-Zellen) parallel verarbeiten ---
    # Beides sind zellweise Jobs mit begrenztem Speicherbedarf (jeweils nur die
    # Input-Kacheln EINER Gitterzelle) und laufen im selben Pool - so sind nie
    # mehr pdal.exe-Prozesse gleichzeitig aktiv als unter "CPU-Kerne" eingestellt.
    cell_workers = {"las": _las_cell_worker, "dsm": _raster_cell_worker}
    cell_tasks = [("las", job["stem"],
                   (job, str(run_dir), output_dir_laz, pdal_exe, clip_wkt, thin_m,
                    out_format, src_gps_bit))
                  for job in jobs]
    if create_raster:
        cell_tasks += [("dsm", job["cell"],
                        (job, str(run_dir), str(cells_dir), pdal_exe, thin_m, gsd_raster))
                       for job in jobs]

    total_tasks = len(cell_tasks)
    _log(f"\nStarte parallele Verarbeitung: {total_tasks} Job(s) auf {num_workers} Prozess(en)"
         + (f" ({len(jobs)} Punktwolken-Kachel(n) + {len(jobs)} DSM-Zelle(n))" if create_raster else "")
         + "\n")

    def _job_label(kind: str, name: str) -> str:
        return f"{name}.{out_format}" if kind == "las" else f"DSM-Zelle {name}"

    written = empty_skipped = errors = 0
    dsm_written = dsm_empty = 0
    done = 0
    failed = []
    errors_by_kind = {"las": 0, "dsm": 0}
    progress_start = 0.10
    progress_span = (0.90 if create_raster else 1.0) - progress_start

    def _count_result(kind: str, status: str) -> None:
        """Erfolgs-/Leer-Zaehler pro Job-Art (Punktwolke bzw. DSM-Zelle)."""
        nonlocal written, empty_skipped, dsm_written, dsm_empty
        if status == "written":
            if kind == "las":
                written += 1
            else:
                dsm_written += 1
        else:
            if kind == "las":
                empty_skipped += 1
            else:
                dsm_empty += 1

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(cell_workers[kind], args): (kind, name, args)
                   for kind, name, args in cell_tasks}
        for future in as_completed(futures):
            kind, name, args = futures[future]
            status, _name, err = future.result()
            done += 1
            label = _job_label(kind, name)
            if status == "written":
                _count_result(kind, status)
                _log(f"  [{done}/{total_tasks}] {label}")
            elif status == "empty":
                _count_result(kind, status)
                _log(f"  [{done}/{total_tasks}] {label} - uebersprungen (keine Punkte nach Clip)")
            else:
                # Noch nicht als Fehler zaehlen: ein abgestuerzter pdal-Prozess ist
                # meist Speicherdruck durch die parallelen Jobs - wird unten
                # seriell wiederholt.
                failed.append((kind, name, args))
                _log(f"  [{done}/{total_tasks}] FEHLER bei {label} (Wiederholung folgt): {err}")
            print(f"PROGRESS:{progress_start + (done / total_tasks) * progress_span:.6f}", flush=True)

    # --- Fehlgeschlagene Jobs seriell wiederholen ---
    # Ein einzelner pdal.exe-Prozess hat den vollen Arbeitsspeicher zur Verfuegung -
    # damit faellt die haeufigste Absturzursache (Speicherdruck durch parallele
    # Prozesse) weg. Bleibt der Fehler, ist er echt.
    if failed:
        _log(f"\nWiederhole {len(failed)} fehlgeschlagene(n) Job(s) seriell "
             f"(ein pdal-Prozess nach dem anderen)...")
        for i, (kind, name, args) in enumerate(failed, 1):
            label = _job_label(kind, name)
            status, _name, err = cell_workers[kind](args)
            if status == "error":
                errors += 1
                errors_by_kind[kind] += 1
                _log(f"  [Retry {i}/{len(failed)}] FEHLER bleibt bei {label}: {err}")
            else:
                _count_result(kind, status)
                _log(f"  [Retry {i}/{len(failed)}] OK: {label}")

    # --- Schritt 6: Zell-Raster zum Gesamt-DSM mosaikieren, dann Hillshade ---
    if create_raster:
        if errors_by_kind["dsm"]:
            _log(f"\nWARNUNG: {errors_by_kind['dsm']} DSM-Zelle(n) fehlgeschlagen - das "
                 f"Gesamt-Raster erhaelt dort Loecher (NoData). Siehe Fehler oben.")
        cell_rasters = sorted(str(p) for p in cells_dir.glob("dsm_*.tif"))
        if not cell_rasters:
            raise RuntimeError("Keine DSM-Zelle wurde erzeugt - Gesamt-Raster nicht moeglich.")
        _mosaic_las_raster(cell_rasters, run_dir, raster_out_path, hillshade_out_path,
                            gsd_raster, clip_shape_path, snap_bounds, str(num_workers),
                            _log, _progress)

    if not keep_staging:
        _log(f"\nRaeume Staging-Ordner auf: {run_dir}")
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass
    else:
        _log(f"\nStaging-Dateien bleiben erhalten: {run_dir}")

    raster_line = (f"Raster: {raster_name}, Hillshade: {hillshade_name}\n"
                   f"DSM-Zellen: {dsm_written} gerastert, {dsm_empty} leer (0 Punkte).\n"
                   if raster_name else "Raster: nicht erstellt (Option deaktiviert)\n")
    _log(f"\nFertig. {raster_line}"
         f".{out_format}: {written} Kachel(n) geschrieben, {skipped} uebersprungen (kein Overlap), "
         f"{empty_skipped} leer (0 Punkte nach Clip).\n"
         f"Fehler gesamt: {errors}.")
    if written == 0:
        raise RuntimeError("Keine Punktwolken-Kachel wurde geschrieben.")
    if errors:
        raise RuntimeError(f"{errors} Job(s) konnten auch beim seriellen Wiederholen nicht "
                            f"verarbeitet werden - siehe Log.")


# ─── DMC LASconverter [LN02] (GDWH-Metadaten, LAS 1.4) ─────────────────────────
#
# Nachgelagerter Schritt zum Tab "DMC - LASconverter [LHN95]": dessen .las-Kacheln
# werden extern mit GeoSuite/REFRAME von LHN95 nach LN02 reframt (nur die Hoehe,
# X/Y bleiben LV95) - dieser Tab bringt das Ergebnis anschliessend in die
# GDWH-taugliche Form.
#
# Ablauf:
#   1) Dateinamen aller Input-Kacheln pruefen (Kachelursprung), Metadaten
#      (Bounding Box) parallel einlesen
#   2) Pro Kachel: Requantisierung auf LAS 1.4 / Point Data Record Format 7
#      (= PF6 + RGB), scale 0.01, Offset = Kachelursprung (aus dem DATEINAMEN geparst),
#      global_encoding 17; danach Byte-Injektion der zwei LV95/LN02-Referenz-VLRs
#      (34735 GeoTIFF-KeyDirectory + 2112 OGC-WKT) und vollstaendige Validierung.
#      Geschrieben wird als .las oder .laz (out_format).
#   3) DSM-Zellen (nur falls "Create Raster" aktiv): dieselbe Zellgeometrie, mit
#      Puffer gecroppt und als Float32-IDW-Raster gerastert (wie im LHN95-Tab)
#   4) Gesamt-Raster: Zell-Raster als VRT mosaikieren, per AOI-/Footprint-Shape
#      maskieren (NoData = LAS_RASTER_NODATA), daraus den Hillshade rechnen und
#      ebenfalls maskieren (NoData = 255 nur ausserhalb des AOI)
#
# KEIN Reframe, KEIN Re-Tiling, KEIN Crop der Punktwolke - der Input ist bereits
# das fertige, AOI-gecroppte 1km-Grid aus dem LHN95-Tab. Das AOI-/Footprint-Shape
# wird ausschliesslich fuer die Raster-Maskierung gebraucht.
#
# Warum die CRS-Tags per Byte-Injektion und nicht ueber PDAL/las2las gesetzt werden,
# siehe Docstring von _inject_reference_vlrs().


def _build_vlr_record(user_id: str, record_id: int, description: str, payload: bytes) -> bytes:
    """Baut einen kompletten LAS-VLR (54-Byte-Header + Payload)."""
    header = struct.pack(
        "<H16sHH32s",
        0,
        user_id.encode("ascii").ljust(16, b"\x00"),
        record_id,
        len(payload),
        description.encode("ascii").ljust(32, b"\x00"),
    )
    return header + payload


def _inject_reference_vlrs(las_path: str) -> int:
    """Fuegt die zwei byte-exakten LV95/LN02-Referenz-VLRs (GeoTIFF-KeyDirectory
    34735 + OGC-WKT 2112) in eine LAS/LAZ-Datei ein, OHNE eine CRS-Bibliothek den
    WKT neu berechnen zu lassen. Uebernommen aus dem verifizierten Skript
    4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py (Projekt topo-importDATAtoGDWH-STAC).

    Begruendung (dort empirisch getestet, nicht angenommen):
      - PDAL erzeugt bei a_srs="EPSG:2056+5728" einen semantisch korrekten, aber
        NICHT byte-identischen WKT (COMPD_CS statt COMPOUNDCRS, anderer CRS-Name)
        und schreibt den GeoTIFF-VLR (34735) gar nicht.
      - las2las -epsg 2056 -vertical_epsg 5728 -set_ogc_wkt lieferte in der
        getesteten Version geodaetisch FALSCHE Oblique-Mercator-Parameter und liess
        die Vertikalkomponente (LN02/5728) ganz weg.
      - PDALs eigene writers.las-Option 'vlrs' verwirft VLRs mit user_id
        "LASF_Projection" still - deshalb Byte-Patch statt PDAL-Option.

    Funktioniert auch bei komprimierten (LAZ) Punktdaten: dafuer muss zusaetzlich
    zum Header ('offset_to_point_data') auch die 'chunk table start position' der
    LASzip-Kompression korrigiert werden (int64 am Anfang des Punkt-Bereichs,
    absoluter Datei-Offset auf die Chunk-Tabelle). Ohne diese Korrektur bleibt die
    Datei zwar fuer 'pdal info --metadata' lesbar (die Punktzahl kommt aus dem
    Header), jeder echte Dekompressions-Durchlauf bricht aber mit 'Invalid version
    ... found in LAZ chunk table' ab.

    Bereits vorhandene CRS-VLRs werden ENTFERNT, nicht als Fehler behandelt: PDAL
    uebernimmt eine in der Quelle vorgefundene (hier: LHN95-)SRS-VLR beim Schreiben
    automatisch und legt zusaetzlich seinen eigenen 'liblas'-Zwilling an. Autoritativ
    fuer die Ziel-CRS ist ausschliesslich die Referenz unten - welche VLRs deshalb
    weichen muessen, steht bei CRS_VLR_USER_IDS.

    Arbeitet in-place - nur auf einer Temp-Datei aufrufen (siehe _ln02_tile_worker).
    Gibt die Anzahl entfernter 'LASF_Projection'-VLRs zurueck.

    Der Punktbereich wird chunkweise umkopiert statt die Datei komplett in den Speicher
    zu lesen: eine unkomprimierte 1km-Kachel wird schnell mehrere hundert MB gross, und
    bei parallelen Kachel-Jobs laege ein Vielfaches davon gleichzeitig im RAM. Header
    und VLR-Block sind dagegen wenige KB und werden ganz gelesen.
    """
    with open(las_path, "rb") as f:
        head = f.read(512)
        header_size, offset_to_point_data, n_vlr = struct.unpack_from("<HII", head, 94)
        f.seek(0)
        data = f.read(offset_to_point_data)  # nur Header + VLR-Block

    existing_vlr_block = data[header_size:offset_to_point_data]

    is_laszip = False
    n_stripped = 0
    kept_vlr_chunks = []
    pos = 0
    for _ in range(n_vlr):
        _, user_id_raw, record_id, record_len, _ = struct.unpack_from(
            "<H16sHH32s", existing_vlr_block, pos)
        user_id = user_id_raw.split(b"\x00")[0].decode("ascii", "replace")
        vlr_len = 54 + record_len
        if _is_crs_vlr(user_id, record_id):
            n_stripped += 1
        else:
            kept_vlr_chunks.append(existing_vlr_block[pos:pos + vlr_len])
        if user_id == "laszip encoded" and record_id == 22204:
            is_laszip = True
        pos += vlr_len

    existing_vlr_block = b"".join(kept_vlr_chunks)
    n_vlr -= n_stripped

    vlr1 = _build_vlr_record("LASF_Projection", 34735, REFERENCE_VLR_DESCRIPTION,
                              base64.b64decode(REFERENCE_VLR_34735_B64))
    vlr2 = _build_vlr_record("LASF_Projection", 2112, REFERENCE_VLR_DESCRIPTION,
                              base64.b64decode(REFERENCE_VLR_2112_B64))
    new_vlr_block = bytes(existing_vlr_block) + vlr1 + vlr2
    new_offset_to_point_data = header_size + len(new_vlr_block)
    # Tatsaechliche Verschiebung der Punktdaten - NICHT einfach len(vlr1)+len(vlr2):
    # wurden oben VLRs entfernt, ist die Nettoverschiebung kleiner.
    shift = new_offset_to_point_data - offset_to_point_data

    new_header = bytearray(data[:header_size])
    struct.pack_into("<I", new_header, 96, new_offset_to_point_data)
    struct.pack_into("<I", new_header, 100, n_vlr + 2)
    global_encoding, = struct.unpack_from("<H", new_header, 6)
    struct.pack_into("<H", new_header, 6, global_encoding | 0x10)  # WKT-Bit setzen

    tmp_out = las_path + ".vlrtmp"
    try:
        with open(las_path, "rb") as fin, open(tmp_out, "wb") as fout:
            fout.write(new_header)
            fout.write(new_vlr_block)
            fin.seek(offset_to_point_data)
            if is_laszip:
                # Absoluter Datei-Offset auf die LASzip-Chunk-Tabelle - verschiebt sich
                # mit den Punktdaten. Ohne diese Korrektur bleibt die Datei fuer
                # 'pdal info --metadata' lesbar, jeder echte Dekompressions-Durchlauf
                # bricht aber mit 'Invalid version ... in LAZ chunk table' ab.
                first8 = fin.read(8)
                if len(first8) == 8:
                    chunk_table_pos, = struct.unpack("<q", first8)
                    if chunk_table_pos != -1:  # -1 = Platzhalter, nicht bei fertigen Dateien
                        first8 = struct.pack("<q", chunk_table_pos + shift)
                fout.write(first8)
            shutil.copyfileobj(fin, fout, 8 * 1024 * 1024)
        os.replace(tmp_out, las_path)
        tmp_out = None
    finally:
        if tmp_out and os.path.isfile(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

    return n_stripped


def _parse_tile_origin(filename: str) -> tuple:
    """Parst Easting/Northing (in km) deterministisch aus dem Dateinamen
    (Muster '..._<E>_<N>_LV95_<LHN95|LN02>.<las|laz>'), NICHT aus dem Datenminimum:
    eine AOI-gecroppte Kachel faengt sonst irgendwo mitten in der Zelle an und der
    Offset waere nicht mehr der Kachelursprung.

    Wirft ValueError bei fehlendem Muster oder unplausiblen Werten (ausserhalb der
    Schweizer Landesgrenzen LV95, in km)."""
    match = LN02_TILE_NAME_PATTERN.search(filename)
    if not match:
        raise ValueError(
            f"Kachelmuster '..._<Easting>_<Northing>_LV95_<LHN95|LN02>.<las|laz>' nicht "
            f"gefunden in '{filename}' - Kachelursprung (Offset) nicht bestimmbar.")
    easting_km, northing_km = int(match.group(1)), int(match.group(2))

    e_min, e_max = LV95_EASTING_KM_RANGE
    n_min, n_max = LV95_NORTHING_KM_RANGE
    if not (e_min <= easting_km <= e_max):
        raise ValueError(f"Kachel-Easting {easting_km} km aus '{filename}' liegt ausserhalb "
                          f"der plausiblen LV95-Ausdehnung ({e_min}-{e_max} km).")
    if not (n_min <= northing_km <= n_max):
        raise ValueError(f"Kachel-Northing {northing_km} km aus '{filename}' liegt ausserhalb "
                          f"der plausiblen LV95-Ausdehnung ({n_min}-{n_max} km).")
    return easting_km, northing_km


def _parse_thin_token(filename: str) -> str:
    """Uebernimmt den Thinning-Token (z.B. 'thinnedout04_') aus dem Quell-Dateinamen.
    Ohne Token im Namen (= nicht ausgeduennt) ein leerer String."""
    match = LN02_THIN_TOKEN_PATTERN.search(filename)
    return f"{match.group(1).lower()}_" if match else ""


def _check_ln02_tile_frame(md: dict, origin_x: float, origin_y: float, filename: str) -> None:
    """Prueft, ob die Quell-BBox innerhalb des nominalen 1km-Kachelrahmens liegt.

    Punkte AUSSERHALB des Rahmens sind ein harter Fehler (falsch geparste
    Kachelkoordinaten oder fehlplatzierte Datei). Luecken zum Rand werden bewusst
    NICHT gemeldet: die Kacheln sind AOI-gecroppt, unvollstaendig gefuellte
    Randkacheln sind hier der Normalfall - anders als bei swissSURFACE3D."""
    try:
        minx, maxx = float(md["minx"]), float(md["maxx"])
        miny, maxy = float(md["miny"]), float(md["maxy"])
    except (KeyError, TypeError, ValueError):
        return
    eps = 0.02  # Toleranz gegen Rundungsrauschen am Rand
    if minx < origin_x - eps or maxx > origin_x + 1000.0 + eps or \
       miny < origin_y - eps or maxy > origin_y + 1000.0 + eps:
        raise ValueError(
            f"Punkte ausserhalb des Kachelrahmens: BBox (X {minx:.2f}-{maxx:.2f}, "
            f"Y {miny:.2f}-{maxy:.2f}) vs. erwarteter Rahmen "
            f"(X {origin_x:.2f}-{origin_x + 1000.0:.2f}, "
            f"Y {origin_y:.2f}-{origin_y + 1000.0:.2f}).")


def _resolve_crs_epsg(md: dict) -> tuple:
    """Liest horizontalen und vertikalen EPSG-Code aus der von PDAL/PROJ bereits
    aufbereiteten 'srs.json'-Struktur (CompoundCRS mit Bestandteilen
    'ProjectedCRS'/'GeographicCRS' und 'VerticalCRS') - keine pyproj-Abhaengigkeit
    noetig, PDAL nutzt intern ohnehin PROJ dafuer.
    Gibt (horizontal, vertikal) zurueck, je None falls nicht aufloesbar."""
    j = ((md.get("srs") or {}).get("json")) or {}
    components = j.get("components") or []
    horizontal_epsg = vertical_epsg = None
    for comp in components:
        ident = comp.get("id") or {}
        epsg = ident.get("code") if ident.get("authority") == "EPSG" else None
        if comp.get("type") == "VerticalCRS":
            vertical_epsg = epsg
        elif comp.get("type") in ("ProjectedCRS", "GeographicCRS", "GeodeticCRS"):
            horizontal_epsg = epsg
    if not components:
        ident = j.get("id") or {}
        if ident.get("authority") == "EPSG":
            horizontal_epsg = ident.get("code")
    return horizontal_epsg, vertical_epsg


def _has_reference_vlrs(md: dict) -> bool:
    """True, wenn die Datei BEIDE byte-exakten Referenz-VLRs traegt (34735 mit der
    Referenz-Payload und 2112 mit dem Referenz-WKT). Das ist der Fingerabdruck einer
    von _inject_reference_vlrs erzeugten Kachel - CRS-Tags aus anderer Quelle
    (GeoSuite, LAStools, PDAL) sehen anders aus, auch wenn sie dasselbe CRS meinen."""
    # Verglichen werden die DEKODIERTEN Bytes, nicht die base64-Strings: Zeilenumbrueche
    # oder abweichende Padding-Schreibweise duerfen das Ergebnis nicht verfaelschen.
    ref = {34735: base64.b64decode(REFERENCE_VLR_34735_B64),
           2112:  base64.b64decode(REFERENCE_VLR_2112_B64)}
    found = set()
    i = 0
    while f"vlr_{i}" in md:
        vlr = md[f"vlr_{i}"]
        rid = vlr.get("record_id")
        if vlr.get("user_id") == "LASF_Projection" and rid in ref:
            try:
                if base64.b64decode(vlr.get("data") or "") == ref[rid]:
                    found.add(rid)
            except Exception:
                pass
        i += 1
    return found == set(ref)


def _ln02_is_already_migrated(md: dict) -> bool:
    """True, wenn die Datei bereits das fertige Zielprodukt ist - dann ist keine
    Konversion noetig und die Kachel wird nur kopiert.

    Geprueft wird bewusst mehr als Version/Punktformat/CRS: seit auch die
    Zwischenstufe LAS 1.4/PF7 ist, unterscheidet sich eine GeoSuite-Ausgabe vom
    Zielprodukt nur noch an scale/offset und den CRS-VLRs. Ohne diese beiden
    Kontrollen koennte eine reframte Kachel faelschlich als 'fertig' durchgehen und
    unveraendert kopiert werden - mit GeoSuites CRS-Tags statt den autoritativen."""
    if md.get("minor_version") != LN02_MINOR_VERSION:
        return False
    if md.get("dataformat_id") != LN02_POINT_FORMAT:
        return False
    if md.get("global_encoding") != LN02_GLOBAL_ENCODING:
        return False
    for key in ("scale_x", "scale_y", "scale_z"):
        try:
            if abs(float(md[key]) - LN02_SCALE) > 1e-9:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    if not _has_reference_vlrs(md):
        return False
    h_epsg, v_epsg = _resolve_crs_epsg(md)
    return h_epsg == 2056 and v_epsg == 5728


def _pdal_classification_range(pdal_exe: str, path: str) -> tuple:
    """Minimum/Maximum der Dimension 'Classification' (gezielter Einzelscan, nicht
    'pdal info --stats' ueber alle Dimensionen)."""
    result = subprocess.run(
        [pdal_exe, "info", "--dimensions", "Classification", "--stats", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"pdal info (Classification) beendet mit Exit-Code "
                            f"{result.returncode}" + (f": {msg}" if msg else ""))
    data = json.loads(result.stdout)
    for stat in data.get("stats", {}).get("statistic", []):
        if stat.get("name") == "Classification":
            return stat.get("minimum"), stat.get("maximum")
    return (None, None)


def _stat_range_from_pipeline_metadata(pipeline_metadata, dimension: str) -> tuple:
    """Liest Min/Max einer Dimension aus der Metadata einer Pipeline-Ausfuehrung mit
    'filters.stats'-Stage - so muss die Quelle nicht ein zweites Mal komplett eingelesen
    werden. Gibt (None, None) zurueck, falls Stage oder Dimension fehlen."""
    stats = ((pipeline_metadata or {}).get("stages") or {}).get("filters.stats") or {}
    for stat in stats.get("statistic", []):
        if stat.get("name") == dimension:
            return stat.get("minimum"), stat.get("maximum")
    return (None, None)


def _stats_bbox_from_pipeline_metadata(pipeline_metadata) -> dict:
    """BBox aus den GEMESSENEN X/Y/Z-Statistiken einer 'filters.stats'-Stage.

    Das sind die tatsaechlichen Extents der gelesenen Punkte - im Gegensatz zur
    Header-BBox, die in reframten Fremddaten veraltet sein kann. Gibt None
    zurueck, wenn eine der sechs Groessen fehlt (dann bleibt der Header die
    einzige Referenz)."""
    bbox = {}
    for dim, lo, hi in (("X", "minx", "maxx"),
                        ("Y", "miny", "maxy"),
                        ("Z", "minz", "maxz")):
        vmin, vmax = _stat_range_from_pipeline_metadata(pipeline_metadata, dim)
        if vmin is None or vmax is None:
            return None
        bbox[lo], bbox[hi] = vmin, vmax
    return bbox


def _validate_ln02_target(src_md: dict, dst_md: dict, src_measured: dict = None,
                          warnings: list = None) -> list:
    """Nachkonversions-Validierung. Gibt eine Liste von Fehler-Strings zurueck
    (leer = alles OK). Prueft NUR, repariert nichts:
      - Punktanzahl identisch
      - BBox identisch innerhalb LN02_BBOX_TOLERANCE_M. Referenz sind, wenn
        uebergeben, die GEMESSENEN Quell-Extents aus 'filters.stats'
        (src_measured), nicht die Quell-Header-BBox: reframte Fremddaten
        (GeoSuite/LAStools) fuehren im Header gelegentlich Werte, die nicht mehr
        zu den Punkten passen, und ein solcher Quell-Header darf eine korrekte
        Konversion nicht zu Fall bringen. Header-Abweichungen werden stattdessen
        nach 'warnings' gemeldet.
      - minor_version / dataformat_id / point_length / header_size / global_encoding
      - beide CRS-VLRs vorhanden (34735 + 2112), VLR 2112 endet auf Nullbyte
      - CRS aufloesbar: horizontal 2056, vertikal 5728
      - '5729' bzw. 'LHN95' kommen im Ziel-WKT NICHT vor (Kontrolle, dass wirklich
        LN02-Daten getaggt werden und nicht versehentlich LHN95-Kacheln)
    """
    problems = []

    if src_md.get("count") != dst_md.get("count"):
        problems.append(f"Punktanzahl weicht ab: Quelle {src_md.get('count')} vs. "
                         f"Ziel {dst_md.get('count')}")

    bbox_keys = ("minx", "maxx", "miny", "maxy", "minz", "maxz")
    reference = src_measured or src_md
    ref_label = "gemessene Quellpunkte" if src_measured else "Quell-Header"
    for key in bbox_keys:
        try:
            d = abs(float(reference[key]) - float(dst_md[key]))
        except (KeyError, TypeError, ValueError):
            problems.append(f"BBox-Feld '{key}' fehlt in Quelle oder Ziel.")
            continue
        if d > LN02_BBOX_TOLERANCE_M:
            problems.append(f"BBox-Feld '{key}' weicht {d:.4f} m ab "
                             f"(Toleranz {LN02_BBOX_TOLERANCE_M} m, "
                             f"Referenz: {ref_label}).")

    # Quell-Header gegen die gemessenen Quellpunkte: eine Abweichung ist ein Mangel
    # der QUELLE (nicht nachgefuehrte Header-BBox), nicht der Konversion. Das Ziel
    # traegt die von PDAL neu berechneten, korrekten Werte - deshalb Warnung statt
    # Fehler, damit die Kachel nicht grundlos aus der Lieferung faellt.
    if src_measured is not None and warnings is not None:
        stale = []
        for key in bbox_keys:
            try:
                d = abs(float(src_md[key]) - float(src_measured[key]))
            except (KeyError, TypeError, ValueError):
                continue
            if d > LN02_BBOX_TOLERANCE_M:
                stale.append(f"{key} {d:.4f} m")
        if stale:
            warnings.append(
                "Quell-Header-BBox passt nicht zu den tatsaechlichen Punkten ("
                + ", ".join(stale) + "). Die Zieldatei traegt die gemessenen Werte "
                "(fachlich korrekt); die Quelle sollte geprueft werden.")

    for field, expected in (("minor_version",   LN02_MINOR_VERSION),
                            ("dataformat_id",   LN02_POINT_FORMAT),
                            ("point_length",    LN02_POINT_LENGTH),
                            ("header_size",     LN02_HEADER_SIZE),
                            ("global_encoding", LN02_GLOBAL_ENCODING)):
        if dst_md.get(field) != expected:
            problems.append(f"{field}={dst_md.get(field)}, erwartet {expected}")

    found_34735 = found_2112 = vlr2112_ok = False
    stray_crs = []
    i = 0
    while f"vlr_{i}" in dst_md:
        vlr = dst_md[f"vlr_{i}"]
        user_id = vlr.get("user_id")
        record_id = vlr.get("record_id")
        if user_id == "LASF_Projection" and record_id == 34735:
            found_34735 = True
        elif user_id == "LASF_Projection" and record_id == 2112:
            found_2112 = True
            vlr2112_ok = base64.b64decode(vlr.get("data", "")).endswith(b"\x00")
        elif _is_crs_vlr(user_id or "", record_id or 0):
            stray_crs.append(f"{user_id}/{record_id}")
        i += 1
    if not found_34735:
        problems.append("VLR record_id 34735 (GeoTIFF KeyDirectory) fehlt im Ziel.")
    if not found_2112:
        problems.append("VLR record_id 2112 (OGC WKT) fehlt im Ziel.")
    elif not vlr2112_ok:
        problems.append("VLR record_id 2112 (OGC WKT) endet nicht auf Nullbyte.")
    # Genau EINE Aussage zum CRS: jeder weitere raumbezogene VLR ist eine zweite,
    # konkurrierende Angabe. Ein Leser, der den falschen nimmt, bekaeme LV95 ohne
    # LN02 - und niemand wuerde es merken. Deshalb harter Fehler, nicht Warnung.
    if stray_crs:
        problems.append(
            "Zusaetzliche CRS-VLRs im Ziel neben der Referenz: "
            + ", ".join(stray_crs)
            + " - die Kachel traegt damit mehr als eine Aussage zum Raumbezug.")

    h_epsg, v_epsg = _resolve_crs_epsg(dst_md)
    if h_epsg != 2056:
        problems.append(f"Horizontales CRS = EPSG:{h_epsg}, erwartet EPSG:2056")
    if v_epsg != 5728:
        problems.append(f"Vertikales CRS = EPSG:{v_epsg}, erwartet EPSG:5728 (LN02)")
    wkt_text = dst_md.get("spatialreference", "") or ""
    if "5729" in wkt_text or "LHN95" in wkt_text:
        problems.append("Ziel-WKT enthaelt '5729' bzw. 'LHN95' statt LN02 - FACHLICHER FEHLER.")

    return problems


def _ln02_tile_worker(args) -> tuple:
    """Konvertiert EINE bereits nach LN02 reframte 1km-Kachel in die GDWH-taugliche
    LAS-1.4-Form. Laeuft in einem eigenen Prozess (Gegenstueck zu _raster_cell_worker).

    Die Zieldatei wird erst nach vollstaendiger Validierung atomar (os.replace)
    geschrieben: schlaegt irgendetwas fehl, bleibt eine evtl. schon vorhandene
    Zieldatei unangetastet und die Temp-Datei wird verworfen. Die Quelle wird NIE
    veraendert.

    Umgesetzt wird ausschliesslich requantisiert und umgetagt (Punktformat, scale,
    offset, global_encoding, CRS-VLRs) - Koordinaten und Attribute bleiben inhaltlich,
    was GeoSuite/REFRAME geliefert hat. Die Farbe kommt seit dem GeoSuite-Update
    ebenfalls direkt aus der Quelle (PF7 rein, PF7 raus).

    Rueckgabe: (status, name, fehler, warnungen) mit status in
    'written' | 'copied' | 'error'."""
    (src_path, dst_path, pdal_exe, run_dir_str, origin_x, origin_y) = args

    src_name = os.path.basename(src_path)
    dst_name = os.path.basename(dst_path)
    warnings = []
    run_dir = Path(run_dir_str)
    stem = os.path.splitext(dst_name)[0]
    pipeline_path = run_dir / f"pipeline_ln02_{stem}.json"
    meta_path = run_dir / f"pipemeta_ln02_{stem}.json"
    tmp_path = None

    try:
        try:
            src_md = _pdal_info_metadata(pdal_exe, src_path)
        except Exception as e:
            return ("error", dst_name, f"Quelldatei nicht lesbar (pdal info): {e}", warnings)

        _check_ln02_tile_frame(src_md, origin_x, origin_y, src_name)

        # Bit 0 sagt, wie die GpsTime-Werte zu lesen sind (0 = GPS Week Time,
        # 1 = Adjusted Standard GPS Time). Das Zielformat verlangt 17, also Bit 0
        # gesetzt. Ob das eine echte Falschaussage waere, haengt davon ab, ob ueberhaupt
        # GpsTime-Werte vorliegen - das wird unten an den Daten gemessen, statt hier
        # pauschal zu warnen.
        src_gps_bit = int(src_md.get("global_encoding", 0) or 0) & 0x01

        # Fuehrt das Punktformat der Quelle ueberhaupt Farbfelder? Headerbasiert, also
        # gratis - und es entscheidet, ob 'filters.stats' unten RGB mitmessen darf
        # (die Stage bricht mit einem Fehler ab, wenn eine Dimension gar nicht existiert).
        src_has_rgb = src_md.get("dataformat_id") in PC_FORMATS_WITH_RGB

        same_ext = os.path.splitext(src_path)[1].lower() == os.path.splitext(dst_path)[1].lower()
        if same_ext and _ln02_is_already_migrated(src_md):
            shutil.copy2(src_path, dst_path)
            return ("copied", dst_name, None, warnings)

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(dst_name)[1],
                                             dir=os.path.dirname(dst_path))
        os.close(tmp_fd)
        os.remove(tmp_path)  # writers.las soll die Datei selbst anlegen

        writer = {
            "type": "writers.las",
            "filename": tmp_path,
            "minor_version": LN02_MINOR_VERSION,
            "dataformat_id": LN02_POINT_FORMAT,
            "scale_x": LN02_SCALE, "scale_y": LN02_SCALE, "scale_z": LN02_SCALE,
            "offset_x": origin_x, "offset_y": origin_y, "offset_z": 0,
            "global_encoding": LN02_GLOBAL_ENCODING,
        }
        if dst_name.lower().endswith(".laz"):
            writer["compression"] = "laszip"

        # 'filters.stats' haengt sich als reiner Durchlauf-Filter (veraendert keine
        # Punkte) an den ohnehin noetigen Lesedurchlauf und liefert die
        # Classification-Spanne der QUELLE gratis mit - ohne sie ein zweites Mal
        # komplett einzulesen.
        # GpsTime kostet hier nichts extra und entscheidet unten, ob der
        # GPS-Time-Typ ueberhaupt eine Aussage ueber die Daten macht.
        # X/Y/Z liefern im selben Durchlauf die tatsaechlichen Extents der
        # Quellpunkte - Referenz fuer die BBox-Validierung, weil die
        # Header-BBox der Quelle dafuer nicht immer verlaesslich ist.
        # Red/Green/Blue zeigen, ob in den Farbfeldern wirklich Werte stehen.
        stats_dims = "Classification,GpsTime,X,Y,Z"
        if src_has_rgb:
            stats_dims += ",Red,Green,Blue"
        stages = [{"type": "readers.las", "filename": src_path},
                  {"type": "filters.stats", "dimensions": stats_dims},
                  writer]

        with open(pipeline_path, "w", encoding="utf-8") as f:
            json.dump({"pipeline": stages}, f)
        pipe_md = _run_pdal_pipeline(pdal_exe, pipeline_path, meta_path)

        src_class = _stat_range_from_pipeline_metadata(pipe_md, "Classification")
        if src_class == (None, None):
            src_class = _pdal_classification_range(pdal_exe, src_path)
        src_measured = _stats_bbox_from_pipeline_metadata(pipe_md)
        gps_min, gps_max = _stat_range_from_pipeline_metadata(pipe_md, "GpsTime")

        # Farbkontrolle an den DATEN, nicht am Punktformat: PF7 fuehrt die RGB-Felder
        # in jedem Fall, auch wenn nur Nullen darin stehen. Eine still schwarz
        # gewordene Lieferung faellt sonst erst im GDWH auf.
        if not src_has_rgb:
            warnings.append(
                f"{src_name}: Die Quelle ist PF{src_md.get('dataformat_id')} und fuehrt "
                f"keine Farbfelder. Die Zieldatei ist PF{LN02_POINT_FORMAT}, ihre "
                f"RGB-Werte bleiben 0. Stammt die Kachel aus einem GeoSuite-Lauf vor dem "
                f"LAS-1.4-Update, ist die Farbe schon beim Reframe verloren gegangen - "
                f"dann den Tab [LHN95] und den Reframe wiederholen.")
        else:
            rgb_max = max((v for v in (_stat_range_from_pipeline_metadata(pipe_md, c)[1]
                                       for c in ("Red", "Green", "Blue"))
                           if v is not None), default=None)
            if rgb_max is not None and rgb_max == 0:
                warnings.append(
                    f"{src_name}: Die Quelle fuehrt zwar PF{src_md.get('dataformat_id')} "
                    f"mit Farbfeldern, die RGB-Werte sind aber durchgehend 0 - das "
                    f"Ergebnis ist eine schwarze Kachel.")

        # GPS-Time-Typ: nur melden, wenn es die Daten wirklich betrifft.
        if not src_gps_bit:
            if gps_min is None:
                warnings.append(
                    f"{src_name}: global_encoding-Bit 0 (GPS-Time-Typ) ist in der Quelle "
                    f"nicht gesetzt und GpsTime war nicht messbar - die Zieldatei "
                    f"deklariert Adjusted Standard GPS Time (global_encoding "
                    f"{LN02_GLOBAL_ENCODING}, vom Zielformat verlangt).")
            elif gps_min == 0 and gps_max == 0:
                pass  # GpsTime durchgehend 0 - der Typ beschreibt nichts, kein Hinweis noetig
            else:
                warnings.append(
                    f"{src_name}: Die Quelle fuehrt GpsTime-Werte ({gps_min} bis {gps_max}), "
                    f"deklariert per global_encoding-Bit 0 aber GPS Week Time. Die Zieldatei "
                    f"deklariert Adjusted Standard GPS Time (global_encoding "
                    f"{LN02_GLOBAL_ENCODING}, vom Zielformat verlangt) - die WERTE bleiben "
                    f"unveraendert, nur ihre Typ-Angabe aendert sich. Bitte pruefen, welcher "
                    f"Typ fachlich zutrifft.")

        n_stripped = _inject_reference_vlrs(tmp_path)
        if n_stripped:
            warnings.append(f"{src_name}: {n_stripped} aus der Quelle uebernommene(r) "
                             f"CRS-VLR(s) entfernt (nicht autoritativ) - durch die "
                             f"LV95/LN02-Referenz-VLRs ersetzt.")
        dst_md = _pdal_info_metadata(pdal_exe, tmp_path)
        val_warnings = []
        problems = _validate_ln02_target(src_md, dst_md, src_measured, val_warnings)
        warnings.extend(f"{src_name}: {w}" for w in val_warnings)

        # Classification-Kontrolle: PF1/PF3 packen die Klasse als 5-Bit-Wert zusammen
        # mit Flag-Bits in ein Byte, PF6/PF7 trennen beides - genau hier koennte die
        # Punktformat-Umwandlung die Klasse still veraendern.
        try:
            dst_class = _pdal_classification_range(pdal_exe, tmp_path)
            if dst_class != src_class:
                problems.append(f"Classification veraendert: Quelle min/max="
                                 f"{src_class[0]}/{src_class[1]}, Ziel min/max="
                                 f"{dst_class[0]}/{dst_class[1]}")
        except Exception as e:
            problems.append(f"Classification-Pruefung fehlgeschlagen: {e}")

        if problems:
            return ("error", dst_name, "; ".join(problems), warnings)

        os.replace(tmp_path, dst_path)
        tmp_path = None
        return ("written", dst_name, None, warnings)

    except Exception as e:
        return ("error", dst_name, str(e), warnings)
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        for p in (pipeline_path, meta_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _vpc_tile_worker(args) -> tuple:
    """Header-Fakten EINER fertigen Ausgabe-Kachel fuer die VPC (Punktanzahl und
    3D-BBox). Headerbasiert wie _tile_bbox_worker - die Punktdaten werden nicht
    gelesen, bei .laz also auch nichts dekomprimiert."""
    pdal_exe, path = args
    try:
        md = _pdal_info_metadata(pdal_exe, path)
        return (path, int(md.get("count", 0)),
                float(md["minx"]), float(md["miny"]), float(md["minz"]),
                float(md["maxx"]), float(md["maxy"]), float(md["maxz"]), None)
    except Exception as e:
        return (path, None, None, None, None, None, None, None, str(e))


def _vpc_feature(stem: str, href: str, count: int, native_bbox: tuple,
                  lonlat_ring: list, wkt2: str, encoding: str, stamp: str) -> dict:
    """EIN STAC-Feature der VPC. Bewusst ohne 'pc:schemas' und 'proj:geometry':
    beides ist optional (am QGIS-Provider geprueft) und waere nur Ballast - die
    Dimensionsliste stuende sonst fuer jede Kachel identisch in der Datei.

    'native_bbox' ist (minx, miny, minz, maxx, maxy, maxz) in LV95, 'lonlat_ring'
    der geschlossene WGS84-Ring aus denselben Ecken."""
    minx, miny, minz, maxx, maxy, maxz = native_bbox
    lons = [p[0] for p in lonlat_ring]
    lats = [p[1] for p in lonlat_ring]
    return {
        "type": "Feature",
        "stac_version": VPC_STAC_VERSION,
        "stac_extensions": list(VPC_STAC_EXTENSIONS),
        "id": stem,
        "geometry": {"type": "Polygon",
                      "coordinates": [[[lon, lat] for lon, lat in lonlat_ring]]},
        # Reihenfolge nach STAC: [minx, miny, minz, maxx, maxy, maxz] - horizontal
        # in WGS84, vertikal in Metern (die Hoehe wird nicht umgerechnet).
        "bbox": [min(lons), min(lats), minz, max(lons), max(lats), maxz],
        "properties": {
            "datetime": stamp,
            "pc:count": count,
            "pc:type": VPC_POINTCLOUD_TYPE,
            "pc:encoding": encoding,
            "proj:bbox": [minx, miny, minz, maxx, maxy, maxz],
            "proj:wkt2": wkt2,
        },
        "links": [],
        "assets": {"data": {"href": href, "roles": ["data"]}},
    }


def _build_vpc(features: list) -> dict:
    """Die VPC als Ganzes - eine GeoJSON/STAC-FeatureCollection."""
    return {"type": "FeatureCollection", "features": features}


def _write_vpc(tile_paths: list, vpc_path: str, pdal_exe: str, num_workers: int,
                log) -> int:
    """Schreibt die Virtual Point Cloud fuer die uebergebenen Kacheln und gibt die
    Anzahl aufgenommener Kacheln zurueck.

    Die Kachel-Fakten werden aus den Headern der FERTIGEN Ausgabedateien gelesen
    (parallel, ohne die Punktdaten anzufassen) - nicht aus den Job-Ergebnissen. So
    beschreibt die VPC nachweislich das, was im Ordner liegt, statt das, was der
    Lauf zu schreiben glaubte; unveraendert kopierte Kacheln sind damit ebenso
    erfasst wie konvertierte.

    Die Pfade sind RELATIV zum Speicherort der .vpc - der Ordner laesst sich damit
    verschieben oder kopieren, ohne dass die Datei bricht."""
    from osgeo import osr
    osr.UseExceptions()

    # Zielkoordinaten der STAC-Felder: WGS84 in Lon/Lat-Reihenfolge. Ohne
    # TRADITIONAL_GIS_ORDER liefert GDAL 3 bei EPSG:4326 Lat/Lon - die VPC waere
    # dann um 90 Grad "verdreht" und QGIS zeigte die Kacheln irgendwo im Meer.
    lv95 = osr.SpatialReference()
    lv95.ImportFromEPSG(2056)
    lv95.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_wgs84 = osr.CoordinateTransformation(lv95, wgs84)
    # Nur der horizontale Rahmen: die VPC ist eine Kartenebene. Der Hoehenbezug
    # (LN02) steckt in den byte-exakten CRS-VLRs der Kacheln selbst - ein
    # Compound-CRS an dieser Stelle wuerde die Karten-Ansicht nur verwirren.
    wkt2 = lv95.ExportToWkt()

    facts = []
    errors = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_vpc_tile_worker, (pdal_exe, p)) for p in tile_paths]
        for fut in as_completed(futures):
            path, count, minx, miny, minz, maxx, maxy, maxz, err = fut.result()
            if err:
                errors.append((path, err))
            else:
                facts.append((path, count, minx, miny, minz, maxx, maxy, maxz))

    for p, e in errors:
        log(f"  WARNUNG: {Path(p).name} nicht in die VPC aufgenommen "
            f"(Header nicht lesbar): {e}")
    if not facts:
        raise RuntimeError("Keine Kachel fuer die VPC lesbar - Datei nicht geschrieben.")

    facts.sort(key=lambda f: f[0])
    vpc_dir = Path(vpc_path).parent
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    features = []
    for path, count, minx, miny, minz, maxx, maxy, maxz in facts:
        ring_native = [(minx, miny), (minx, maxy), (maxx, maxy), (maxx, miny), (minx, miny)]
        ring_wgs84 = []
        for x, y in ring_native:
            lon, lat, _ = to_wgs84.TransformPoint(x, y)
            ring_wgs84.append((lon, lat))
        href = os.path.relpath(path, str(vpc_dir)).replace("\\", "/")
        if not href.startswith("."):
            href = "./" + href
        encoding = ("application/vnd.laszip" if path.lower().endswith(".laz")
                    else "application/vnd.las")
        features.append(_vpc_feature(Path(path).stem, href, count,
                                      (minx, miny, minz, maxx, maxy, maxz),
                                      ring_wgs84, wkt2, encoding, stamp))

    Path(vpc_path).parent.mkdir(parents=True, exist_ok=True)
    with open(vpc_path, "w", encoding="utf-8") as f:
        json.dump(_build_vpc(features), f, indent=1)
    return len(features)


def _process_las_ln02(cfg: dict) -> None:
    from osgeo import gdal, ogr

    jahr              = str(cfg["jahr"]).strip()
    area              = str(cfg["area"]).strip()
    create_raster     = bool(cfg.get("create_raster", False))
    gsd_raster        = float(cfg["gsd"]) if create_raster else None
    # Virtual Point Cloud fuer QGIS - reines Ansichtsprodukt neben der Lieferung.
    create_vpc        = bool(cfg.get("create_vpc", False))
    input_dir         = cfg["input_dir"]
    output_dir_las    = cfg["output_dir_las"]
    output_dir_raster = cfg.get("output_dir_raster")
    out_format        = cfg.get("out_format", "laz")
    clip_shape_path   = cfg.get("clip_shape_path")
    staging_dir       = cfg["staging_dir"]
    num_workers       = int(cfg.get("num_workers", 6))
    keep_staging      = bool(cfg.get("keep_staging", False))
    pdal_exe          = cfg["pdal_exe"]

    def _log(msg: str) -> None:
        print(msg, flush=True)

    if not pdal_exe or not os.path.isfile(pdal_exe):
        raise FileNotFoundError(
            "pdal.exe wurde nicht gefunden. Bitte pdal (Teil von OSGeo4W/QGIS) "
            "zum System-PATH hinzufuegen.")

    if create_raster and (not clip_shape_path or not os.path.isfile(clip_shape_path)):
        # Frueh pruefen: sonst faellt das erst nach dem kompletten Metadaten-Scan auf.
        raise FileNotFoundError(
            f"AOI/Footprint-Shape fuer die Raster-Maskierung nicht gefunden: {clip_shape_path}")

    gdal.UseExceptions()
    ogr.UseExceptions()

    Path(output_dir_las).mkdir(parents=True, exist_ok=True)
    if create_raster:
        Path(output_dir_raster).mkdir(parents=True, exist_ok=True)
    run_dir = Path(staging_dir) / f"{area}_{jahr}_LN02"
    run_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Staging-Ordner: {run_dir}")
    _log(f"PDAL           : {pdal_exe}")

    last_emit = {"t": 0.0, "p": -1.0}

    def _progress(complete, message, unknown=None):
        try:
            if complete is None:
                return 1
            pct = float(complete)
            now = time.time()
            if (now - last_emit["t"]) >= 1.0 or (pct - last_emit["p"]) >= 0.005:
                print(f"PROGRESS:{0.90 + pct * 0.10:.6f}", flush=True)
                last_emit["t"] = now
                last_emit["p"] = pct
        except Exception:
            pass
        return 1

    # --- Schritt 1: Input-Kacheln finden, Kachelursprung aus dem Namen parsen ---
    tiles = sorted(glob.glob(os.path.join(input_dir, "*.las")) +
                   glob.glob(os.path.join(input_dir, "*.laz")))
    if not tiles:
        raise FileNotFoundError(f"Keine .las/.laz Kacheln gefunden in: {input_dir}")
    _log(f"\nGefundene Input-Kacheln: {len(tiles)}")

    # Alle Namen VOR der eigentlichen Arbeit pruefen: ein nicht parsbarer Name ist ein
    # harter Fehler (der Offset muesste sonst geraten werden) und soll nicht erst nach
    # der halben Verarbeitung auffallen.
    origins = {}
    name_errors = []
    for t in tiles:
        try:
            origins[t] = _parse_tile_origin(os.path.basename(t))
        except ValueError as e:
            name_errors.append(str(e))
    if name_errors:
        raise ValueError("Dateinamen nicht auswertbar:\n  - " + "\n  - ".join(name_errors))

    seen = {}
    duplicates = []
    for t in tiles:
        cell = origins[t]
        if cell in seen:
            duplicates.append(f"{cell[0]}_{cell[1]}: {os.path.basename(seen[cell])} / "
                               f"{os.path.basename(t)}")
        else:
            seen[cell] = t
    if duplicates:
        raise ValueError("Mehrere Input-Kacheln zeigen auf dieselbe Gitterzelle - die "
                          "Ausgabedateien wuerden sich gegenseitig ueberschreiben:\n  - "
                          + "\n  - ".join(duplicates))

    # --- Schritt 2: Bounding Boxes parallel einlesen (Gesamt-Extent + Raster-Zellen) ---
    _log("Lese Metadaten (Bounding Box) aller Kacheln...")
    tile_bboxes = []
    meta_errors = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_tile_bbox_worker, (pdal_exe, t)) for t in tiles]
        for i, fut in enumerate(as_completed(futures), 1):
            path, minx, miny, maxx, maxy, _genc, err = fut.result()
            if err:
                meta_errors.append((path, err))
            else:
                tile_bboxes.append((path, minx, miny, maxx, maxy))
            if i == len(tiles) or i % max(1, len(tiles) // 100) == 0:
                print(f"PROGRESS:{(i / len(tiles)) * 0.10:.6f}", flush=True)

    for p, e in meta_errors:
        _log(f"  WARNUNG: Metadaten von {Path(p).name} nicht lesbar: {e}")
    if not tile_bboxes:
        raise RuntimeError("Keine gueltigen Kachel-Metadaten gefunden.")

    all_minx = min(b[1] for b in tile_bboxes)
    all_miny = min(b[2] for b in tile_bboxes)
    all_maxx = max(b[3] for b in tile_bboxes)
    all_maxy = max(b[4] for b in tile_bboxes)
    _log(f"  Gesamt-Extent Input: {all_minx:.1f}, {all_miny:.1f} - {all_maxx:.1f}, {all_maxy:.1f}")

    _log(f"\nZielformat          : LAS 1.4 / PF{LN02_POINT_FORMAT} (mit RGB), "
         f"global_encoding {LN02_GLOBAL_ENCODING}, scale {LN02_SCALE}, "
         f"Offset = Kachelursprung")
    _log(f"CRS-Tag             : LV95 + LN02 (EPSG:2056+5728), byte-exakte "
         f"Referenz-VLRs 34735 + 2112")
    # Die Farbe kommt unveraendert aus der Quelle. Fuehrt die schon keine, wird das
    # pro Kachel als Warnung gemeldet - hier die Stichprobe auf der ersten Kachel,
    # damit es vor dem Lauf sichtbar ist und nicht erst danach.
    try:
        src_fmt0 = _pdal_info_metadata(pdal_exe, tiles[0]).get("dataformat_id")
        if src_fmt0 in PC_FORMATS_WITH_RGB:
            _log(f"Farbe (RGB)         : Quelle ist PF{src_fmt0} und fuehrt Farbfelder - "
                 f"sie werden unveraendert uebernommen.")
        else:
            _log(f"  WARNUNG: Die Quelle ist PF{src_fmt0} und fuehrt KEINE Farbfelder. Die "
                 f"Zieldateien werden trotzdem PF{LN02_POINT_FORMAT}, ihre RGB-Werte "
                 f"bleiben 0. Wurde der Reframe mit einer GeoSuite-Version vor dem "
                 f"LAS-1.4-Update gemacht?")
    except Exception as e:
        _log(f"  WARNUNG: Punktformat der Quelle nicht lesbar ({e}) - RGB-Hinweis "
             f"uebersprungen.")
    _log(f"Punktwolken-Format  : .{out_format}")
    _log(f"LAS-Dataset / VPC   : "
         + (f"AKTIV - {VPC_SUBDIR}/{jahr}_{area}_LV95_LN02.vpc (fuer QGIS)"
            if create_vpc else "inaktiv"))
    _log(f"Raster erstellen    : "
         f"{'AKTIV (GSD ' + format(gsd_raster, 'g') + ' m)' if create_raster else 'inaktiv'}")
    _log(f"Benennung           : {jahr}_{area}_TIN_[thinnedout<NN>_]raw_<E>_<N>_LV95_LN02."
         f"{out_format}")
    _log(f"                      ([thinnedout<NN>_] und <E>_<N> aus dem Quell-Dateinamen)")

    # --- Schritt 3: Zielnamen fuer DSM/Hillshade, nur falls aktiviert ---
    raster_name = hillshade_name = None
    raster_out_path = hillshade_out_path = None
    cells_dir = None
    snap_bounds = None
    if create_raster:
        gsd_label = f"{round(gsd_raster * 100)}cm"
        raster_name = f"{jahr}_{area}_DSM_{gsd_label}_LV95_LN02.tif"
        hillshade_name = f"{jahr}_{area}_hillshade_{gsd_label}_LV95_LN02.tif"
        raster_out_path = str(Path(output_dir_raster) / raster_name)
        hillshade_out_path = str(Path(output_dir_raster) / hillshade_name)
        # Pixelursprung auf ein sauberes GSD-Vielfaches snappen (keine AOI-Kante im Grid)
        snap_bounds = ((all_minx // gsd_raster) * gsd_raster,
                       (all_miny // gsd_raster) * gsd_raster,
                       math.ceil(all_maxx / gsd_raster) * gsd_raster,
                       math.ceil(all_maxy / gsd_raster) * gsd_raster)
        cells_dir = run_dir / "03_raster_cells"
        cells_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Raster-Benennung    : {raster_name}  (+ .tfw)")
        _log(f"Hillshade-Benennung : {hillshade_name}  (+ .tfw)")
        _log(f"AOI/Footprint-Shape : {clip_shape_path}")

    # --- Schritt 4: Jobs aufbauen (Punktwolken-Kachel + optional DSM-Zelle) ---
    jobs = []
    for t in tiles:
        easting_km, northing_km = origins[t]
        cell = f"{easting_km}_{northing_km}"
        thin_token = _parse_thin_token(os.path.basename(t))
        stem = f"{jahr}_{area}_TIN_{thin_token}raw_{cell}_LV95_LN02"
        job = {"src": t, "cell": cell, "stem": stem,
               "origin": (easting_km * 1000.0, northing_km * 1000.0)}
        if create_raster:
            # Nominalen 1km-Rahmen auf das globale, gesnappte GSD-Raster legen, damit
            # sich die Zell-Raster luecken- und ueberlappungsfrei mosaikieren lassen.
            ox, oy = snap_bounds[0], snap_bounds[1]
            cminx, cminy = easting_km * 1000.0, northing_km * 1000.0
            cmaxx, cmaxy = cminx + 1000.0, cminy + 1000.0
            job["raster_bounds"] = (
                ox + math.floor((cminx - ox) / gsd_raster + 1e-6) * gsd_raster,
                oy + math.floor((cminy - oy) / gsd_raster + 1e-6) * gsd_raster,
                ox + math.ceil((cmaxx - ox) / gsd_raster - 1e-6) * gsd_raster,
                oy + math.ceil((cmaxy - oy) / gsd_raster - 1e-6) * gsd_raster,
            )
            # Beitragende Kacheln: alles, was den GEPUFFERTEN Zellausschnitt beruehrt.
            # Ohne den Puffer waere das genau eine Kachel (der Input ist ja bereits
            # exakt 1km-gekachelt) und die IDW-Nachbarschaft am Zellrand bliebe
            # einseitig - sichtbare Naht an jeder Kilometergrenze.
            rb = job["raster_bounds"]
            buf = _raster_cell_buffer(gsd_raster, None)
            job["tiles"] = [b[0] for b in tile_bboxes
                            if not (b[3] <= rb[0] - buf or b[1] >= rb[2] + buf or
                                    b[4] <= rb[1] - buf or b[2] >= rb[3] + buf)]
            job["srs"] = LAS_LN02_SRS
        jobs.append(job)

    # --- Schritt 5: Kacheln (+ DSM-Zellen) parallel verarbeiten ---
    # Beide Job-Arten laufen im selben Pool - so sind nie mehr pdal.exe-Prozesse
    # gleichzeitig aktiv als unter "CPU-Kerne" eingestellt.
    cell_workers = {"ln02": _ln02_tile_worker, "dsm": _raster_cell_worker}
    cell_tasks = [("ln02", job["stem"],
                   (job["src"], str(Path(output_dir_las) / f"{job['stem']}.{out_format}"),
                    pdal_exe, str(run_dir), job["origin"][0], job["origin"][1]))
                  for job in jobs]
    if create_raster:
        cell_tasks += [("dsm", job["cell"],
                        (job, str(run_dir), str(cells_dir), pdal_exe, None, gsd_raster))
                       for job in jobs]

    total_tasks = len(cell_tasks)
    _log(f"\nStarte parallele Verarbeitung: {total_tasks} Job(s) auf {num_workers} Prozess(en)"
         + (f" ({len(jobs)} Punktwolken-Kachel(n) + {len(jobs)} DSM-Zelle(n))"
            if create_raster else "")
         + "\n")

    def _job_label(kind: str, name: str) -> str:
        return f"{name}.{out_format}" if kind == "ln02" else f"DSM-Zelle {name}"

    written = copied = errors = 0
    dsm_written = dsm_empty = 0
    done = 0
    failed = []
    errors_by_kind = {"ln02": 0, "dsm": 0}
    progress_start = 0.10
    progress_span = (0.90 if create_raster else 1.0) - progress_start

    def _handle(kind: str, name: str, res: tuple, prefix: str) -> bool:
        """Ergebnis eines Jobs auswerten, Warnungen und Status loggen. Gibt True
        zurueck, wenn der Job erledigt ist (auch 'leer'), False bei Fehler."""
        nonlocal written, copied, dsm_written, dsm_empty
        status = res[0]
        for w in (res[3] if len(res) > 3 else []):
            _log(f"      WARNUNG: {w}")
        label = _job_label(kind, name)
        if status == "written":
            if kind == "ln02":
                written += 1
            else:
                dsm_written += 1
            _log(f"  {prefix} {label}")
            return True
        if status == "copied":
            copied += 1
            _log(f"  {prefix} {label}  (war bereits LAS 1.4/LN02 - unveraendert kopiert)")
            return True
        if status == "empty":
            dsm_empty += 1
            _log(f"  {prefix} {label} - uebersprungen (keine Punkte)")
            return True
        return False

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(cell_workers[kind], args): (kind, name, args)
                   for kind, name, args in cell_tasks}
        for future in as_completed(futures):
            kind, name, args = futures[future]
            res = future.result()
            done += 1
            prefix = f"[{done}/{total_tasks}]"
            if not _handle(kind, name, res, prefix):
                # Noch nicht als Fehler zaehlen: ein abgestuerzter pdal-Prozess ist
                # meist Speicherdruck durch die parallelen Jobs - wird unten seriell
                # wiederholt.
                failed.append((kind, name, args))
                _log(f"  {prefix} FEHLER bei {_job_label(kind, name)} "
                     f"(Wiederholung folgt): {res[2]}")
            print(f"PROGRESS:{progress_start + (done / total_tasks) * progress_span:.6f}",
                  flush=True)

    # --- Fehlgeschlagene Jobs seriell wiederholen (voller RAM pro pdal-Prozess) ---
    if failed:
        _log(f"\nWiederhole {len(failed)} fehlgeschlagene(n) Job(s) seriell "
             f"(ein pdal-Prozess nach dem anderen)...")
        for i, (kind, name, args) in enumerate(failed, 1):
            res = cell_workers[kind](args)
            if not _handle(kind, name, res, f"[Retry {i}/{len(failed)}]"):
                errors += 1
                errors_by_kind[kind] += 1
                _log(f"  [Retry {i}/{len(failed)}] FEHLER bleibt bei "
                     f"{_job_label(kind, name)}: {res[2]}")

    # --- Schritt 5b: Virtual Point Cloud (QGIS) ---
    # Bewusst NACH der Konversion und aus dem Ordner-Inhalt heraus: die VPC soll
    # beschreiben, was tatsaechlich ausgeliefert wird. Ein Fehler hier darf den Lauf
    # nicht scheitern lassen - die Kacheln sind das Produkt, die VPC nur die Ansicht.
    if create_vpc:
        vpc_tiles = sorted(glob.glob(os.path.join(output_dir_las, f"*.{out_format}")))
        _log(f"\nVirtual Point Cloud (QGIS): {len(vpc_tiles)} Kachel(n) im Output-Ordner")
        if not vpc_tiles:
            _log(f"  WARNUNG: keine .{out_format}-Kachel im Output-Ordner - "
                 f"keine VPC geschrieben.")
        else:
            vpc_path = str(Path(output_dir_las) / VPC_SUBDIR /
                           f"{jahr}_{area}_LV95_LN02.vpc")
            try:
                n_vpc = _write_vpc(vpc_tiles, vpc_path, pdal_exe, num_workers, _log)
                _log(f"  Geschrieben: {vpc_path}")
                _log(f"  {n_vpc} Kachel(n) referenziert (relative Pfade). In QGIS als "
                     f"eine Punktwolken-Ebene zu oeffnen; ArcGIS Pro liest das Format nicht.")
            except Exception as e:
                _log(f"  WARNUNG: VPC konnte nicht geschrieben werden ({e}) - die "
                     f"Kacheln selbst sind davon nicht betroffen.")

    # --- Schritt 6: Zell-Raster zum Gesamt-DSM mosaikieren, dann Hillshade ---
    if create_raster:
        if errors_by_kind["dsm"]:
            _log(f"\nWARNUNG: {errors_by_kind['dsm']} DSM-Zelle(n) fehlgeschlagen - das "
                 f"Gesamt-Raster erhaelt dort Loecher (NoData). Siehe Fehler oben.")
        cell_rasters = sorted(str(p) for p in cells_dir.glob("dsm_*.tif"))
        if not cell_rasters:
            raise RuntimeError("Keine DSM-Zelle wurde erzeugt - Gesamt-Raster nicht moeglich.")
        _mosaic_las_raster(cell_rasters, run_dir, raster_out_path, hillshade_out_path,
                            gsd_raster, clip_shape_path, snap_bounds, str(num_workers),
                            _log, _progress)

    if not keep_staging:
        _log(f"\nRaeume Staging-Ordner auf: {run_dir}")
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass
    else:
        _log(f"\nStaging-Dateien bleiben erhalten: {run_dir}")

    raster_line = (f"Raster: {raster_name}, Hillshade: {hillshade_name}\n"
                   f"DSM-Zellen: {dsm_written} gerastert, {dsm_empty} leer (0 Punkte).\n"
                   if raster_name else "Raster: nicht erstellt (Option deaktiviert)\n")
    _log(f"\nFertig. {raster_line}"
         f".{out_format}: {written} Kachel(n) konvertiert, {copied} unveraendert kopiert "
         f"(waren bereits LAS 1.4/LN02).\n"
         f"Fehler gesamt: {errors}.")
    if (written + copied) == 0:
        raise RuntimeError("Keine Punktwolken-Kachel wurde geschrieben.")
    if errors:
        raise RuntimeError(f"{errors} Job(s) konnten auch beim seriellen Wiederholen nicht "
                            f"verarbeitet werden - siehe Log.")


# ─── Create DSM-Raster (eigenstaendiger Raster-Build) ──────────────────────────
#
# Rastert einen beliebigen Ordner mit LAS/LAZ-Kacheln zu EINEM DSM + Hillshade.
# Gegenstueck zur Raster-Option der beiden Konverter-Tabs, nur ohne deren
# Punktwolken-Verarbeitung: kein Re-Tiling, kein Thinning, kein Crop der Punkte.
#
# Unterschied zu den anderen Tabs beim Zellschnitt: die brauchen ihre Zellen ohnehin
# fuer die Punktwolken-Ausgabe und holen sie deshalb aus dem Grid-Shape bzw. aus den
# Dateinamen. Hier sind die Zellen NUR ein Mittel zur Parallelisierung und
# Speicherbegrenzung (ein Gesamt-Merge ueber alle Kacheln sprengt bei grossen
# Projekten den RAM). Deshalb wird das 1km-Raster direkt aus dem Gesamt-Extent
# abgeleitet - kein Grid-Shape noetig, und die Kachelnamen muessen keiner Konvention
# folgen. Das Ergebnis ist identisch, weil ohnehin mosaikiert wird.
def _dsm_cell_jobs(tile_bboxes, snap_bounds: tuple, gsd: float, srs: str) -> list:
    """Zerlegt den Datenbereich in 1km-Arbeitszellen fuer den Tab "Create DSM-Raster".

    Anders als in den Konverter-Tabs sind die Zellen hier KEINE Ausgabegeometrie,
    sondern nur ein Mittel zur Parallelisierung und Speicherbegrenzung (ein
    Gesamt-Merge ueber alle Kacheln sprengt bei grossen Projekten den RAM). Sie
    werden am Ende ohnehin mosaikiert.

    Zwei Eigenschaften, auf die es ankommt:
      - Die Zellen werden auf 'snap_bounds' BESCHNITTEN. Ohne das rastert eine
        Kachel, die nur in einer Ecke ihrer Kilometerzelle liegt, trotzdem den
        ganzen Quadratkilometer; das anschliessende Fuellen der NoData-Loecher
        laeuft dann ueber eine riesige leere Flaeche (an einem Testdatensatz
        gemessen: 7.7 Mio. NoData-Pixel und >120 s statt 0 Pixel und ~1 s).
      - Jede Zelle bekommt die Kacheln, die ihren GEPUFFERTEN Ausschnitt beruehren.
        Der Puffer haelt die IDW-Nachbarschaft am Zellrand vollstaendig - sonst
        bleibt sie einseitig und an jeder Zellgrenze entsteht eine sichtbare Naht.

    'tile_bboxes' ist [(pfad, minx, miny, maxx, maxy), ...]. Zurueck kommen die
    Jobs fuer _raster_cell_worker; Zellen ohne beitragende Kachel entfallen."""
    buf = _raster_cell_buffer(gsd, None)
    ox, oy = snap_bounds[0], snap_bounds[1]
    all_minx = min(b[1] for b in tile_bboxes)
    all_miny = min(b[2] for b in tile_bboxes)
    all_maxx = max(b[3] for b in tile_bboxes)
    all_maxy = max(b[4] for b in tile_bboxes)

    jobs = []
    for e_km in range(int(math.floor(all_minx / 1000.0)), int(math.ceil(all_maxx / 1000.0))):
        for n_km in range(int(math.floor(all_miny / 1000.0)), int(math.ceil(all_maxy / 1000.0))):
            cminx = max(e_km * 1000.0, snap_bounds[0])
            cmaxx = min(e_km * 1000.0 + 1000.0, snap_bounds[2])
            cminy = max(n_km * 1000.0, snap_bounds[1])
            cmaxy = min(n_km * 1000.0 + 1000.0, snap_bounds[3])
            if cmaxx <= cminx or cmaxy <= cminy:
                continue
            # Auf das globale, gesnappte GSD-Raster legen, damit sich die
            # Zell-Raster luecken- und ueberlappungsfrei mosaikieren lassen. Das
            # Epsilon faengt Float-Rauschen ab (sonst gelegentlich eine
            # Pixelspalte Ueberlappung).
            rb = (ox + math.floor((cminx - ox) / gsd + 1e-6) * gsd,
                  oy + math.floor((cminy - oy) / gsd + 1e-6) * gsd,
                  ox + math.ceil((cmaxx - ox) / gsd - 1e-6) * gsd,
                  oy + math.ceil((cmaxy - oy) / gsd - 1e-6) * gsd)
            cell_tiles = [b[0] for b in tile_bboxes
                          if not (b[3] <= rb[0] - buf or b[1] >= rb[2] + buf or
                                  b[4] <= rb[1] - buf or b[2] >= rb[3] + buf)]
            if not cell_tiles:
                continue
            jobs.append({"cell": f"{e_km}_{n_km}", "raster_bounds": rb,
                         "tiles": cell_tiles, "srs": srs})
    return jobs


def _process_dsm(cfg: dict) -> None:
    from osgeo import gdal, ogr

    jahr              = str(cfg["jahr"]).strip()
    area              = str(cfg["area"]).strip()
    input_dir         = cfg["input_dir"]
    output_dir_raster = cfg["output_dir_raster"]
    clip_shape_path   = cfg["clip_shape_path"]
    gsd_raster        = float(cfg["gsd"])
    # Nur fuer die Benennung und den SRS-Tag der Reader - gerastert wird in beiden
    # Faellen identisch (die Hoehe wird nirgends umgerechnet, siehe _mosaic_las_raster).
    height_ref        = str(cfg.get("height_ref", "LHN95")).strip().upper()
    staging_dir       = cfg["staging_dir"]
    num_workers       = int(cfg.get("num_workers", 6))
    keep_staging      = bool(cfg.get("keep_staging", False))
    pdal_exe          = cfg["pdal_exe"]

    def _log(msg: str) -> None:
        print(msg, flush=True)

    if height_ref not in ("LHN95", "LN02"):
        raise ValueError(f"Hoehenbezug '{height_ref}' unbekannt - erwartet LHN95 oder LN02.")
    if not pdal_exe or not os.path.isfile(pdal_exe):
        raise FileNotFoundError(
            "pdal.exe wurde nicht gefunden. Bitte pdal (Teil von OSGeo4W/QGIS) "
            "zum System-PATH hinzufuegen.")
    if not clip_shape_path or not os.path.isfile(clip_shape_path):
        raise FileNotFoundError(
            f"AOI/Footprint-Shape fuer die Raster-Maskierung nicht gefunden: {clip_shape_path}")

    gdal.UseExceptions()
    ogr.UseExceptions()

    Path(output_dir_raster).mkdir(parents=True, exist_ok=True)
    run_dir = Path(staging_dir) / f"{area}_{jahr}_DSM"
    run_dir.mkdir(parents=True, exist_ok=True)
    cells_dir = run_dir / "03_raster_cells"
    cells_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Staging-Ordner: {run_dir}")
    _log(f"PDAL           : {pdal_exe}")

    last_emit = {"t": 0.0, "p": -1.0}

    def _progress(complete, message, unknown=None):
        try:
            if complete is None:
                return 1
            pct = float(complete)
            now = time.time()
            if (now - last_emit["t"]) >= 1.0 or (pct - last_emit["p"]) >= 0.005:
                print(f"PROGRESS:{0.90 + pct * 0.10:.6f}", flush=True)
                last_emit["t"] = now
                last_emit["p"] = pct
        except Exception:
            pass
        return 1

    # --- Schritt 1: Input-Kacheln + Bounding Boxes (parallel, headerbasiert) ---
    tiles = sorted(glob.glob(os.path.join(input_dir, "*.laz")) +
                   glob.glob(os.path.join(input_dir, "*.las")))
    if not tiles:
        raise FileNotFoundError(f"Keine .laz/.las Kacheln gefunden in: {input_dir}")
    _log(f"\nGefundene Input-Kacheln: {len(tiles)}")

    _log("Lese Metadaten (Bounding Box) aller Kacheln...")
    tile_bboxes = []
    meta_errors = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_tile_bbox_worker, (pdal_exe, t)) for t in tiles]
        for i, fut in enumerate(as_completed(futures), 1):
            path, minx, miny, maxx, maxy, _genc, err = fut.result()
            if err:
                meta_errors.append((path, err))
            else:
                tile_bboxes.append((path, minx, miny, maxx, maxy))
            if i == len(tiles) or i % max(1, len(tiles) // 100) == 0:
                print(f"PROGRESS:{(i / len(tiles)) * 0.10:.6f}", flush=True)

    for p, e in meta_errors:
        _log(f"  WARNUNG: Metadaten von {Path(p).name} nicht lesbar: {e}")
    if not tile_bboxes:
        raise RuntimeError("Keine gueltigen Kachel-Metadaten gefunden.")

    all_minx = min(b[1] for b in tile_bboxes)
    all_miny = min(b[2] for b in tile_bboxes)
    all_maxx = max(b[3] for b in tile_bboxes)
    all_maxy = max(b[4] for b in tile_bboxes)
    _log(f"  Gesamt-Extent Input: {all_minx:.1f}, {all_miny:.1f} - "
         f"{all_maxx:.1f}, {all_maxy:.1f}")

    # --- Schritt 2: Zielnamen und gesnapptes Zielgitter ---
    gsd_label = f"{round(gsd_raster * 100)}cm"
    raster_name = f"{jahr}_{area}_DSM_{gsd_label}_LV95_{height_ref}.tif"
    hillshade_name = f"{jahr}_{area}_hillshade_{gsd_label}_LV95_{height_ref}.tif"
    raster_out_path = str(Path(output_dir_raster) / raster_name)
    hillshade_out_path = str(Path(output_dir_raster) / hillshade_name)
    # Pixelursprung auf ein sauberes GSD-Vielfaches snappen (keine AOI-Kante im Grid)
    snap_bounds = ((all_minx // gsd_raster) * gsd_raster,
                   (all_miny // gsd_raster) * gsd_raster,
                   math.ceil(all_maxx / gsd_raster) * gsd_raster,
                   math.ceil(all_maxy / gsd_raster) * gsd_raster)
    src_srs = LAS_LN02_SRS if height_ref == "LN02" else LAS_INPUT_SRS

    _log(f"\nRaster-Aufloesung   : {format(gsd_raster, 'g')} m")
    _log(f"Hoehenbezug         : {height_ref}  (nur Benennung und SRS-Tag der Reader - "
         f"die Z-Werte werden nirgends umgerechnet)")
    _log(f"SRS der Eingabe     : {src_srs}  (den Readern aufgezwungen)")
    _log(f"Raster-Benennung    : {raster_name}  (+ .tfw)")
    _log(f"Hillshade-Benennung : {hillshade_name}  (+ .tfw)")
    _log(f"AOI/Footprint-Shape : {clip_shape_path}")

    # --- Schritt 3: 1km-Zellen aus dem Extent ableiten ---
    jobs = _dsm_cell_jobs(tile_bboxes, snap_bounds, gsd_raster, src_srs)
    if not jobs:
        raise RuntimeError("Keine Zelle mit Punkten - Input-Ordner pruefen.")
    _log(f"\nStarte parallele Verarbeitung: {len(jobs)} DSM-Zelle(n) auf "
         f"{num_workers} Prozess(en)\n")

    # --- Schritt 4: Zellen parallel rastern, Fehler seriell wiederholen ---
    written = empty = errors = done = 0
    failed = []
    progress_start, progress_span = 0.10, 0.80

    def _handle(cell, res, prefix) -> bool:
        nonlocal written, empty
        if res[0] == "written":
            written += 1
            _log(f"  {prefix} DSM-Zelle {cell}")
            return True
        if res[0] == "empty":
            empty += 1
            _log(f"  {prefix} DSM-Zelle {cell} - uebersprungen (keine Punkte)")
            return True
        return False

    tasks = [(job["cell"], (job, str(run_dir), str(cells_dir), pdal_exe, None, gsd_raster))
             for job in jobs]
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_raster_cell_worker, args): (cell, args)
                   for cell, args in tasks}
        for fut in as_completed(futures):
            cell, args = futures[fut]
            res = fut.result()
            done += 1
            prefix = f"[{done}/{len(tasks)}]"
            if not _handle(cell, res, prefix):
                # Noch nicht als Fehler zaehlen: ein abgestuerzter pdal-Prozess ist
                # meist Speicherdruck durch die parallelen Jobs - wird unten seriell
                # wiederholt.
                failed.append((cell, args))
                _log(f"  {prefix} FEHLER bei DSM-Zelle {cell} "
                     f"(Wiederholung folgt): {res[2]}")
            print(f"PROGRESS:{progress_start + (done / len(tasks)) * progress_span:.6f}",
                  flush=True)

    if failed:
        _log(f"\nWiederhole {len(failed)} fehlgeschlagene(n) Job(s) seriell "
             f"(ein pdal-Prozess nach dem anderen)...")
        for i, (cell, args) in enumerate(failed, 1):
            res = _raster_cell_worker(args)
            if not _handle(cell, res, f"[Retry {i}/{len(failed)}]"):
                errors += 1
                _log(f"  [Retry {i}/{len(failed)}] FEHLER bleibt bei DSM-Zelle "
                     f"{cell}: {res[2]}")

    # --- Schritt 5: Zell-Raster zum Gesamt-DSM mosaikieren, dann Hillshade ---
    if errors:
        _log(f"\nWARNUNG: {errors} DSM-Zelle(n) fehlgeschlagen - das Gesamt-Raster "
             f"erhaelt dort Loecher (NoData). Siehe Fehler oben.")
    cell_rasters = sorted(str(p) for p in cells_dir.glob("dsm_*.tif"))
    if not cell_rasters:
        raise RuntimeError("Keine DSM-Zelle wurde erzeugt - Gesamt-Raster nicht moeglich.")
    _mosaic_las_raster(cell_rasters, run_dir, raster_out_path, hillshade_out_path,
                        gsd_raster, clip_shape_path, snap_bounds, str(num_workers),
                        _log, _progress)

    if not keep_staging:
        _log(f"\nRaeume Staging-Ordner auf: {run_dir}")
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass
    else:
        _log(f"\nStaging-Dateien bleiben erhalten: {run_dir}")

    _log(f"\nFertig. Raster: {raster_name}, Hillshade: {hillshade_name}\n"
         f"DSM-Zellen: {written} gerastert, {empty} leer (0 Punkte).\n"
         f"Fehler gesamt: {errors}.")
    if errors:
        raise RuntimeError(f"{errors} DSM-Zelle(n) konnten auch beim seriellen "
                            f"Wiederholen nicht verarbeitet werden - siehe Log.")


def main() -> None:
    if len(sys.argv) < 2:
        print("[FEHLER] Kein Konfigurationspfad uebergeben.", flush=True)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)

    action = cfg.get("action", "")

    from osgeo import gdal
    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")

    try:
        if action == "info":
            _info(cfg)
        elif action == "process":
            _process(cfg)
        elif action == "process_las":
            _process_las(cfg)
        elif action == "process_las_ln02":
            _process_las_ln02(cfg)
        elif action == "process_dsm":
            _process_dsm(cfg)
        else:
            print(f"[FEHLER] Unbekannte Aktion: '{action}'", flush=True)
            sys.exit(1)

    except Exception as e:
        print(f"\n[FEHLER] {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
