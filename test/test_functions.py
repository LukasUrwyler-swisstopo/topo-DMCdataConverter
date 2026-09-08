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
    # Zielwerte identisch zu SB_DSM_PUNKTWOLKE
    assert (runner_mod.LN02_MINOR_VERSION, runner_mod.LN02_POINT_FORMAT,
            runner_mod.LN02_GLOBAL_ENCODING, runner_mod.LN02_SCALE) == (4, 6, 17, 0.01)
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
        "minor_version": 4, "dataformat_id": 6, "point_length": 30,
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
    """Die Schreib-Pipeline muss exakt die GDWH-Zielwerte setzen: LAS 1.4/PF6,
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
    assert writer["dataformat_id"] == 6
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
        # Kein Thinning-Feld mehr - ausgeduennt wird ausschliesslich im Tab [LHN95]
        assert not hasattr(app, "_ln02_thin_var")

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


def test_las_tile_writer_is_geosuite_readable(tmp_path):
    """Die Punktwolken-Tiles aus dem Tab [LHN95] sind die EINGABE fuer den
    GeoSuite/REFRAME-Batch. Ohne explizite Angabe schreibt PDAL 2.8.3 LAS 1.4 / PF7
    (nachgemessen), was GeoSuite mit "unknown or unsupported format" ablehnt -
    geschrieben werden muss LAS 1.2 / PF1, wie in der etablierten
    SB_DSM_PUNKTWOLKE-Lieferkette."""
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
        "count": 42, "minor_version": 2, "dataformat_id": 1}

    job = {"stem": out_file.stem, "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    args = (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
            None, "las", 1)
    status, _name, err = runner_mod._las_cell_worker(args)

    assert (status, err) == ("written", None)
    writer = captured["stages"][-1]
    assert writer["type"] == "writers.las"
    assert writer["minor_version"] == 2          # LAS 1.2
    assert writer["dataformat_id"] == 1          # PF1 - NICHT PDALs Default 3
    assert writer["a_srs"] == "EPSG:2056"        # nur horizontal, kein VerticalCSTypeGeoKey
    assert writer["scale_x"] == writer["scale_y"] == writer["scale_z"] == 0.01
    # Der Hoehenbezug darf im Header NICHT getaggt sein (REFRAME bekommt ihn aus
    # der Batch-Konfiguration; den autoritativen LN02-Tag setzt erst Tab [LN02]).
    assert "5729" not in writer["a_srs"] and "5728" not in writer["a_srs"]
    # GPS-Time-Typ wird aus der Quelle uebernommen, nicht erfunden; Bit 4 (WKT) gibt es
    # erst ab LAS 1.4 und darf hier nicht gesetzt sein.
    assert writer["global_encoding"] == 1

    # Kontrolle statt Annahme: schreibt PDAL wider Erwarten doch LAS 1.4/PF7, darf die
    # Kachel nicht im Output-Ordner liegen bleiben - sie waere fuer REFRAME unbrauchbar.
    runner_mod._pdal_info_metadata = lambda exe, path: {
        "count": 42, "minor_version": 4, "dataformat_id": 7}
    status, _name, err = runner_mod._las_cell_worker(args)
    assert status == "error"
    assert "LAS 1.4/PF7" in err and "GeoSuite" in err
    assert not out_file.exists()


def test_las_tile_forwards_gps_time_type(tmp_path):
    """Bit 0 des global_encoding (GPS-Time-Typ) beschreibt, wie die GpsTime-Werte zu
    lesen sind - das ist eine Eigenschaft der Daten. Es wird aus der Quelle uebernommen
    und nur gesetzt, wenn ALLE Quell-Tiles es fuehren."""
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
        "count": 42, "minor_version": 2, "dataformat_id": 1}

    job = {"stem": out_file.stem, "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    for gps_bit in (0, 1):
        runner_mod._las_cell_worker(
            (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
             None, "las", gps_bit))
        assert captured["stages"][-1]["global_encoding"] == gps_bit


def test_ln02_gps_time_notice_is_measured_not_assumed():
    """Der GPS-Time-Hinweis im LN02-Tab haengt an den DATEN: nur wenn die Quelle
    tatsaechlich GpsTime-Werte fuehrt (und Bit 0 nicht gesetzt hat), ist die
    Typ-Deklaration eine Aussage, die jemand pruefen muss. Ist GpsTime durchgehend 0,
    beschreibt der Typ nichts - dann kein Hinweis."""
    import inspect
    runner_mod = _runner()

    src = inspect.getsource(runner_mod._ln02_tile_worker)
    # GpsTime wird im ohnehin noetigen Lesedurchlauf mitgemessen (kein zweiter Scan)
    assert '"dimensions": "Classification,GpsTime,X,Y,Z"' in src
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
