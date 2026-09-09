import importlib.util
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_constants_and_paths():
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )

    assert os.path.basename(gui_mod.RUNNER_SCRIPT) == "_osgeo_runner.py"
    assert isinstance(gui_mod.CONFIG_FILE, str) and gui_mod.CONFIG_FILE.endswith("_dmc_config.json")
    assert gui_mod.DEFAULT_GRID_SHAPE.endswith("chGRID_1km2.shp")
    assert "DMC_tempProcessingFolder" in gui_mod.DEFAULT_STAGING_DIR


def test_detect_python_home_returns_string():
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    candidate = os.path.join("C:", "OSGeo4W", "bin", "python3.exe")
    res = gui_mod._detect_python_home(candidate)
    assert isinstance(res, str)


def test_app_class_exists():
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    assert callable(gui_mod.DMCConverterApp)


def test_process_action_available():
    runner_mod = load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )
    assert callable(runner_mod._process)
    assert callable(runner_mod._grid_tile_worker)


def test_tiff_tab_name_preview():
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    app = gui_mod.DMCConverterApp()
    try:
        app._jahr_var.set("2026")
        app._area_var.set("GUPPENFIRN")
        app._gsd_var.set("10cm")
        app._update_name_preview()
        text = app._name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_DOP_10cm_<NAME>_LV95.tif" in text
    finally:
        app.destroy()


def test_tiff_tab_band_selection():
    """Band-Ausgabe im TIFFconverter: Default ist 4-Band (unveraendert), die
    3-Band-Auszuege bilden auf die Runner-Schluessel 'rgb'/'nrg' ab."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    app = gui_mod.DMCConverterApp()
    try:
        # Ohne Auswahl bleibt der Input unangetastet
        assert app._band_var.get() == gui_mod.BAND_KEEP
        assert app._band_mode() == "keep"

        app._band_var.set(gui_mod.BAND_RGB)
        assert app._band_mode() == "rgb"
        assert "RGB" in app._name_preview_lbl.cget("text")

        app._band_var.set(gui_mod.BAND_NRG)
        assert app._band_mode() == "nrg"
        assert "NRG" in app._name_preview_lbl.cget("text")

        # 4-Band-Input: Auswahl bleibt bedienbar
        app._apply_band_availability(4)
        assert str(app._band_combo.cget("state")) == "readonly"
        assert app._band_mode() == "nrg"

        # 3-Band-Input: Auswahl sperren und auf 4-Band/unveraendert zuruecksetzen,
        # sonst liefe der Job erst im Runner in einen Fehler
        app._apply_band_availability(3)
        assert str(app._band_combo.cget("state")) == "disabled"
        assert app._band_mode() == "keep"
        assert "3 Band" in app._band_hint_lbl.cget("text")

        # Unbekannte Bandzahl (keine Datei-Info): frei waehlbar
        app._apply_band_availability(None)
        assert str(app._band_combo.cget("state")) == "readonly"
    finally:
        app.destroy()


def test_band_modes_match_between_gui_and_runner():
    """Die GUI-Schluessel muessen exakt die Modi des Runners treffen - sonst
    bricht der Job erst im Subprocess ab."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    runner_mod = load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )
    assert set(gui_mod.BAND_MODE_KEYS.values()) == set(runner_mod.BAND_MODES)
    # RGB = Quellbaender 1,2,3  |  NRG = Quellbaender 4,1,2 (NIR, Rot, Gruen)
    assert runner_mod.BAND_MODES == {"keep": None, "rgb": [1, 2, 3], "nrg": [4, 1, 2]}
    assert callable(runner_mod._select_bands)


def test_band_selection_needs_four_band_input(tmp_path, monkeypatch):
    """Ein 3-Band-Input mit angeforderter Bandauswahl muss mit klarer Meldung
    abbrechen statt stillschweigend etwas Falsches zu schreiben."""
    import types

    runner_mod = load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )

    class _FakeDS:
        RasterCount = 3

    fake_gdal = types.SimpleNamespace(
        GA_ReadOnly=0,
        Open=lambda path, mode: _FakeDS(),
    )
    monkeypatch.setitem(__import__("sys").modules, "osgeo",
                        types.SimpleNamespace(gdal=fake_gdal))

    logged = []
    try:
        runner_mod._select_bands("mosaic.vrt", "rgb", tmp_path, logged.append)
        raise AssertionError("Bandauswahl haette scheitern muessen")
    except RuntimeError as e:
        assert "4-Band" in str(e)

    # 'keep' laesst die Quelle unangetastet (kein VRT, kein Oeffnen noetig)
    assert runner_mod._select_bands("mosaic.vrt", "keep", tmp_path, logged.append) == "mosaic.vrt"
def test_las_raster_is_built_cellwise():
    """Der Raster-Build muss zellweise laufen (kein Gesamt-Merge ueber alle Kacheln,
    der bei grossen Projekten den Arbeitsspeicher sprengt)."""
    runner_mod = load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )
    assert callable(runner_mod._raster_cell_worker)
    assert callable(runner_mod._mosaic_las_raster)
    assert not hasattr(runner_mod, "_build_las_raster")


