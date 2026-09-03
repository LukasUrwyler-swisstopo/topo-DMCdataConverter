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
"""

import sys
import os
import glob
import json
import shutil
import subprocess
import traceback
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

# NoData-Sentinel fuer Float32-DSM-Raster, analog GDWH-Konvention bei SB_DSM (Raster, nicht Hillshade)
LAS_RASTER_NODATA = -3.4028235e+38

# Erwartetes SRS der Input-.laz-Kacheln (LV95 + LHN95). Wird den Readern explizit
# aufgezwungen (override_srs), damit eine Kachel mit fehlendem/falschem SRS-Tag
# nicht still mit einer abweichenden Referenz in den Merge einfliesst.
LAS_INPUT_SRS = "EPSG:2056+5729"


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
     compress, blocksize, nodata_val) = args
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

    def _log(msg: str) -> None:
        print(msg, flush=True)

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
    _log(f"Kompression         : {compress} (von Input-Kacheln uebernommen, verlustfrei)")
    _log(f"Blockgroesse        : {blocksize}")
    _log(f"Parallele Prozesse  : {num_workers}")

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
                     compress, blocksize, nodata_val))

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
#   2) Raster (EIN Gesamt-TIFF fuer die AOI), nur falls "Create Raster" aktiv -
#      laeuft als Hintergrund-Thread PARALLEL zu Schritt 3 (nicht seriell davor):
#      a) alle Input-Kacheln mergen, optional thinnen
#      b) als Float32-Raster rastern (PDAL writers.gdal, IDW), Pixelursprung
#         auf ein sauberes GSD-Vielfaches gesnappt (keine AOI-Kante im Grid)
#      c) per AOI-Shape maskieren (gdal.Warp Cutline, NoData ausserhalb)
#   3) Punktwolken-Kacheln (pro 1km-Grid-Kachel):
#      pro Grid-Zelle die ueberlappenden Input-Kacheln mergen, per AOI-Polygon
#      croppen, optional thinnen, als .las oder .laz schreiben (out_format,
#      Default .las - wird u.a. fuer GeoSuite-Reframe LHN95->LN02 benoetigt)
#      (parallelisiert ueber mehrere Prozesse, analog TIFFconverter)
#   Vor dem Staging-Aufraeumen wird auf den Raster-Hintergrund-Thread gewartet
#   (Schritt 2 schreibt Zwischendateien in denselben Staging-Ordner).
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


def _tile_bbox_worker(args) -> tuple:
    pdal_exe, path = args
    try:
        meta = _pdal_info_metadata(pdal_exe, path)
        return (path, meta["minx"], meta["miny"], meta["maxx"], meta["maxy"], None)
    except Exception as e:
        return (path, None, None, None, None, str(e))


def _run_pdal_pipeline(pdal_exe: str, pipeline_path: Path) -> None:
    result = subprocess.run([pdal_exe, "pipeline", str(pipeline_path)],
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


def _las_cell_worker(args) -> tuple:
    """Wird in einem eigenen Prozess ausgefuehrt - mergt die Input-Kacheln einer
    1km-Grid-Zelle, croppt/thinnt optional, schreibt eine Punktwolken-Kachel
    (.las oder .laz, siehe out_format)."""
    (job, run_dir_str, output_dir_laz, pdal_exe, clip_wkt, thin_m, out_format) = args

    stem = job["stem"]
    cminx, cminy, cmaxx, cmaxy = job["cell_bounds"]
    tiles = job["tiles"]

    run_dir = Path(run_dir_str)
    pipeline_path = run_dir / f"pipeline_{stem}.json"
    laz_out = str(Path(output_dir_laz) / f"{stem}.{out_format}")

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
                    "scale_x": 0.01, "scale_y": 0.01, "scale_z": 0.01})

    try:
        with open(pipeline_path, "w", encoding="utf-8") as f:
            json.dump({"pipeline": stages}, f)
        _run_pdal_pipeline(pdal_exe, pipeline_path)

        if not os.path.isfile(laz_out):
            return ("empty", stem, None)

        meta = _pdal_info_metadata(pdal_exe, laz_out)
        if int(meta.get("count", 0)) == 0:
            try:
                os.remove(laz_out)
            except OSError:
                pass
            return ("empty", stem, None)

        return ("written", stem, None)
    except Exception as e:
        return ("error", stem, str(e))
    finally:
        try:
            pipeline_path.unlink(missing_ok=True)
        except Exception:
            pass


def _build_las_raster(tiles, run_dir: Path, output_path: str, hillshade_output_path: str,
                       pdal_exe: str, gsd: float, thin_m, clip_shape_path: str,
                       all_bounds: tuple, num_threads: str, log, progress) -> None:
    """Ein Gesamt-Raster (DSM) fuer die AOI: alle Kacheln mergen -> optional thinnen ->
    rastern (IDW, Pixelursprung auf GSD-Vielfaches gesnapped) -> per AOI-Shape
    maskieren (NoData ausserhalb, Cutline wie im TIFFconverter). Danach wird aus dem
    fertigen (bereits geclippten) DSM ein Hillshade gerechnet und ebenfalls per
    AOI-Shape maskiert (NoData=255)."""
    import math
    from osgeo import gdal
    gdal.UseExceptions()

    all_minx, all_miny, all_maxx, all_maxy = all_bounds
    snap_minx = (all_minx // gsd) * gsd
    snap_miny = (all_miny // gsd) * gsd
    snap_maxx = math.ceil(all_maxx / gsd) * gsd
    snap_maxy = math.ceil(all_maxy / gsd) * gsd
    bounds_str = f"([{snap_minx:.3f},{snap_maxx:.3f}],[{snap_miny:.3f},{snap_maxy:.3f}])"
    log(f"  Raster-Grid (auf {gsd:g}m gesnapped): {snap_minx:.2f}, {snap_miny:.2f} - "
        f"{snap_maxx:.2f}, {snap_maxy:.2f}")

    stages = []
    tags = []
    for i, t in enumerate(tiles):
        tag = f"r{i}"
        stages.append({"type": "readers.las", "filename": t, "tag": tag,
                        "override_srs": LAS_INPUT_SRS})
        tags.append(tag)
    stages.append({"type": "filters.merge", "inputs": tags})

    if thin_m:
        stages.append({"type": "filters.sample", "radius": float(thin_m)})

    raw_raster_path = run_dir / "03_raster_merged_raw.tif"
    stages.append({
        "type": "writers.gdal",
        "filename": str(raw_raster_path),
        "resolution": float(gsd),
        "output_type": "idw",
        "gdaldriver": "GTiff",
        "data_type": "float32",
        "bounds": bounds_str,
        "nodata": LAS_RASTER_NODATA,
    })

    pipeline_path = run_dir / "pipeline_raster.json"
    with open(pipeline_path, "w", encoding="utf-8") as f:
        json.dump({"pipeline": stages}, f)

    log(f"\nErzeuge Gesamt-Raster aus {len(tiles)} Kachel(n) (PDAL, IDW, {gsd:g}m)... "
        f"(ein PDAL-Lauf ohne Fortschrittsanzeige, kann bei grossen Projekten "
        f"mehrere Minuten dauern - kein Einfrieren)")
    _run_pdal_pipeline(pdal_exe, pipeline_path)
    if not raw_raster_path.is_file():
        raise RuntimeError("PDAL writers.gdal hat kein Raster erzeugt.")
    log(f"  Rohraster (ungeclippt): {raw_raster_path}")

    # Verifizieren, dass PDAL die angeforderten "bounds" tatsaechlich respektiert hat
    # (bounds-Unterstuetzung in writers.gdal ist PDAL-versionsabhaengig).
    check_ds = gdal.Open(str(raw_raster_path), gdal.GA_ReadOnly)
    check_gt = check_ds.GetGeoTransform()
    check_ds = None
    origin_off = max(abs(check_gt[0] - snap_minx), abs(check_gt[3] - snap_maxy))
    if origin_off > 0.001:
        log(f"  WARNUNG: PDAL-Rohraster-Ursprung ({check_gt[0]:.3f}, {check_gt[3]:.3f}) weicht "
            f"vom angeforderten gesnappten Ursprung ({snap_minx:.3f}, {snap_maxy:.3f}) ab "
            f"(Differenz {origin_off:.3f}m) - 'bounds' wird von dieser PDAL-Version in "
            f"writers.gdal evtl. nicht wie erwartet unterstuetzt. Bitte pruefen.")
    else:
        log(f"  Pixelraster-Check OK: Rohraster-Ursprung entspricht dem gesnappten {gsd:g}m-Raster.")

    log(f"\nClippe Raster auf AOI (Cutline): {clip_shape_path}")
    log(f"  Ausserhalb -> NoData = {LAS_RASTER_NODATA:g}")
    warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=clip_shape_path,
        cropToCutline=False,
        outputBounds=(snap_minx, snap_miny, snap_maxx, snap_maxy),
        xRes=gsd, yRes=gsd,  # Raster-Grid exakt beibehalten (kein implizites Resampling)
        srcNodata=LAS_RASTER_NODATA,
        dstNodata=LAS_RASTER_NODATA,
        multithread=True,
        warpOptions=[f"NUM_THREADS={num_threads}"],
        outputSRS="EPSG:2056",
        creationOptions=[
            "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
            "COMPRESS=LZW", "PREDICTOR=3", "BIGTIFF=YES", "TFW=YES",
        ],
        callback=progress,
    )
    out_ds = gdal.Warp(output_path, str(raw_raster_path), options=warp_options)
    if out_ds is None:
        raise RuntimeError("gdal.Warp hat None zurueckgegeben - Raster-Clip fehlgeschlagen.")
    out_ds.FlushCache()
    out_ds = None
    log(f"  Gesamt-Raster (DSM) geschrieben: {output_path}")

    # --- Hillshade aus dem fertigen (bereits geclippten) DSM rechnen ---
    log("\nErzeuge Hillshade aus dem DSM...")
    raw_hillshade_path = run_dir / "05_hillshade_raw.tif"
    hs_ds = gdal.DEMProcessing(
        str(raw_hillshade_path), output_path, "hillshade",
        options=gdal.DEMProcessingOptions(computeEdges=True),
    )
    if hs_ds is None:
        raise RuntimeError("gdal.DEMProcessing hat None zurueckgegeben - Hillshade fehlgeschlagen.")
    hs_ds.FlushCache()
    hs_ds = None

    log(f"  Clippe Hillshade auf AOI (Cutline): {clip_shape_path}  (NoData ausserhalb = 255)")
    hs_warp_options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=clip_shape_path,
        cropToCutline=False,
        dstNodata=255,
        multithread=True,
        warpOptions=[f"NUM_THREADS={num_threads}"],
        outputSRS="EPSG:2056",
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
                print(f"PROGRESS:{0.30 + pct * 0.10:.6f}", flush=True)
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
    meta_errors = []
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_tile_bbox_worker, (pdal_exe, t)) for t in tiles]
        for i, fut in enumerate(as_completed(futures), 1):
            path, minx, miny, maxx, maxy, err = fut.result()
            if err:
                meta_errors.append((path, err))
            else:
                tile_bboxes.append((path, minx, miny, maxx, maxy))
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
    _log(f"Punktwolken-Format  : .{out_format}")
    _log(f"Benennung           : {jahr}_{area}_TIN_{thin_token}raw_<NAME>_LV95_LHN95.{out_format}")

    # --- Schritt 2: Gesamt-Raster (DSM + Hillshade fuer die AOI), nur falls aktiviert ---
    # Laeuft als Hintergrund-Thread (der eigentliche Rechenaufwand steckt im
    # PDAL-Subprocess bzw. in gdal.Warp/gdal.DEMProcessing, nicht im Python-Thread) -
    # parallel zur Punktwolken-Verarbeitung in Schritt 5, nutzt also einen
    # zusaetzlichen Kern nebenbei statt seriell davor zu laufen.
    raster_name = None
    hillshade_name = None
    raster_executor = None
    raster_future = None
    if create_raster:
        gsd_label = f"{round(gsd_raster * 100)}cm"
        raster_name = f"{jahr}_{area}_DSM_{gsd_label}_LV95_LHN95.tif"
        hillshade_name = f"{jahr}_{area}_hillshade_{gsd_label}_LV95_LHN95.tif"
        raster_out_path = str(Path(output_dir_raster) / raster_name)
        hillshade_out_path = str(Path(output_dir_raster) / hillshade_name)
        _log(f"Raster-Benennung    : {raster_name}  (+ .tfw)")
        _log(f"Hillshade-Benennung : {hillshade_name}  (+ .tfw)")
        raster_executor = ThreadPoolExecutor(max_workers=1)
        raster_future = raster_executor.submit(
            _build_las_raster,
            [t[0] for t in tile_bboxes], run_dir, raster_out_path, hillshade_out_path, pdal_exe,
            gsd_raster, thin_m, clip_shape_path,
            (all_minx, all_miny, all_maxx, all_maxy),
            str(num_workers), _log, _progress,
        )
        _log("\nRaster-Build (DSM + Hillshade) im Hintergrund gestartet (laeuft parallel "
             "zur Punktwolken-Verarbeitung weiter unten)...")

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
        jobs.append({"cell_bounds": (cminx, cminy, cmaxx, cmaxy),
                     "tiles": cell_tiles, "stem": stem})

    shp_ds = None

    if not jobs:
        raise RuntimeError(
            "Keine Grid-Kachel ueberlappt die Input-Kacheln - Grid-Shape/Input pruefen."
        )

    # Der Raster-Build (falls aktiv) laeuft als zusaetzlicher pdal.exe-Prozess im
    # Hintergrund - hier einen Slot dafuer reservieren, damit insgesamt nie mehr
    # gleichzeitige pdal.exe-Prozesse laufen als unter "CPU-Kerne" eingestellt
    # (sonst droht bei grossen Projekten Ressourcenueberlastung/Absturz).
    laz_workers = max(1, num_workers - 1) if create_raster else num_workers
    _log(f"\nStarte parallele Verarbeitung: {len(jobs)} Kachel(n) auf {laz_workers} Prozess(en)"
         + (f" ({num_workers} CPU-Kerne, 1 davon fuer den Raster-Build reserviert)" if create_raster else "")
         + "\n")

    laz_progress_start = 0.10  # Raster-Build laeuft parallel im Hintergrund, nicht mehr seriell davor
    written = errors = empty_skipped = done = 0
    with ProcessPoolExecutor(max_workers=laz_workers) as executor:
        futures = {
            executor.submit(_las_cell_worker,
                             (job, str(run_dir), output_dir_laz, pdal_exe, clip_wkt, thin_m, out_format)
                             ): job for job in jobs
        }
        for future in as_completed(futures):
            done += 1
            status, stem, err = future.result()
            if status == "written":
                written += 1
                _log(f"  [{done}/{len(jobs)}] {stem}.{out_format}")
            elif status == "empty":
                empty_skipped += 1
                _log(f"  [{done}/{len(jobs)}] {stem} - uebersprungen (keine Punkte nach Clip)")
            else:
                errors += 1
                _log(f"  [{done}/{len(jobs)}] FEHLER bei {stem}: {err}")
            print(f"PROGRESS:{laz_progress_start + (done / len(jobs)) * (1.0 - laz_progress_start):.6f}", flush=True)

    if raster_future is not None:
        _log("\nWarte auf Abschluss des Gesamt-Raster-Builds (Hintergrund)...")
        raster_future.result()  # wirft die Exception hier weiter, falls der Raster-Build fehlschlug
        raster_executor.shutdown(wait=True)

    if not keep_staging:
        _log(f"\nRaeume Staging-Ordner auf: {run_dir}")
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:
            pass
    else:
        _log(f"\nStaging-Dateien bleiben erhalten: {run_dir}")

    raster_line = (f"Raster: {raster_name}, Hillshade: {hillshade_name}\n" if raster_name
                   else "Raster: nicht erstellt (Option deaktiviert)\n")
    _log(f"\nFertig. {raster_line}"
         f".{out_format}: {written} Kachel(n) geschrieben, {skipped} uebersprungen (kein Overlap), "
         f"{empty_skipped} leer (0 Punkte nach Clip), {errors} Fehler.")
    if written == 0:
        raise RuntimeError("Keine Punktwolken-Kachel wurde geschrieben.")
    if errors:
        raise RuntimeError(f"{errors} Punktwolken-Kachel(n) konnten nicht verarbeitet werden - siehe Log.")


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
        else:
            print(f"[FEHLER] Unbekannte Aktion: '{action}'", flush=True)
            sys.exit(1)

    except Exception as e:
        print(f"\n[FEHLER] {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
