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
        (str(src_path), str(dst_path), "pdal.exe", str(tmp_path), 2713000.0, 1206000.0,
         None))

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
         2713000.0, 1206000.0, None))

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
            None, "las", 1, None)
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
             None, "las", gps_bit, None))
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


# ══════════════════════ Farb-Join: PF7-Master (RGB) ══════════════════════

def test_las_cell_worker_writes_pf7_master(tmp_path):
    """Der Tab [LHN95] muss aus DEMSELBEN Pipeline-Lauf zwei Ausgaben schreiben:
    das PF1-LAS fuer GeoSuite und den farbfuehrenden PF7-Master. Zwei getrennte
    Laeufe waeren nicht zulaessig - 'filters.sample' entscheidet ueber die
    Punktauswahl, deren Identitaet waere dann nur eine Annahme."""
    import json

    runner_mod = _runner()
    captured = {}
    master_dir = tmp_path / "_master_PF7"
    master_dir.mkdir()
    out_file = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"
    master_file = master_dir / out_file.name

    def fake_run(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        out_file.write_bytes(b"dummy")
        master_file.write_bytes(b"dummy")

    runner_mod._run_pdal_pipeline = fake_run
    runner_mod._pdal_info_metadata = lambda exe, path: (
        {"count": 42, "minor_version": 4, "dataformat_id": 7} if "_master_PF7" in path
        else {"count": 42, "minor_version": 2, "dataformat_id": 1})

    job = {"stem": out_file.stem,
           "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    status, _name, err = runner_mod._las_cell_worker(
        (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
         0.2, "las", 1, str(master_dir)))

    assert (status, err) == ("written", None)

    writers = [s for s in captured["stages"] if s["type"] == "writers.las"]
    assert len(writers) == 2, "GeoSuite-Ausgabe und Master muessen beide geschrieben werden"
    geosuite = next(w for w in writers if "_master_PF7" not in w["filename"])
    master = next(w for w in writers if "_master_PF7" in w["filename"])

    # Der Master fuehrt Farbe, die GeoSuite-Eingabe nicht
    assert geosuite["dataformat_id"] == 1 and geosuite["minor_version"] == 2
    assert master["dataformat_id"] == 7 and master["minor_version"] == 4

    # Beide haengen an DERSELBEN letzten Filterstufe - ein Lauf, eine Punktauswahl
    assert geosuite["inputs"] == master["inputs"]
    sample = [s for s in captured["stages"] if s["type"] == "filters.sample"]
    assert len(sample) == 1
    assert geosuite["inputs"] == [sample[0]["tag"]]

    # Gleiches Gitter: nur so ist die X/Y-Kontrolle beim Join exakt
    for w in (geosuite, master):
        assert (w["scale_x"], w["scale_y"], w["scale_z"]) == (0.01, 0.01, 0.01)
        assert (w["offset_x"], w["offset_y"], w["offset_z"]) == (2713000.0, 1206000.0, 0)


def test_las_cell_worker_discards_master_on_count_mismatch(tmp_path):
    """Traegt der Master nicht dieselbe Punktmenge wie die GeoSuite-Eingabe, waere der
    Index-Join spaeter nicht zuordenbar - das muss hier auffallen, nicht erst nach dem
    GeoSuite-Lauf."""
    runner_mod = _runner()
    master_dir = tmp_path / "_master_PF7"
    master_dir.mkdir()
    out_file = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"
    master_file = master_dir / out_file.name

    def fake_run(pdal_exe, pipeline_path, metadata_path=None):
        out_file.write_bytes(b"dummy")
        master_file.write_bytes(b"dummy")

    runner_mod._run_pdal_pipeline = fake_run
    runner_mod._pdal_info_metadata = lambda exe, path: (
        {"count": 41, "minor_version": 4, "dataformat_id": 7} if "_master_PF7" in path
        else {"count": 42, "minor_version": 2, "dataformat_id": 1})

    job = {"stem": out_file.stem,
           "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}
    status, _name, err = runner_mod._las_cell_worker(
        (job, str(tmp_path), str(tmp_path), "pdal.exe", "POLYGON((0 0,1 0,1 1,0 0))",
         None, "las", 1, str(master_dir)))

    assert status == "error"
    assert "41" in err and "42" in err
    assert not out_file.exists() and not master_file.exists()


def test_ln02_validate_target_accepts_pf7_profile():
    """Mit Farb-Join ist PF7/36 das Ziel, ohne ihn PF6/30. Beide Profile muessen
    sauber durchgehen - und das jeweils andere Format als Fehler melden."""
    runner_mod = _runner()
    src = _ln02_target_metadata()

    pf7 = _ln02_target_metadata(dataformat_id=7, point_length=36)
    assert runner_mod._validate_ln02_target(src, pf7, point_format=7, point_length=36) == []

    # PF6-Datei, aber PF7 erwartet -> muss auffallen
    problems = runner_mod._validate_ln02_target(src, _ln02_target_metadata(),
                                                point_format=7, point_length=36)
    assert any("dataformat_id" in p for p in problems)
    assert any("point_length" in p for p in problems)

    # Default (ohne Parameter) bleibt das bisherige PF6-Profil
    assert runner_mod._validate_ln02_target(src, _ln02_target_metadata()) == []


def test_validate_ln02_rgb_detects_lost_colour(monkeypatch, tmp_path):
    """Ohne RGB-Kontrolle wuerde ein fehlgeschlagener Join gruen validieren:
    Punktanzahl, BBox und Classification sind auch bei lauter Nullen in Ordnung."""
    import json as _json
    runner_mod = _runner()
    dst = str(tmp_path / "x.laz")
    span = {"lo": 0, "hi": 65280}

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = _json.dumps({"stats": {"statistic": [
                {"name": c, "minimum": span["lo"], "maximum": span["hi"]}
                for c in ("Red", "Green", "Blue")]}})
            stderr = ""
        return R()
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    master = {"rgb_min": 0, "rgb_max": 65280}

    # Farbe korrekt durchgereicht
    assert runner_mod._validate_ln02_rgb("pdal.exe", dst, master) == []

    # Ziel schwarz, Master hatte Farbe -> harter Fehler
    span["hi"] = 0
    problems = runner_mod._validate_ln02_rgb("pdal.exe", dst, master)
    assert problems and "verloren" in problems[0]

    # Spanne veraendert -> Fehler
    span["hi"] = 255
    problems = runner_mod._validate_ln02_rgb("pdal.exe", dst, master)
    assert problems and "RGB-Spanne veraendert" in problems[0]


def test_validate_ln02_rgb_rejects_colourless_master(tmp_path):
    """Ein Master ohne Farbwerte ergaebe eine schwarze Kachel - lieber abbrechen als
    ausliefern. Es wird gar nicht erst in die Zieldatei geschaut."""
    runner_mod = _runner()
    problems = runner_mod._validate_ln02_rgb("pdal.exe", str(tmp_path / "x.laz"),
                                             {"rgb_min": 0, "rgb_max": 0})
    assert problems and "schwarze Kachel" in problems[0]


# ─── Farb-Join: echte Dateien, echter Durchlauf ────────────────────────────────
# Der Join arbeitet ohne laspy direkt auf den LAS-Bytes (numpy + struct) und laesst
# sich deshalb hier vollstaendig ausfuehren - nicht nur auf Quelltext-Ebene pruefen.

def _make_las(point_format, point_length, records, scales, offsets,
              minor_version=4, bounds=None):
    """Baut eine minimale, gueltige LAS-Datei ohne VLRs im Speicher."""
    import struct
    header_size = 375 if minor_version >= 4 else 227
    n = len(records) // point_length
    h = bytearray(header_size)
    h[0:4] = b"LASF"
    struct.pack_into("<H", h, 6, 17)                 # global_encoding
    h[24], h[25] = 1, minor_version
    struct.pack_into("<HII", h, 94, header_size, header_size, 0)
    h[104] = point_format
    struct.pack_into("<H", h, 105, point_length)
    struct.pack_into("<I", h, 107, n if minor_version < 4 else 0)
    struct.pack_into("<3d", h, 131, *scales)
    struct.pack_into("<3d", h, 155, *offsets)
    bx = bounds or (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    struct.pack_into("<6d", h, 179, *bx)             # maxx,minx,maxy,miny,maxz,minz
    if minor_version >= 4:
        struct.pack_into("<Q", h, 247, n)
    return bytes(h) + records


def _pf7_records(xyz_raw, rgb, classification=2, gps=0.0):
    """PF7-Punktrecords (36 Byte) aus Rohkoordinaten und Farben."""
    import struct
    out = bytearray()
    for (x, y, z), (r, g, b) in zip(xyz_raw, rgb):
        rec = bytearray(36)
        struct.pack_into("<3i", rec, 0, x, y, z)
        struct.pack_into("<H", rec, 12, 100)          # intensity
        rec[16] = classification
        struct.pack_into("<d", rec, 22, gps)
        struct.pack_into("<3H", rec, 30, r, g, b)
        out += rec
    return bytes(out)


def _pf1_records(xyz_raw):
    """PF1-Punktrecords (28 Byte) - so kommt die Kachel von GeoSuite zurueck."""
    import struct
    out = bytearray()
    for (x, y, z) in xyz_raw:
        rec = bytearray(28)
        struct.pack_into("<3i", rec, 0, x, y, z)
        out += rec
    return bytes(out)


def _write_join_pair(tmp_path, dz=-0.37, shuffle=False, drop=0):
    """Master (PF7, LN02-Zielgitter) + GeoSuite-Ausgabe (PF1, anderes Gitter).
    Die GeoSuite-Datei nutzt bewusst ein ANDERES scale/offset - damit ist belegt, dass
    der Join die echten Koordinaten rekonstruiert und nicht Rohwerte vergleicht."""
    ox, oy = 2713000.0, 1206000.0
    dxy = [(0.0, 0.0), (1.23, 4.56), (500.0, 500.0), (999.99, 999.99), (250.5, 750.25)]
    z_lhn95 = [1000.00, 1001.50, 1100.25, 1234.56, 999.01]
    rgb = [(0, 0, 0), (65280, 32000, 100), (255, 255, 255), (1000, 2000, 3000), (7, 8, 9)]

    master_raw = [(round(dx / 0.01), round(dy / 0.01), round(z / 0.01))
                  for (dx, dy), z in zip(dxy, z_lhn95)]
    geo_raw = [(round((ox + dx) / 0.01), round((oy + dy) / 0.01), round((z + dz) / 0.01))
               for (dx, dy), z in zip(dxy, z_lhn95)]
    if shuffle:
        geo_raw = geo_raw[1:] + geo_raw[:1]
    if drop:
        geo_raw = geo_raw[:-drop]

    master = tmp_path / "master.las"
    geo = tmp_path / "geo.las"
    master.write_bytes(_make_las(7, 36, _pf7_records(master_raw, rgb),
                                 (0.01, 0.01, 0.01), (ox, oy, 0.0)))
    geo.write_bytes(_make_las(1, 28, _pf1_records(geo_raw),
                              (0.01, 0.01, 0.01), (0.0, 0.0, 0.0), minor_version=2))
    return str(master), str(geo), ox, oy, z_lhn95, rgb, dz


def test_ln02_join_master_roundtrip(tmp_path):
    """Vollstaendiger Durchlauf: X/Y und Farbe kommen aus dem Master, Z verbatim aus
    der GeoSuite-Ausgabe. Der Join darf Z NICHT umrechnen."""
    import struct
    runner_mod = _runner()
    master, geo, ox, oy, z_lhn95, rgb, dz = _write_join_pair(tmp_path)
    out = str(tmp_path / "joined.las")

    stats = runner_mod._ln02_join_master(master, geo, out, ox, oy)

    assert stats["count"] == 5
    assert (stats["rgb_min"], stats["rgb_max"]) == (0, 65280)
    assert (stats["class_min"], stats["class_max"]) == (2, 2)
    assert stats["max_dx"] <= runner_mod.LN02_XY_TOLERANCE_M
    assert stats["max_dy"] <= runner_mod.LN02_XY_TOLERANCE_M

    info = runner_mod._las_header_info(out)
    assert info["point_format"] == 7 and info["point_length"] == 36
    assert info["point_count"] == 5

    raw = open(out, "rb").read()[info["offset_to_point_data"]:]
    for i, (z_src, (r, g, b)) in enumerate(zip(z_lhn95, rgb)):
        x, y, z = struct.unpack_from("<3i", raw, i * 36)
        rr, gg, bb = struct.unpack_from("<3H", raw, i * 36 + 30)
        # Z ist der LN02-Wert aus der GeoSuite-Datei, unveraendert uebernommen
        assert z == round((z_src + dz) / 0.01), f"Punkt {i}: Z falsch"
        # X/Y unveraendert aus dem Master (Zielgitter, offset = Kachelursprung)
        assert abs((x * 0.01 + ox) - (ox + [0.0, 1.23, 500.0, 999.99, 250.5][i])) < 1e-6
        # Farbe unveraendert
        assert (rr, gg, bb) == (r, g, b), f"Punkt {i}: RGB falsch"

    # min_z/max_z im Header nachgefuehrt - die Werte sind jetzt LN02
    maxz, minz = struct.unpack_from("<2d", open(out, "rb").read(375), 211)
    assert abs(minz - (min(z_lhn95) + dz)) < 1e-6
    assert abs(maxz - (max(z_lhn95) + dz)) < 1e-6
    assert abs(stats["measured"]["minz"] - (min(z_lhn95) + dz)) < 1e-6


def test_ln02_join_master_detects_reordering(tmp_path):
    """Wird die Punktreihenfolge von GeoSuite veraendert, wuerde der Index-Join die
    Farben auf die falschen Punkte schreiben - das muss auffallen."""
    import pytest
    runner_mod = _runner()
    master, geo, ox, oy, *_ = _write_join_pair(tmp_path, shuffle=True)

    with pytest.raises(ValueError) as e:
        runner_mod._ln02_join_master(master, geo, str(tmp_path / "j.las"), ox, oy)
    assert "Punktreihenfolge" in str(e.value)


def test_ln02_join_master_detects_count_mismatch(tmp_path):
    """Verliert GeoSuite Punkte, ist die Zuordnung ueber den Index nicht mehr
    definiert - harter Fehler statt stillem Versatz."""
    import pytest
    runner_mod = _runner()
    master, geo, ox, oy, *_ = _write_join_pair(tmp_path, drop=2)

    with pytest.raises(ValueError) as e:
        runner_mod._ln02_join_master(master, geo, str(tmp_path / "j.las"), ox, oy)
    assert "Punktanzahl unterschiedlich" in str(e.value)


def test_ln02_join_master_rejects_wrong_grid(tmp_path):
    """Liegt der Master nicht auf dem Zielgitter, waeren die unveraendert uebernommenen
    X/Y-Rohwerte falsch interpretiert."""
    import pytest
    runner_mod = _runner()
    master, geo, ox, oy, *_ = _write_join_pair(tmp_path)

    with pytest.raises(ValueError) as e:
        # Falscher Kachelursprung -> Master passt nicht zum Ziel
        runner_mod._ln02_join_master(master, geo, str(tmp_path / "j.las"), 2714000.0, oy)
    assert "Zielgitter" in str(e.value)


def test_ln02_join_master_rejects_non_pf7(tmp_path):
    """Zeigt der Master-Ordner versehentlich auf die GeoSuite-Ausgabe, ist das Format
    PF1 - der Join muss das melden statt Unsinn zu schreiben."""
    import pytest
    runner_mod = _runner()
    _master, geo, ox, oy, *_ = _write_join_pair(tmp_path)

    with pytest.raises(ValueError) as e:
        runner_mod._ln02_join_master(geo, geo, str(tmp_path / "j.las"), ox, oy)
    assert "PF7" in str(e.value) or "Punktformat" in str(e.value)


def test_las_header_info_reads_both_las_versions(tmp_path):
    """Punktanzahl steht in LAS 1.4 als uint64 an Offset 247, in 1.2 als uint32 an 107."""
    runner_mod = _runner()
    recs = _pf1_records([(1, 2, 3), (4, 5, 6)])

    p12 = tmp_path / "a12.las"
    p12.write_bytes(_make_las(1, 28, recs, (0.01,) * 3, (0.0,) * 3, minor_version=2))
    i12 = runner_mod._las_header_info(str(p12))
    assert (i12["version_minor"], i12["point_count"], i12["header_size"]) == (2, 2, 227)
    assert not i12["compressed"]

    p14 = tmp_path / "a14.las"
    p14.write_bytes(_make_las(1, 28, recs, (0.01,) * 3, (0.0,) * 3, minor_version=4))
    i14 = runner_mod._las_header_info(str(p14))
    assert (i14["version_minor"], i14["point_count"], i14["header_size"]) == (4, 2, 375)


def test_las_header_info_flags_compression(tmp_path):
    """Bit 6/7 im Formatbyte markiert LASZIP - der Join braucht unkomprimierte Daten
    und muss das erkennen, damit _ensure_uncompressed greift."""
    runner_mod = _runner()
    p = tmp_path / "c.laz"
    p.write_bytes(_make_las(1 | 0x80, 28, _pf1_records([(1, 2, 3)]),
                            (0.01,) * 3, (0.0,) * 3))
    info = runner_mod._las_header_info(str(p))
    assert info["compressed"] is True
    assert info["point_format"] == 1


def test_ln02_worker_without_master_stays_pf6(tmp_path, monkeypatch):
    """Ohne Master-Ordner muss der Tab [LN02] unveraendert arbeiten: PDAL-Pipeline,
    PF6, keine Farbe. Bestehende Projekte duerfen sich nicht anders verhalten."""
    import json
    runner_mod = _runner()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    src_path = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LHN95.las"
    src_path.write_bytes(b"x")
    dst_path = out_dir / "2026_G_TIN_raw_2713_1206_LV95_LN02.laz"
    captured = {}

    def fake_pipeline(pdal_exe, pipeline_path, metadata_path=None):
        captured["stages"] = json.loads(
            open(pipeline_path, encoding="utf-8").read())["pipeline"]
        open(captured["stages"][-1]["filename"], "wb").write(b"tmp")
        return {"stages": {"filters.stats": {"statistic": [
            {"name": "Classification", "minimum": 1, "maximum": 2}]}}}

    monkeypatch.setattr(runner_mod, "_run_pdal_pipeline", fake_pipeline)
    monkeypatch.setattr(runner_mod, "_inject_reference_vlrs", lambda path: 0)
    monkeypatch.setattr(runner_mod, "_pdal_classification_range", lambda exe, path: (1, 2))
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path: _ln02_target_metadata())
    # Wuerde der Join trotzdem laufen, faellt der Test hier um
    monkeypatch.setattr(runner_mod, "_ln02_join_master",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("Join darf ohne Master nicht laufen")))

    status, _name, err, _w = runner_mod._ln02_tile_worker(
        (str(src_path), str(dst_path), "pdal.exe", str(tmp_path),
         2713000.0, 1206000.0, None))

    assert (status, err) == ("written", None)
    assert captured["stages"][-1]["dataformat_id"] == 6


def test_ln02_worker_with_master_produces_pf7(tmp_path, monkeypatch):
    """Verdrahtung des Farb-Pfads: der Join laeuft echt, sein Ergebnis geht als
    Eingabe in die PDAL-Ausgabe (PF7), und die Zwischendateien im Staging werden
    wieder aufgeraeumt."""
    import json
    import shutil as _shutil
    runner_mod = _runner()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    pair_dir = tmp_path / "pair"
    pair_dir.mkdir()

    master, geo, ox, oy, *_ = _write_join_pair(pair_dir)
    # Quelldatei so benennen, wie sie nach GeoSuite heisst
    src_path = tmp_path / "2026_G_TIN_raw_2713_1206_LV95_LN02.las"
    _shutil.copy2(geo, src_path)
    dst_path = out_dir / "2026_G_TIN_raw_2713_1206_LV95_LN02.laz"

    captured = {}

    def fake_pipeline(pdal_exe, pipeline_path, metadata_path=None):
        stages = json.loads(open(pipeline_path, encoding="utf-8").read())["pipeline"]
        captured["stages"] = stages
        # PDAL simulieren: Eingabe (die zusammengefuegte Datei) an den Zielort kopieren
        _shutil.copy2(stages[0]["filename"], stages[-1]["filename"])
        return {}

    monkeypatch.setattr(runner_mod, "_run_pdal_pipeline", fake_pipeline)
    monkeypatch.setattr(runner_mod, "_inject_reference_vlrs", lambda path: 0)
    monkeypatch.setattr(runner_mod, "_pdal_classification_range", lambda exe, path: (2, 2))
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path: _ln02_target_metadata(
                            dataformat_id=7, point_length=36, count=5,
                            minx=2713000.0, maxx=2713999.99,
                            miny=1206000.0, maxy=1206999.99,
                            minz=998.64, maxz=1234.19))

    # RGB-Kontrolle: 'pdal info --stats' auf der Zieldatei simulieren
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps({"stats": {"statistic": [
                {"name": c, "minimum": 0, "maximum": 65280}
                for c in ("Red", "Green", "Blue")]}})
            stderr = ""
        return R()
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    status, name, err, warnings = runner_mod._ln02_tile_worker(
        (str(src_path), str(dst_path), "pdal.exe", str(staging), ox, oy, master))

    assert (status, err) == ("written", None), err
    assert name == dst_path.name
    assert dst_path.is_file()

    # PDAL bekommt die zusammengefuegte Datei, nicht die GeoSuite-Kachel
    assert captured["stages"][0]["filename"].endswith(".las")
    assert "joined_" in os.path.basename(captured["stages"][0]["filename"])
    writer = captured["stages"][-1]
    assert writer["dataformat_id"] == 7          # PF7 mit RGB
    assert writer["global_encoding"] == 17
    assert (writer["offset_x"], writer["offset_y"], writer["offset_z"]) == (ox, oy, 0)
    assert writer["compression"] == "laszip"
    # Ohne Farb-Join haengt filters.stats dazwischen - mit Join liefert der die Werte
    assert [s["type"] for s in captured["stages"]] == ["readers.las", "writers.las"]

    # Staging wieder sauber: keine zusammengefuegten oder entpackten Reste
    assert not list(staging.glob("joined_*")), "Zwischendatei nicht aufgeraeumt"
    assert not list(staging.glob("unpacked_*"))
    assert not list(staging.glob("pipeline_ln02_*.json"))

    # Die Farbe ist wirklich im Ergebnis
    info = runner_mod._las_header_info(str(dst_path))
    assert info["point_format"] == 7 and info["point_count"] == 5