def test_raster_cell_pipeline_structure(tmp_path):
    """Pipeline einer DSM-Zelle: gepufferter Crop (nahtlose IDW-Nachbarschaft),
    aber Ausgabe exakt auf dem Zellausschnitt (luecken-/ueberlappungsfreies Mosaik)."""
    import json

    runner_mod = load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )

    tif_out = tmp_path / "dsm_2714_1207.tif"
    captured = {}

    def fake_run(pdal_exe, pipeline_path):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        tif_out.write_bytes(b"dummy")  # PDAL-Ausgabe simulieren

    runner_mod._run_pdal_pipeline = fake_run

    bounds = (2714000.0, 1207000.0, 2715000.0, 1208000.0)
    job = {"cell": "2714_1207", "raster_bounds": bounds,
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    status, name, err = runner_mod._raster_cell_worker(
        (job, str(tmp_path), str(tmp_path), "pdal.exe", 0.2, 0.5))

    assert (status, name, err) == ("written", "2714_1207", None)
    types = [s["type"] for s in captured["stages"]]
    assert types == ["readers.las", "filters.merge", "filters.crop",
                     "filters.sample", "writers.gdal"]

    crop = captured["stages"][2]
    writer = captured["stages"][-1]
    assert writer["bounds"] == "([2714000.000,2715000.000],[1207000.000,1208000.000])"
    assert writer["output_type"] == "idw"
    # NIE -FLT_MAX an writers.gdal: PDAL lehnt die Float32-Bereichsgrenze selbst ab
    # ("Invalid nodata value ... for output data_type 'float'") - siehe
    # test_raster_nodata_sentinels.
    assert writer["nodata"] == runner_mod.LAS_CELL_NODATA
    assert writer["nodata"] != runner_mod.LAS_RASTER_NODATA
    # Crop-Bereich muss die Zelle rundum ueberragen (Puffer)
    assert crop["bounds"] == "([2713998.000,2715002.000],[1206998.000,1208002.000])"
    # Pipeline-Datei wird aufgeraeumt
    assert not list(tmp_path.glob("pipeline_*.json"))


# ── Tab "DMC - LASconverter [LN02]" ───────────────────────────────────────────
def _runner():
    return load_module_from_path(
        "runner_module",
        os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"),
    )


def _fake_las(vlrs, point_payload=b"POINTDATA", global_encoding=1):
    """Baut eine minimale, syntaktisch gueltige LAS-1.4-Datei im Speicher:
    375-Byte-Header + VLR-Block + Punktdaten. vlrs = [(user_id, record_id, payload)]."""
    import struct
    vlr_block = b""
    for user_id, record_id, payload in vlrs:
        vlr_block += struct.pack(
            "<H16sHH32s", 0, user_id.encode("ascii").ljust(16, b"\x00"),
            record_id, len(payload), b"test".ljust(32, b"\x00")) + payload
    header = bytearray(b"\x00" * 375)
    header[0:4] = b"LASF"
    struct.pack_into("<H", header, 6, global_encoding)
    struct.pack_into("<HII", header, 94, 375, 375 + len(vlr_block), len(vlrs))
    return bytes(header) + vlr_block + point_payload


def test_ln02_action_available():
    """Die LN02-Pipeline und ihre Bausteine muessen im Runner vorhanden sein."""
    runner_mod = _runner()
    for name in ("_process_las_ln02", "_ln02_tile_worker", "_inject_reference_vlrs",
                 "_parse_tile_origin", "_validate_ln02_target", "_resolve_crs_epsg"):
        assert callable(getattr(runner_mod, name)), name
    # Zielwerte wie SB_DSM_PUNKTWOLKE - bis auf das Punktformat: PF7 statt PF6,
    # damit die Farbe der DMC-Daten im GDWH-Produkt erhalten bleibt.
    assert (runner_mod.LN02_MINOR_VERSION, runner_mod.LN02_POINT_FORMAT,
            runner_mod.LN02_GLOBAL_ENCODING, runner_mod.LN02_SCALE) == (4, 7, 17, 0.01)
    assert runner_mod.LN02_POINT_LENGTH == 36
    assert runner_mod.LAS_LN02_SRS == "EPSG:2056+5728"


def test_ln02_parse_tile_origin():
    """Kachelursprung kommt deterministisch aus dem Dateinamen - LHN95- wie
    LN02-benannte Quellen, .las wie .laz."""
    import pytest
    runner_mod = _runner()

    assert runner_mod._parse_tile_origin(
        "2026_GUPPENFIRN_TIN_thinnedout04_raw_2713_1206_LV95_LHN95.las") == (2713, 1206)
    assert runner_mod._parse_tile_origin(
        "2026_GUPPENFIRN_TIN_raw_2713_1206_LV95_LN02.laz") == (2713, 1206)

    with pytest.raises(ValueError):
        runner_mod._parse_tile_origin("irgendwas_ohne_muster.las")
    with pytest.raises(ValueError):  # ausserhalb der Schweizer LV95-Ausdehnung
        runner_mod._parse_tile_origin("2026_X_TIN_raw_9999_1206_LV95_LN02.las")


def test_ln02_inject_reference_vlrs(tmp_path):
    """VLR-Injektion: vorhandene LASF_Projection-VLR wird ersetzt, Header-Offsets
    stimmen, WKT-Bit wird gesetzt, Punktdaten bleiben unangetastet."""
    import struct
    runner_mod = _runner()

    src = tmp_path / "tile.las"
    src.write_bytes(_fake_las([("LASF_Projection", 2112, b"ALT-LHN95-WKT")]))

    n_stripped = runner_mod._inject_reference_vlrs(str(src))
    assert n_stripped == 1

    data = src.read_bytes()
    header_size, offset_to_point_data, n_vlr = struct.unpack_from("<HII", data, 94)
    assert (header_size, n_vlr) == (375, 2)
    # 2x (54 Byte VLR-Header + Payload), alte VLR entfernt
    assert offset_to_point_data == 375 + (54 + 48) + (54 + 959)
    assert data[offset_to_point_data:] == b"POINTDATA"
    global_encoding, = struct.unpack_from("<H", data, 6)
    assert global_encoding & 0x10  # WKT-Bit
    assert b"LN02 height" in data and b"5728" in data
    assert b"ALT-LHN95-WKT" not in data


def test_inject_strips_pdal_liblas_wkt_twin(tmp_path):
    """PDALs writers.las schreibt denselben WKT ZWEIMAL: einmal als
    'LASF_Projection'/2112 und einmal als 'liblas'/2112 ("OGR variant of OpenGIS
    WKT SRS") - an einer erzeugten Kachel nachgemessen.

    Der 'liblas'-Zwilling steht in der Datei VOR der autoritativen Angabe und
    enthaelt nur den horizontalen Teil (LV95 ohne LN02). Bliebe er stehen, traege
    jede ausgelieferte Kachel zwei widersprechende Aussagen zum Raumbezug, und ein
    Leser, der schlicht den ersten 2112er nimmt, bekaeme die falsche.

    Der laszip-VLR muss dagegen zwingend erhalten bleiben - ohne ihn ist eine .laz
    nicht mehr dekomprimierbar."""
    import struct
    runner_mod = _runner()

    src = tmp_path / "tile.las"
    src.write_bytes(_fake_las([
        ("liblas", 2112, b"OGR-VARIANTE-NUR-LV95"),
        ("laszip encoded", 22204, b"LASZIP-VLR"),
        ("LASF_Projection", 2112, b"ALT-LHN95-WKT"),
    ]))

    n_stripped = runner_mod._inject_reference_vlrs(str(src))
    assert n_stripped == 2          # beide WKT-Varianten, nicht nur eine

    data = src.read_bytes()
    _hs, _offset, n_vlr = struct.unpack_from("<HII", data, 94)
    assert n_vlr == 3               # laszip + die zwei Referenz-VLRs
    assert b"OGR-VARIANTE-NUR-LV95" not in data
    assert b"ALT-LHN95-WKT" not in data
    assert b"LASZIP-VLR" in data    # bleibt - sonst waere die .laz unlesbar
    assert b"LN02 height" in data


def test_is_crs_vlr_trennt_raumbezug_von_nutzdaten():
    """Was als CRS-VLR gilt, entscheidet ueber Entfernen oder Behalten - hier die
    Grenzfaelle explizit."""
    runner_mod = _runner()
    assert runner_mod._is_crs_vlr("LASF_Projection", 2112)
    assert runner_mod._is_crs_vlr("LASF_Projection", 34735)
    assert runner_mod._is_crs_vlr("LASF_Projection", 999)   # Namensraum ist reserviert
    assert runner_mod._is_crs_vlr("liblas", 2112)
    assert not runner_mod._is_crs_vlr("liblas", 22204)
    assert not runner_mod._is_crs_vlr("laszip encoded", 22204)
    assert not runner_mod._is_crs_vlr("irgendwer", 2112)


def test_ln02_validate_target_rejects_second_crs_vlr():
    """Riegel gegen kuenftige Varianten: taucht neben der Referenz noch ein
    CRS-VLR auf, ist die Kachel ein Fehler - nicht bloss eine Warnung. Eine zweite
    Aussage zum Raumbezug faellt sonst niemandem auf."""
    runner_mod = _runner()
    src = _ln02_target_metadata(minor_version=2, dataformat_id=3, global_encoding=1)

    md = _ln02_target_metadata()
    md["vlr_2"] = {"user_id": "liblas", "record_id": 2112, "data": ""}
    problems = runner_mod._validate_ln02_target(src, md)
    assert any("liblas/2112" in p for p in problems)

    # Der laszip-VLR ist kein Raumbezug und darf nicht anschlagen
    md2 = _ln02_target_metadata()
    md2["vlr_2"] = {"user_id": "laszip encoded", "record_id": 22204, "data": ""}
    assert runner_mod._validate_ln02_target(src, md2) == []


def test_ln02_inject_reference_vlrs_fixes_laz_chunk_table(tmp_path):
    """Bei LAZ muss zusaetzlich die 'chunk table start position' um die
    Verschiebung des VLR-Blocks korrigiert werden - sonst bricht jeder echte
    Dekompressions-Durchlauf ab."""
    import struct
    runner_mod = _runner()

    chunk_table_pos = 5000
    point_payload = struct.pack("<q", chunk_table_pos) + b"LAZDATA"
    src = tmp_path / "tile.laz"
    src.write_bytes(_fake_las(
        [("laszip encoded", 22204, b"LZ"), ("LASF_Projection", 2112, b"ALT")],
        point_payload=point_payload))
    old_offset = 375 + (54 + 2) + (54 + 3)

    runner_mod._inject_reference_vlrs(str(src))

    data = src.read_bytes()
    _hs, new_offset, n_vlr = struct.unpack_from("<HII", data, 94)
    assert n_vlr == 3  # laszip-VLR bleibt, zwei Referenz-VLRs kommen dazu
    new_chunk_table_pos, = struct.unpack_from("<q", data, new_offset)
    assert new_chunk_table_pos == chunk_table_pos + (new_offset - old_offset)


def _ln02_target_metadata(**overrides):
    """Metadaten einer korrekt konvertierten Zielkachel (fuer _validate_ln02_target)."""
    import base64
    runner_mod = _runner()
    md = {
        "count": 100, "minx": 2713000.0, "maxx": 2713999.99,
        "miny": 1206000.0, "maxy": 1206999.99, "minz": 1000.0, "maxz": 1100.0,
        "minor_version": 4, "dataformat_id": 7, "point_length": 36,
        "header_size": 375, "global_encoding": 17,
        "vlr_0": {"user_id": "LASF_Projection", "record_id": 34735, "data": ""},
        "vlr_1": {"user_id": "LASF_Projection", "record_id": 2112,
                   "data": base64.b64encode(
                       base64.b64decode(runner_mod.REFERENCE_VLR_2112_B64)).decode()},
        "srs": {"json": {"components": [
            {"type": "ProjectedCRS", "id": {"authority": "EPSG", "code": 2056}},
            {"type": "VerticalCRS", "id": {"authority": "EPSG", "code": 5728}}]}},
        "spatialreference": 'COMPOUNDCRS["...",VERT_CS["LN02 height"]]',
    }
    md.update(overrides)
    return md


def test_ln02_validate_target():
    """Die Nachkonversions-Validierung muss die fachlich kritischen Faelle fangen."""
    runner_mod = _runner()
    src = _ln02_target_metadata(minor_version=2, dataformat_id=3, global_encoding=1)

    assert runner_mod._validate_ln02_target(src, _ln02_target_metadata()) == []

    # Hoehenbezug versehentlich LHN95 statt LN02 -> fachlicher Fehler
    problems = runner_mod._validate_ln02_target(src, _ln02_target_metadata(
        srs={"json": {"components": [
            {"type": "ProjectedCRS", "id": {"authority": "EPSG", "code": 2056}},
            {"type": "VerticalCRS", "id": {"authority": "EPSG", "code": 5729}}]}},
        spatialreference='COMPOUNDCRS["...",VERT_CS["LHN95 height"]]'))
    assert any("5728" in p for p in problems)
    assert any("LHN95" in p for p in problems)

    # Punkte verloren / falsches global_encoding / fehlender CRS-VLR
    assert any("Punktanzahl" in p
               for p in runner_mod._validate_ln02_target(src, _ln02_target_metadata(count=99)))
    assert any("global_encoding" in p
               for p in runner_mod._validate_ln02_target(src, _ln02_target_metadata(global_encoding=1)))
    md = _ln02_target_metadata()
    del md["vlr_0"], md["vlr_1"]
    assert any("34735" in p for p in runner_mod._validate_ln02_target(src, md))


def test_ln02_validate_bbox_uses_measured_source_extent():
    """Referenz fuer den BBox-Vergleich sind die gemessenen Quellpunkte, nicht der
    Quell-Header: eine nicht nachgefuehrte Header-BBox (kommt bei GeoSuite-reframten
    Kacheln vor) darf die Kachel nicht aus der Lieferung werfen, sondern nur warnen."""
    runner_mod = _runner()
    src = _ln02_target_metadata(minor_version=2, dataformat_id=3, global_encoding=1,
                                 maxz=1100.0101)          # Header 1 cm zu hoch
    measured = {"minx": 2713000.0, "maxx": 2713999.99,
                "miny": 1206000.0, "maxy": 1206999.99,
                "minz": 1000.0, "maxz": 1100.0}           # tatsaechliche Punkte

    warnings = []
    assert runner_mod._validate_ln02_target(
        src, _ln02_target_metadata(), measured, warnings) == []
    assert any("Quell-Header-BBox" in w and "maxz" in w for w in warnings)

    # Ohne gemessene Werte bleibt der Header die Referenz - und schlaegt an
    assert any("maxz" in p for p in runner_mod._validate_ln02_target(
        src, _ln02_target_metadata()))

    # Eine echte Abweichung Ziel vs. gemessene Quelle bleibt ein harter Fehler
    problems = runner_mod._validate_ln02_target(
        src, _ln02_target_metadata(maxz=1099.0), measured, [])
    assert any("maxz" in p and "gemessene Quellpunkte" in p for p in problems)


def test_ln02_stats_bbox_from_pipeline_metadata():
    """Die gemessene BBox kommt aus der ohnehin laufenden 'filters.stats'-Stage;
    fehlt eine der sechs Groessen, gibt es keine halbe Referenz."""
    runner_mod = _runner()
    meta = {"stages": {"filters.stats": {"statistic": [
        {"name": "X", "minimum": 2713000.0, "maximum": 2713999.99},
        {"name": "Y", "minimum": 1206000.0, "maximum": 1206999.99},
        {"name": "Z", "minimum": 1000.0, "maximum": 1100.0}]}}}
    assert runner_mod._stats_bbox_from_pipeline_metadata(meta) == {
        "minx": 2713000.0, "maxx": 2713999.99, "miny": 1206000.0,
        "maxy": 1206999.99, "minz": 1000.0, "maxz": 1100.0}

    del meta["stages"]["filters.stats"]["statistic"][2]      # Z fehlt
    assert runner_mod._stats_bbox_from_pipeline_metadata(meta) is None
    assert runner_mod._stats_bbox_from_pipeline_metadata(None) is None


def test_ln02_tile_worker_writer_options(tmp_path, monkeypatch):
    """Die Schreib-Pipeline muss exakt die GDWH-Zielwerte setzen: LAS 1.4/PF7,
    scale 0.01, Offset = Kachelursprung, global_encoding 17 - und bei .laz
    zusaetzlich LASzip-Kompression."""
    import json
    runner_mod = _runner()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    src_path = tmp_path / "2026_GUPPENFIRN_TIN_raw_2713_1206_LV95_LHN95.las"
    src_path.write_bytes(b"x")
    dst_path = out_dir / "2026_GUPPENFIRN_TIN_raw_2713_1206_LV95_LN02.laz"

    src_md = _ln02_target_metadata(minor_version=2, dataformat_id=3, global_encoding=1)
    captured = {}

    def fake_pipeline(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        # PDAL-Ausgabe simulieren, damit os.replace etwas zu verschieben hat
        open(captured["stages"][-1]["filename"], "wb").write(b"tmp")
        return {"stages": {"filters.stats": {"statistic": [
            {"name": "Classification", "minimum": 1, "maximum": 2}]}}}

    monkeypatch.setattr(runner_mod, "_run_pdal_pipeline", fake_pipeline)
    monkeypatch.setattr(runner_mod, "_inject_reference_vlrs", lambda path: 0)
    monkeypatch.setattr(runner_mod, "_pdal_classification_range", lambda exe, path: (1, 2))
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                         lambda exe, path: src_md if path == str(src_path)
                         else _ln02_target_metadata())

    status, name, err, warnings = runner_mod._ln02_tile_worker(
        (str(src_path), str(dst_path), "pdal.exe", str(tmp_path), 2713000.0, 1206000.0))

    assert (status, err) == ("written", None)
    assert name == dst_path.name
    assert dst_path.is_file()

    types = [s["type"] for s in captured["stages"]]
    assert types == ["readers.las", "filters.stats", "writers.las"]
    writer = captured["stages"][-1]
    assert writer["minor_version"] == 4
    assert writer["dataformat_id"] == 7
    assert writer["global_encoding"] == 17
    assert (writer["scale_x"], writer["scale_y"], writer["scale_z"]) == (0.01, 0.01, 0.01)
    assert (writer["offset_x"], writer["offset_y"], writer["offset_z"]) == (2713000.0, 1206000.0, 0)
    assert writer["compression"] == "laszip"   # .laz-Ziel
    # Kein a_srs: die CRS-Tags kommen ausschliesslich aus den Referenz-VLRs
    assert "a_srs" not in writer
    # Aufgeraeumt: keine Pipeline-/Metadata-Reste im Staging
    assert not list(tmp_path.glob("pipeline_ln02_*.json"))
    assert not list(tmp_path.glob("pipemeta_ln02_*.json"))


def test_ln02_tile_worker_rejects_points_outside_frame(tmp_path, monkeypatch):
    """Punkte ausserhalb des nominalen 1km-Rahmens deuten auf eine fehlplatzierte
    Datei hin - harter Fehler, keine Ausgabe."""
    runner_mod = _runner()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dst_path = out_dir / "2026_X_TIN_raw_2713_1206_LV95_LN02.las"

    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                         lambda exe, path: _ln02_target_metadata(maxx=2715000.0))

    status, _name, err, _w = runner_mod._ln02_tile_worker(
        (str(tmp_path / "a.las"), str(dst_path), "pdal.exe", str(tmp_path),
         2713000.0, 1206000.0))

    assert status == "error"
    assert "Kachelrahmen" in err
    assert not dst_path.exists()


def test_ln02_raster_cell_uses_ln02_srs(tmp_path):
    """Der Raster-Worker muss den im Job gesetzten SRS verwenden (LN02 statt des
    LHN95-Defaults) - sonst flossen die Kacheln mit falscher Hoehenreferenz ein."""
    import json
    runner_mod = _runner()

    tif_out = tmp_path / "dsm_2713_1206.tif"
    captured = {}

    def fake_run(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        tif_out.write_bytes(b"dummy")

    runner_mod._run_pdal_pipeline = fake_run

    job = {"cell": "2713_1206", "raster_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")], "srs": runner_mod.LAS_LN02_SRS}
    status, _name, err = runner_mod._raster_cell_worker(
        (job, str(tmp_path), str(tmp_path), "pdal.exe", None, 0.5))

    assert (status, err) == ("written", None)
    assert captured["stages"][0]["override_srs"] == "EPSG:2056+5728"


def test_ln02_thin_token_from_filename():
    """Der Thinning-Token wird aus dem Quell-Dateinamen uebernommen (ausgeduennt wurde
    bereits im Tab [LHN95]), nicht im LN02-Tab neu erfragt."""
    runner_mod = _runner()
    assert runner_mod._parse_thin_token(
        "2026_G_TIN_thinnedout04_raw_2713_1206_LV95_LHN95.las") == "thinnedout04_"
    assert runner_mod._parse_thin_token(
        "2026_G_TIN_raw_2713_1206_LV95_LHN95.las") == ""
    # Der Runner darf keine Thinning-Stage in die LN02-Pipeline haengen
    import inspect
    src = inspect.getsource(runner_mod._ln02_tile_worker)
    assert "filters.sample" not in src


def test_ln02_tab_name_preview():
    """Benennung im LN02-Tab: Jahr/AREA aus den GUI-Feldern, Thinning-Token und E/N
    aus dem Input-Dateinamen; Raster-Namen nur bei aktivierter Raster-Option."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    app = gui_mod.DMCConverterApp()
    try:
        app._ln02_jahr_var.set("2026")
        app._ln02_area_var.set("GUPPENFIRN")
        app._ln02_out_format_var.set("laz")
        app._update_ln02_name_preview()
        text = app._ln02_name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_TIN_[thinnedout<NN>_]raw_<E>_<N>_LV95_LN02.laz" in text
        assert "DSM" not in text
        assert ".vpc" not in text
        # Kein Thinning-Feld mehr - ausgeduennt wird ausschliesslich im Tab [LHN95]
        assert not hasattr(app, "_ln02_thin_var")

        # VPC ist unabhaengig vom Raster zuschaltbar
        app._ln02_create_vpc_var.set(True)
        app._update_ln02_name_preview()
        text = app._ln02_name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_LV95_LN02.vpc" in text
        assert "DSM" not in text

        app._ln02_create_raster_var.set(True)
        app._ln02_gsd_var.set("0.5")
        app._on_ln02_create_raster_toggle()
        text = app._ln02_name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_DSM_50cm_LV95_LN02.tif" in text
        assert "2026_GUPPENFIRN_hillshade_50cm_LV95_LN02.tif" in text
    finally:
        app.destroy()


def test_raster_nodata_sentinels():
    """Zwei getrennte NoData-Sentinel: PDAL schreibt einen unverfaenglichen Wert in die
    Staging-Zellraster, GDAL setzt beim Mosaikieren den GDWH-Sentinel (-FLT_MAX).

    Grund: PDALs writers.gdal lehnt die Float32-Bereichsgrenze selbst ab
    ("Invalid nodata value -3.402823466e+38 for output data_type 'float'"), und zwar
    deterministisch fuer jede Zelle. Ein anderes Zahlen-Literal hilft nicht - der Wert
    ist bitgenau -FLT_MAX (siehe Assertion unten)."""
    import inspect
    import struct
    runner_mod = _runner()

    # Der GDWH-Sentinel ist exakt -FLT_MAX (verlustfrei durch Float32)
    v = runner_mod.LAS_RASTER_NODATA
    assert v == struct.unpack("<f", struct.pack("<f", v))[0]
    # Der Zell-Sentinel ist ein anderer, in Float32 exakt darstellbarer Wert
    c = runner_mod.LAS_CELL_NODATA
    assert c != v
    assert c == struct.unpack("<f", struct.pack("<f", c))[0]
    # ... und als Schweizer Hoehenwert unmoeglich
    assert c < -1000

    # Das Mosaik liest den Zell-Sentinel und schreibt den GDWH-Sentinel
    src = inspect.getsource(runner_mod._mosaic_las_raster)
    assert "srcNodata=LAS_CELL_NODATA" in src
    assert "dstNodata=LAS_RASTER_NODATA" in src
    assert "VRTNodata=LAS_CELL_NODATA" in src
    # Der Hillshade bleibt bei NoData 255 (Byte) - Werte werden vorher aufbereitet
    assert (runner_mod.LAS_HILLSHADE_NODATA,
            runner_mod.LAS_HILLSHADE_VALID_MAX) == (255, 254)
    assert "dstNodata=LAS_HILLSHADE_NODATA" in src
    assert "_prepare_hillshade_values" in src
    # Reihenfolge ist entscheidend: erst aufbereiten, dann clippen
    assert src.index("_prepare_hillshade_values(") < src.index("hs_warp_options")

    # writers.gdal darf den GDWH-Sentinel nirgends direkt gesetzt bekommen
    assert "LAS_RASTER_NODATA" not in inspect.getsource(runner_mod._raster_cell_worker)

    # Der geschriebene Header wird zurueckgelesen und geprueft (Kontrolle statt Annahme)
    assert "GetNoDataValue()" in src
    assert "nicht auslieferbar" in src


def test_dsm_small_holes_filled_large_holes_kept():
    """Kleine DSM-Loecher werden interpoliert, grosse bleiben echtes NoData - und
    gefuellt wird VOR dem AOI-Clip. Danach ist ausserhalb des AOI ebenfalls NoData;
    die Interpolation kennt die AOI-Grenze nicht und liesse sich nicht mehr aufs
    Innere beschraenken."""
    import inspect
    runner_mod = _runner()

    assert runner_mod.LAS_FILL_NODATA_HOLES is True
    # Schwelle als FLAECHE definiert, damit sie von der GSD unabhaengig ist
    assert runner_mod.LAS_FILL_MAX_HOLE_AREA_M2 == 900.0
    assert runner_mod.LAS_FILL_HOLE_CONNECTEDNESS == 8

    src = inspect.getsource(runner_mod._mosaic_las_raster)
    assert "warp_source, hillshade_source = _fill_raster_nodata(" in src
    assert src.index("_fill_raster_nodata(") < src.index("cutlineDSName=clip_shape_path")
    # Geclippt wird das gefuellte Raster, nicht mehr das rohe VRT
    assert "gdal.Warp(output_path, warp_source" in src

    fill = inspect.getsource(runner_mod._fill_raster_nodata)
    # Zwei Varianten: DSM mit ehrlichen Luecken, Hillshade-Quelle vollstaendig gefuellt
    assert "return (str(filled_path), str(hs_src_path))" in fill
    # Die vollgefuellte Kopie muss VOR dem Ruecksetzen der grossen Loecher entstehen
    assert fill.index("CreateCopy(") < fill.index("arr[big] = LAS_CELL_NODATA")
    # Flaechenschwelle -> Pixelschwelle ueber die GSD, +1 damit "bis zu" inklusiv ist
    assert "LAS_FILL_MAX_HOLE_AREA_M2 / (gsd * gsd)" in fill
    assert "max_hole_px + 1" in fill
    # Grosse Loecher werden per SieveFilter identifiziert und danach zurueckgesetzt
    assert "gdal.SieveFilter(" in fill
    assert "arr[big] = LAS_CELL_NODATA" in fill
    # Erst fuellen, dann die grossen Loecher zuruecksetzen
    assert fill.index("gdal.FillNodata(") < fill.index("arr[big] = LAS_CELL_NODATA")
    # Das UND mit der Originalmaske schuetzt gemessene Inseln in grossen Loechern
    assert "(mask_band.ReadAsArray(0, y0, xs, rows) != 0)" in fill
    # Keine Glaettung -> kein Filter kann gemessene Werte antasten
    assert runner_mod.LAS_FILL_SMOOTHING_ITERATIONS == 0
    # Die GDAL-Hilfsraster in Originalgroesse gehoeren ins Staging
    assert 'SetConfigOption("CPL_TMPDIR", str(run_dir))' in fill
    # Kontrolle statt Annahme
    assert "remaining != kept" in fill and "WARNUNG" in fill


def test_hillshade_comes_from_fully_filled_mosaic():
    """Der Hillshade wird aus dem vollstaendig gefuellten, ungeclippten Mosaik
    gerechnet - nicht aus dem geclippten DSM. Sonst erschienen die grossen DSM-Loecher
    als weisse Flaechen und am AOI-Rand entstuende ein NoData-Saum. Beide Produkte
    muessen trotzdem exakt deckungsgleich sein."""
    import inspect
    runner_mod = _runner()
    src = inspect.getsource(runner_mod._mosaic_las_raster)

    # Quelle des Hillshade ist die gefuellte Variante, nicht output_path
    assert 'gdal.DEMProcessing(\n        str(raw_hillshade_path), hillshade_source,' in src
    assert 'str(raw_hillshade_path), output_path' not in src

    # Weil die Quelle jetzt das Mosaik ist, muss das Zielgitter erzwungen werden
    hs_opts = src[src.index("hs_warp_options = gdal.WarpOptions("):]
    assert "outputBounds=(snap_minx, snap_miny, snap_maxx, snap_maxy)" in hs_opts
    assert "xRes=gsd, yRes=gsd" in hs_opts

    # Kontrolle statt Annahme: Deckungsgleichheit wird geprueft, nicht angenommen
    assert "nicht auf demselben Gitter" in src


def test_prepare_hillshade_values(tmp_path):
    """Im Hillshade darf innerhalb des AOI kein 255 uebrig bleiben: voll beleuchtete
    Pixel (255) UND die NoData-Pixel ueber den DSM-Loechern (gdaldem schreibt dort 0)
    werden beide auf 254 gezogen. Alle uebrigen Werte bleiben unangetastet."""
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    runner_mod = _runner()

    path = str(tmp_path / "hs.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 3, 1, gdal.GDT_Byte)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(0)                      # so schreibt es gdaldem hillshade
    band.WriteArray(np.array([[0, 1, 254, 255],
                              [255, 255, 128, 0],
                              [7, 254, 255, 200]], dtype="uint8"))
    ds = None

    assert runner_mod._prepare_hillshade_values(path) == (5, 2)   # 5x255, 2x NoData

    ds = gdal.Open(path)
    out = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    assert not (out == 255).any()
    assert not (out == 0).any()
    assert out.tolist() == [[254, 1, 254, 254], [254, 254, 128, 254],
                            [7, 254, 254, 200]]

    # Idempotent: ein zweiter Lauf findet nichts mehr
    assert runner_mod._prepare_hillshade_values(path) == (0, 0)


def test_las_tile_writer_keeps_colour_for_geosuite(tmp_path):
    """Die Punktwolken-Tiles aus dem Tab [LHN95] sind die EINGABE fuer den
    GeoSuite/REFRAME-Batch und zugleich der Traeger der Farbe. Geschrieben wird
    deshalb explizit LAS 1.4 / PF7 (= PF6 + RGB) - GeoSuite liest das seit dem
    LAS-1.4-Update und reicht die Farbwerte durch."""
    import json

    runner_mod = _runner()
    captured = {}
    out_file = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"

    def fake_run(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        out_file.write_bytes(b"dummy")

    runner_mod._run_pdal_pipeline = fake_run
    runner_mod._pdal_info_metadata = lambda exe, path: {
        "count": 42, "minor_version": 4, "dataformat_id": 7}

    job = {"stem": out_file.stem, "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    args = (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
            None, "las", 1)
    status, _name, err = runner_mod._las_cell_worker(args)

    assert (status, err) == ("written", None)
    writer = captured["stages"][-1]
    assert writer["type"] == "writers.las"
    assert writer["minor_version"] == 4          # LAS 1.4
    assert writer["dataformat_id"] == 7          # PF7 - PF6 + RGB
    assert writer["a_srs"] == "EPSG:2056"        # nur horizontal, kein VerticalCSTypeGeoKey
    assert writer["scale_x"] == writer["scale_y"] == writer["scale_z"] == 0.01
    # Offset = Kachelursprung aus dem DATEINAMEN - dasselbe Gitter wie im Tab [LN02],
    # die dortige Requantisierung verschiebt dann keine Koordinaten.
    assert (writer["offset_x"], writer["offset_y"]) == (2713000.0, 1206000.0)
    # Der Hoehenbezug darf im Header NICHT getaggt sein (REFRAME bekommt ihn aus
    # der Batch-Konfiguration; den autoritativen LN02-Tag setzt erst Tab [LN02]).
    assert "5729" not in writer["a_srs"] and "5728" not in writer["a_srs"]
    # Bit 4 (WKT) gehoert bei LAS 1.4 mit PF >= 6 gesetzt, Bit 0 kommt aus der Quelle.
    assert writer["global_encoding"] == 0x11

    # Kontrolle statt Annahme: schreibt PDAL wider Erwarten ein anderes Format (z.B.
    # das frueher noetige LAS 1.2/PF1 ohne Farbfelder), darf die Kachel nicht im
    # Output-Ordner liegen bleiben.
    runner_mod._pdal_info_metadata = lambda exe, path: {
        "count": 42, "minor_version": 2, "dataformat_id": 1}
    status, _name, err = runner_mod._las_cell_worker(args)
    assert status == "error"
    assert "LAS 1.2/PF1" in err and "PF7" in err
    assert not out_file.exists()


def test_las_tile_forwards_gps_time_type(tmp_path):
    """Bit 0 des global_encoding (GPS-Time-Typ) beschreibt, wie die GpsTime-Werte zu
    lesen sind - das ist eine Eigenschaft der Daten. Es wird aus der Quelle uebernommen
    und nur gesetzt, wenn ALLE Quell-Tiles es fuehren. Bit 4 (WKT) setzt LAS 1.4 mit
    PF >= 6 dagegen immer."""
    import json

    runner_mod = _runner()
    captured = {}
    out_file = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"

    def fake_run(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        out_file.write_bytes(b"dummy")

    runner_mod._run_pdal_pipeline = fake_run
    runner_mod._pdal_info_metadata = lambda exe, path: {
        "count": 42, "minor_version": 4, "dataformat_id": 7}

    job = {"stem": out_file.stem, "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    for gps_bit in (0, 1):
        runner_mod._las_cell_worker(
            (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
             None, "las", gps_bit))
        assert captured["stages"][-1]["global_encoding"] == 0x10 | gps_bit


def test_ln02_gps_time_notice_is_measured_not_assumed():
    """Der GPS-Time-Hinweis im LN02-Tab haengt an den DATEN: nur wenn die Quelle
    tatsaechlich GpsTime-Werte fuehrt (und Bit 0 nicht gesetzt hat), ist die
    Typ-Deklaration eine Aussage, die jemand pruefen muss. Ist GpsTime durchgehend 0,
    beschreibt der Typ nichts - dann kein Hinweis."""
    import inspect
    runner_mod = _runner()

    src = inspect.getsource(runner_mod._ln02_tile_worker)
    # GpsTime wird im ohnehin noetigen Lesedurchlauf mitgemessen (kein zweiter Scan)
    assert 'stats_dims = "Classification,GpsTime,X,Y,Z"' in src
    assert 'gps_min == 0 and gps_max == 0' in src
    # Die WERTE werden nie angefasst - es gibt keine GpsTime-Umrechnung
    assert "filters.assign" not in src
    assert "GpsTime\":" not in src

    # Der Helfer liest beliebige Dimensionen aus derselben stats-Stage
    meta = {"stages": {"filters.stats": {"statistic": [
        {"name": "Classification", "minimum": 1, "maximum": 2},
        {"name": "GpsTime", "minimum": 0, "maximum": 0}]}}}
    assert runner_mod._stat_range_from_pipeline_metadata(meta, "Classification") == (1, 2)
    assert runner_mod._stat_range_from_pipeline_metadata(meta, "GpsTime") == (0, 0)
    assert runner_mod._stat_range_from_pipeline_metadata(meta, "Fehlt") == (None, None)


def test_ln02_worker_reports_missing_colour(tmp_path, monkeypatch):
    """PF7 fuehrt die RGB-Felder immer - auch wenn nur Nullen darin stehen. Eine
    farblos gewordene Lieferung faellt sonst erst im GDWH auf. Geprueft wird deshalb
    am Punktformat der Quelle UND an den gemessenen RGB-Werten; RGB darf dabei nur
    dann in 'filters.stats' stehen, wenn die Quelle die Dimensionen ueberhaupt hat
    (die Stage bricht sonst ab)."""
    import json
    runner_mod = _runner()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    src_path = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"
    src_path.write_bytes(b"x")
    dst_path = out_dir / "2026_G_TIN_raw_2713_1206_LV95_LN02.laz"

    captured = {}

    def run_with(src_format, rgb_max):
        src_md = _ln02_target_metadata(minor_version=2, dataformat_id=src_format,
                                        global_encoding=1)
        stats = [{"name": "Classification", "minimum": 1, "maximum": 2}]
        if rgb_max is not None:
            stats += [{"name": c, "minimum": 0, "maximum": rgb_max}
                      for c in ("Red", "Green", "Blue")]

        def fake_pipeline(pdal_exe, pipeline_path, metadata_path=None):
            captured["stages"] = json.loads(
                open(pipeline_path, encoding="utf-8").read())["pipeline"]
            open(captured["stages"][-1]["filename"], "wb").write(b"tmp")
            return {"stages": {"filters.stats": {"statistic": stats}}}

        monkeypatch.setattr(runner_mod, "_run_pdal_pipeline", fake_pipeline)
        monkeypatch.setattr(runner_mod, "_inject_reference_vlrs", lambda path: 0)
        monkeypatch.setattr(runner_mod, "_pdal_classification_range", lambda exe, path: (1, 2))
        monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                             lambda exe, path: src_md if path == str(src_path)
                             else _ln02_target_metadata())
        return runner_mod._ln02_tile_worker(
            (str(src_path), str(dst_path), "pdal.exe", str(tmp_path), 2713000.0, 1206000.0))

    # Quelle ohne Farbfelder (PF1, z.B. aus einem GeoSuite-Lauf vor dem LAS-1.4-Update)
    status, _n, err, warnings = run_with(1, None)
    assert (status, err) == ("written", None)
    assert "Red" not in captured["stages"][1]["dimensions"]
    assert any("keine Farbfelder" in w for w in warnings)

    # Quelle mit Farbfeldern, aber durchgehend 0 -> schwarze Kachel
    status, _n, err, warnings = run_with(7, 0)
    assert (status, err) == ("written", None)
    assert captured["stages"][1]["dimensions"].endswith("Red,Green,Blue")
    assert any("durchgehend 0" in w for w in warnings)

    # Quelle mit echten Farbwerten -> kein Farb-Hinweis
    status, _n, err, warnings = run_with(7, 65280)
    assert (status, err) == ("written", None)
    assert not any("Farb" in w or "RGB" in w for w in warnings)


def test_ln02_copy_shortcut_only_for_finished_tiles():
    """Die Abkuerzung 'schon migriert, nur kopieren' darf NUR fuer das fertige
    Zielprodukt greifen. Seit auch die Zwischenstufe LAS 1.4/PF7 ist, unterscheidet
    sich eine GeoSuite-Ausgabe davon nur noch an scale/offset und den CRS-VLRs -
    ohne diese Kontrollen wuerde eine reframte Kachel unveraendert durchgereicht."""
    import base64
    runner_mod = _runner()

    def md(**over):
        m = _ln02_target_metadata(scale_x=0.01, scale_y=0.01, scale_z=0.01)
        m.update(over)
        return m

    # Fertige Kachel: byte-exakte Referenz-VLRs, scale 0.01, PF7/17
    finished = md(vlr_0={"user_id": "LASF_Projection", "record_id": 34735,
                          "data": runner_mod.REFERENCE_VLR_34735_B64},
                   vlr_1={"user_id": "LASF_Projection", "record_id": 2112,
                          "data": runner_mod.REFERENCE_VLR_2112_B64})
    assert runner_mod._ln02_is_already_migrated(finished)

    # Gleiches Format und CRS, aber fremde CRS-VLRs (z.B. von GeoSuite) -> keine Kopie
    foreign = md(vlr_0={"user_id": "LASF_Projection", "record_id": 34735,
                         "data": base64.b64encode(b"nicht die Referenz").decode()},
                  vlr_1={"user_id": "LASF_Projection", "record_id": 2112,
                         "data": base64.b64encode(b"auch nicht").decode()})
    assert not runner_mod._ln02_is_already_migrated(foreign)

    # Referenz-VLRs, aber falsches scale -> keine Kopie (Requantisierung fehlt)
    assert not runner_mod._ln02_is_already_migrated(dict(finished, scale_x=0.001))
    # Noch die Zwischenstufe (kein Vertikal-CRS) -> keine Kopie
    assert not runner_mod._ln02_is_already_migrated(dict(
        finished, srs={"json": {"components": [
            {"type": "ProjectedCRS", "id": {"authority": "EPSG", "code": 2056}}]}}))
# ══════════════════════ Virtual Point Cloud (VPC) fuer QGIS ══════════════════════

def test_vpc_feature_uses_wgs84_for_bbox_and_native_for_proj():
    """Die entscheidende Eigenschaft der VPC: 'geometry'/'bbox' sind WGS84
    (STAC-Konvention), die Landeskoordinaten stehen in 'proj:bbox'.

    Das ist kein Formalismus - mit LV95-Werten in 'bbox' laedt QGIS die Ebene OHNE
    Fehlermeldung, liefert aber einen unendlichen Extent: die Kacheln sind dann
    unsichtbar und nichts weist darauf hin. Am QGIS-Provider (3.44) nachgemessen."""
    runner_mod = _runner()

    native = (2713000.0, 1206000.0, 1000.0, 2714000.0, 1207000.0, 1100.0)
    ring = [(8.92, 46.99), (8.92, 47.00), (8.94, 47.00), (8.94, 46.99), (8.92, 46.99)]
    feat = runner_mod._vpc_feature(
        "2026_G_TIN_raw_2713_1206_LV95_LN02", "../2026_G_TIN_raw_2713_1206_LV95_LN02.laz",
        4711, native, ring, "PROJCS[\"CH1903+ / LV95\"...]",
        "application/vnd.laszip", "2026-09-09T10:00:00Z")

    # bbox: horizontal WGS84 aus dem Ring, vertikal unveraendert in Metern
    assert feat["bbox"] == [8.92, 46.99, 1000.0, 8.94, 47.00, 1100.0]
    assert feat["geometry"]["coordinates"][0][0] == [8.92, 46.99]
    # Landeskoordinaten NUR in proj:bbox - niemals in bbox
    assert feat["properties"]["proj:bbox"] == list(native)
    assert feat["bbox"][0] != native[0]
    # Ohne proj:wkt2 lehnt QGIS die Datei ab
    assert "CH1903+" in feat["properties"]["proj:wkt2"]
    assert feat["assets"]["data"]["href"].startswith("../")
    assert feat["properties"]["pc:count"] == 4711
    # Photogrammetrisch abgeleitet - nicht 'lidar'
    assert feat["properties"]["pc:type"] == "eopc"


def test_vpc_document_is_a_feature_collection():
    """QGIS erwartet eine GeoJSON/STAC-FeatureCollection."""
    runner_mod = _runner()
    doc = runner_mod._build_vpc([{"type": "Feature"}, {"type": "Feature"}])
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == 2


def test_vpc_is_optional_and_never_fails_the_run():
    """Die VPC ist ein Ansichtsprodukt neben der Lieferung. Ein Fehler beim Schreiben
    darf den Lauf nicht scheitern lassen - die Kacheln sind das Produkt."""
    import inspect
    runner_mod = _runner()

    src = inspect.getsource(runner_mod._process_las_ln02)
    assert "if create_vpc:" in src
    # Der VPC-Block haengt in einem try/except und meldet nur eine WARNUNG
    vpc_block = src.split("if create_vpc:", 1)[1].split("Schritt 6", 1)[0]
    assert "except Exception" in vpc_block
    assert "WARNUNG" in vpc_block
    assert "raise" not in vpc_block


def test_vpc_tile_worker_reports_errors_instead_of_raising(monkeypatch):
    """Eine unlesbare Kachel darf den VPC-Lauf nicht abbrechen, sondern wird als
    Fehler zurueckgemeldet und uebersprungen."""
    runner_mod = _runner()

    def boom(exe, path):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(runner_mod, "_pdal_info_metadata", boom)
    res = runner_mod._vpc_tile_worker(("pdal.exe", "x.laz"))
    assert res[0] == "x.laz"
    assert res[-1] == "kaputt"

    monkeypatch.setattr(runner_mod, "_pdal_info_metadata", lambda exe, path: {
        "count": 12, "minx": 1.0, "miny": 2.0, "minz": 3.0,
        "maxx": 4.0, "maxy": 5.0, "maxz": 6.0})
    res = runner_mod._vpc_tile_worker(("pdal.exe", "y.laz"))
    assert res == ("y.laz", 12, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, None)
# ══════════════════════ Tab "Create DSM-Raster" ══════════════════════

def test_dsm_action_available():
    """Die eigenstaendige Raster-Pipeline und ihre Bausteine muessen da sein."""
    runner_mod = _runner()
    for name in ("_process_dsm", "_dsm_cell_jobs", "_raster_cell_worker",
                 "_mosaic_las_raster"):
        assert callable(getattr(runner_mod, name)), name

    import inspect
    src = inspect.getsource(runner_mod.main)
    assert '"process_dsm"' in src          # Aktion ist verdrahtet


def test_dsm_cells_are_clipped_to_the_data_extent():
    """Die Arbeitszellen werden auf den Datenbereich beschnitten. Ohne das rastert
    eine Kachel, die nur in einer Ecke ihrer Kilometerzelle liegt, trotzdem den
    ganzen Quadratkilometer - an einem Testdatensatz gemessen 7.7 Mio. leere Pixel
    und ueber zwei Minuten Laufzeit statt rund einer Sekunde."""
    runner_mod = _runner()

    # Eine 200x200-m-Kachel mitten in der Zelle 2713_1206
    tiles = [("a.laz", 2713800.0, 1206000.0, 2714000.0, 1206200.0)]
    snap = (2713800.0, 1206000.0, 2714000.0, 1206200.0)
    jobs = runner_mod._dsm_cell_jobs(tiles, snap, 0.5, "EPSG:2056+5729")

    assert len(jobs) == 1
    rb = jobs[0]["raster_bounds"]
    assert rb == snap                      # exakt der Datenbereich, nicht 1 km2
    assert jobs[0]["srs"] == "EPSG:2056+5729"
    assert jobs[0]["tiles"] == ["a.laz"]


def test_dsm_cells_tile_without_gap_or_overlap():
    """Ueber eine Kilometergrenze hinweg muessen die Zellen luecken- UND
    ueberlappungsfrei aneinanderstossen - sonst hat das Mosaik eine Naht."""
    runner_mod = _runner()

    tiles = [("links.laz",  2713800.0, 1206000.0, 2714000.0, 1206200.0),
             ("rechts.laz", 2714000.0, 1206000.0, 2714200.0, 1206200.0)]
    snap = (2713800.0, 1206000.0, 2714200.0, 1206200.0)
    jobs = runner_mod._dsm_cell_jobs(tiles, snap, 0.5, "EPSG:2056+5729")

    assert {j["cell"] for j in jobs} == {"2713_1206", "2714_1206"}
    links = next(j for j in jobs if j["cell"] == "2713_1206")["raster_bounds"]
    rechts = next(j for j in jobs if j["cell"] == "2714_1206")["raster_bounds"]
    # Rechte Kante der einen ist exakt die linke Kante der anderen
    assert links[2] == rechts[0] == 2714000.0
    # Zusammen decken sie den Datenbereich vollstaendig ab
    assert (links[0], rechts[2]) == (snap[0], snap[2])

    # Randkacheln kommen dank Puffer in BEIDEN Zellen vor - sonst bliebe die
    # IDW-Nachbarschaft an der Naht einseitig.
    assert "rechts.laz" in next(j for j in jobs if j["cell"] == "2713_1206")["tiles"]
    assert "links.laz" in next(j for j in jobs if j["cell"] == "2714_1206")["tiles"]


def test_dsm_cells_skip_empty_areas():
    """Zellen ohne beitragende Kachel entfallen - bei verstreuten Kacheln waeren
    das sonst hunderte leere Rasterjobs."""
    runner_mod = _runner()

    # Zwei Kacheln 3 km auseinander; die rechte beginnt 100 m INNERHALB ihrer Zelle,
    # also klar ausserhalb des Puffers der Nachbarzelle 2715.
    tiles = [("a.laz", 2713000.0, 1206000.0, 2713500.0, 1206500.0),
             ("b.laz", 2716100.0, 1206000.0, 2716500.0, 1206500.0)]
    snap = (2713000.0, 1206000.0, 2716500.0, 1206500.0)
    jobs = runner_mod._dsm_cell_jobs(tiles, snap, 0.5, "EPSG:2056+5729")

    # 2713, 2714, 2715, 2716 waeren moeglich - nur die zwei mit Daten bleiben
    assert {j["cell"] for j in jobs} == {"2713_1206", "2716_1206"}

    # Gegenprobe zum Puffer: beginnt die Kachel exakt auf der Zellkante, gehoert sie
    # sehr wohl auch zur Nachbarzelle - deren Randpixel brauchen sie fuer eine
    # vollstaendige IDW-Nachbarschaft. Die dazwischen liegende, wirklich leere Zelle
    # entfaellt trotzdem.
    tiles_kante = [("a.laz", 2713000.0, 1206000.0, 2713500.0, 1206500.0),
                   ("b.laz", 2716000.0, 1206000.0, 2716500.0, 1206500.0)]
    snap_kante = (2713000.0, 1206000.0, 2716500.0, 1206500.0)
    cells = {j["cell"] for j in
             runner_mod._dsm_cell_jobs(tiles_kante, snap_kante, 0.5, "EPSG:2056+5729")}
    assert "2715_1206" in cells        # Kachel liegt exakt auf deren rechter Kante
    assert "2714_1206" not in cells    # dazwischen: nichts zu rastern


def test_dsm_tab_name_preview():
    """Benennung im DSM-Tab: Jahr/AREA/GSD/Hoehenbezug aus den GUI-Feldern."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    app = gui_mod.DMCConverterApp()
    try:
        tabs = [app._notebook.tab(i, "text") for i in range(app._notebook.index("end"))]
        assert "Create DSM-Raster" in tabs

        app._dsm_jahr_var.set("2026")
        app._dsm_area_var.set("GUPPENFIRN")
        app._dsm_gsd_var.set("0.5")
        app._dsm_href_var.set("LN02")
        text = app._dsm_name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_DSM_50cm_LV95_LN02.tif" in text
        assert "2026_GUPPENFIRN_hillshade_50cm_LV95_LN02.tif" in text

        # Hoehenbezug schlaegt bis in den Dateinamen durch
        app._dsm_href_var.set("LHN95")
        assert "LV95_LHN95.tif" in app._dsm_name_preview_lbl.cget("text")
    finally:
        app.destroy()


def test_dsm_tab_reads_project_from_tile_name():
    """Beim Waehlen des Input-Ordners werden Jahr, AREA und Hoehenbezug aus dem
    ersten Kachelnamen vorbelegt - AREA darf dabei Unterstriche enthalten."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    assert gui_mod._project_from_tile_name(
        "2026_GUPPENFIRN_v2_TIN_thinnedout02_raw_2713_1206_LV95_LN02.laz"
    ) == ("2026", "GUPPENFIRN_v2", "LN02")
    assert gui_mod._project_from_tile_name(
        "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"
    ) == ("2026", "G", "LHN95")
    # Fremddaten: nichts vorbelegen statt etwas zu raten
    assert gui_mod._project_from_tile_name("irgendeine_wolke.laz") is None
