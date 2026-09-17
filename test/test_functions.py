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


def test_area_from_input_path():
    """AREA wird lexikalisch aus dem Input-Pfad abgeleitet - je nach Tab
    unterschiedlich tief in der Ablagestruktur.

        [1]  DOP : ...\2026\_MUSTER\DOP\LV95\01_INPUT_realityStudio        (4.-letzter)
        [2a] DSM : ...\2026\_MUSTER\DSM\LV95_LHN95\01_INPUT_realityStudio  (4.-letzter)
        [2b] LN02: ...\2026\_MUSTER\DSM\LV95_LN02\01_DSM_LAZ\01_INPUT_...  (5.-letzter)

    Die Ableitung greift NICHT auf die Platte zu - sie muss auch funktionieren,
    wenn das Netzlaufwerk gerade offline ist."""
    gui_mod = load_module_from_path(
        "gui_module", os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"))
    f = gui_mod._area_from_input_path
    d5 = gui_mod.AREA_PATH_DEPTH_LN02

    dop  = r"U:\VDI-Transfer\2026\_MUSTER\DOP\LV95\01_INPUT_realityStudio"
    dsm  = r"U:\VDI-Transfer\2026\_MUSTER\DSM\LV95_LHN95\01_INPUT_realityStudio"
    ln02 = r"U:\VDI-Transfer\2026\_MUSTER\DSM\LV95_LN02\01_DSM_LAZ\01_INPUT_GeoSuite_LN02"

    assert gui_mod.AREA_PATH_DEPTH == 4 and d5 == 5
    assert f(dop) == "_MUSTER"
    assert f(dsm) == "_MUSTER"
    assert f(ln02, d5) == "_MUSTER"
    # Der LN02-Pfad hat eine Ebene mehr (01_DSM_LAZ): mit der Standardtiefe 4 kaeme
    # "DSM" heraus statt des Gebietsnamens - daher die eigene Tiefe fuer Tab [2b]
    assert f(ln02) == "DSM"

    assert f(dop + "\\") == "_MUSTER"                  # abschliessender Backslash
    assert f(dop.replace("\\", "/")) == "_MUSTER"      # Schraegstriche
    assert f('"' + dop + '"') == "_MUSTER"             # aus der Zwischenablage kopiert
    assert f("  " + dop + "  ") == "_MUSTER"           # Leerzeichen aussen
    # UNC: die Freigabe ist ein Pfadteil, nicht zwei Ordner
    assert f(r"\server\share\2026\GUPPENFIRN\DOP\LV95\01_INPUT") == "GUPPENFIRN"
    # Zu kurz - an der Stelle staende nur noch das Laufwerk
    assert f(r"C:\DOP\LV95\01_INPUT") is None
    assert f(r"C:\LV95\01_INPUT") is None
    assert f(ln02[:0] or "") is None
    assert f(None) is None


def test_area_autofill_in_allen_drei_tabs():
    """AREA-Vorbelegung in den Tabs [1], [2a] und [2b].

    Liefert der Pfad einen Namen, gewinnt er - auch gegen einen abweichenden
    Handeintrag (der kann von einem frueheren Gebiet stehengeblieben sein).
    Liefert der Pfad nichts, bleibt ein vorhandener Eintrag unangetastet.
    Die uebrigen Tabs (Create DSM-Raster / COGTIFF / COPC) haben die Vorbelegung
    bewusst NICHT - dort laufen spontane Verarbeitungen mit beliebigen Pfaden."""
    gui_mod = load_module_from_path(
        "gui_module", os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"))
    app = gui_mod.DMCConverterApp()
    try:
        d4 = gui_mod.AREA_PATH_DEPTH
        d5 = gui_mod.AREA_PATH_DEPTH_LN02
        tabs = [
            (app._in_var, app._area_var, d4,
             r"U:\VDI-Transfer\2026\_MUSTER\DOP\LV95\01_INPUT_realityStudio"),
            (app._las_in_var, app._las_area_var, d4,
             r"U:\VDI-Transfer\2026\_MUSTER\DSM\LV95_LHN95\01_INPUT_realityStudio"),
            (app._ln02_in_var, app._ln02_area_var, d5,
             r"U:\VDI-Transfer\2026\_MUSTER\DSM\LV95_LN02\01_DSM_LAZ\01_INPUT_GeoSuite_LN02"),
        ]
        for in_var, area_var, depth, pfad in tabs:
            # leeres Feld wird gefuellt
            area_var.set("")
            in_var.set(pfad)
            app._autofill_area(in_var, area_var, depth)
            assert area_var.get() == "_MUSTER"

            # abweichender Handeintrag: der Pfad gewinnt
            area_var.set("ALTES_GEBIET")
            app._autofill_area(in_var, area_var, depth)
            assert area_var.get() == "_MUSTER"

            # Pfad liefert nichts -> Handeintrag bleibt stehen, nichts wird geraten
            area_var.set("HANDEINGABE")
            in_var.set(r"C:\LV95\01_INPUT")
            app._autofill_area(in_var, area_var, depth)
            assert area_var.get() == "HANDEINGABE"

        # Die Eingabefelder loesen die Vorbelegung selbst aus
        for attr in ("_in_entry", "_las_in_entry", "_ln02_in_entry"):
            entry = getattr(app, attr)
            for seq in ("<FocusOut>", "<Return>", "<<Paste>>"):
                assert entry.bind(seq), f"{attr}: {seq} nicht gebunden"

        # Die uebrigen Tabs bleiben ohne Vorbelegung
        for attr in ("_dsm_in_entry", "_cog_in_entry", "_copc_in_entry"):
            assert not hasattr(app, attr), f"{attr} sollte keine Vorbelegung haben"
    finally:
        app.destroy()


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
        # Band-Kuerzel im Namen: ohne erkannte Datei-Info gilt RGBN (4-BAND ist
        # seit der Umstellung der Standard)
        assert "2026_GUPPENFIRN_DOP_10cm_RGBN_<NAME>_LV95.tif" in text
        assert "checkData" not in text
        # QC-Mosaik: eigener Unterordner, 'checkData' anstelle des Kachelnamens
        app._create_cog_var.set(True)
        app._on_create_cog_toggle()
        text = app._name_preview_lbl.cget("text")
        assert "cog_QC\\2026_GUPPENFIRN_DOP_10cm_RGBN_checkData_LV95.tif" in text
        # Ein 3-Band-Auszug schlaegt auf beide Namen durch
        app._band_var.set(gui_mod.BAND_NRG)
        text = app._name_preview_lbl.cget("text")
        assert "2026_GUPPENFIRN_DOP_10cm_NRG_<NAME>_LV95.tif" in text
        assert "cog_QC\\2026_GUPPENFIRN_DOP_10cm_NRG_checkData_LV95.tif" in text
        # 3-Band-Input ohne Auszug -> RGB statt RGBN
        app._band_var.set(gui_mod.BAND_KEEP)
        app._apply_band_availability(3)
        assert "2026_GUPPENFIRN_DOP_10cm_RGB_<NAME>_LV95.tif" in app._name_preview_lbl.cget("text")

    finally:
        app.destroy()


def test_band_token_matches_between_gui_and_runner():
    """Das Kuerzel im Dateinamen entsteht in der GUI (Vorschau, Log-Name) und im
    Runner (echter Kachelname) getrennt - beide muessen dasselbe liefern."""
    gui_mod = load_module_from_path(
        "gui_module", os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"))
    runner_mod = load_module_from_path(
        "runner_module", os.path.join(PROJECT_ROOT, "process_scripts", "_osgeo_runner.py"))
    for mode, count, expected in [
        ("keep", 4, "RGBN"), ("keep", 3, "RGB"), ("keep", 5, "5BAND"),
        ("rgb", 4, "RGB"), ("nrg", 4, "NRG"),
        ("keep", None, "RGBN"),          # Bandzahl unbekannt -> Normalfall 4-BAND
    ]:
        assert gui_mod._band_token(mode, count) == expected, (mode, count)
        assert runner_mod._band_token(mode, count) == expected, (mode, count)


def test_nodata_choices_follow_band_count():
    """Die NoData-Auswahl beschreibt die Ausgabe: 4-BAND -> vier Werte. Inhaltlich
    aendert das nichts (der Runner fuellt auf), die Anzeige darf aber nicht luegen."""
    gui_mod = load_module_from_path(
        "gui_module", os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"))
    assert gui_mod._nodata_choices(4) == ["0 0 0 0", "255 255 255 255"]
    assert gui_mod._nodata_choices(3) == ["0 0 0", "255 255 255"]
    assert gui_mod._nodata_choices(None) == gui_mod.NODATA_CHOICES
    # Auffuellen mit dem letzten Wert / ueberzaehlige abschneiden - wie _nodata_per_band
    assert gui_mod._fit_nodata_text("0 0 0", 4) == "0 0 0 0"
    assert gui_mod._fit_nodata_text("255 255 255 255", 3) == "255 255 255"
    assert gui_mod._fit_nodata_text("0 0 0", None) == "0 0 0"
    # Ein Auszug liefert immer 3 Baender, unabhaengig von der Quelle
    assert gui_mod._output_band_count("nrg", 4) == 3
    assert gui_mod._output_band_count("keep", 4) == 4
    assert gui_mod._output_band_count("keep", 3) == 3
    # Ohne gelesene Datei-Info zaehlt die Zusage der Auswahl: "4-BAND (RGBN)" = 4
    assert gui_mod._output_band_count("keep", None) == 4
    assert gui_mod._output_band_count("nrg", None) == 3


def test_tiff_tab_band_selection():
    """Band-Ausgabe im TIFFconverter: Default ist 4-BAND (RGBN), die
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

        # 4-BAND-Input: Auswahl bleibt bedienbar
        app._apply_band_availability(4)
        assert str(app._band_combo.cget("state")) == "readonly"
        assert app._band_mode() == "nrg"

        # 3-BAND-Input: Auswahl sperren und auf 4-BAND zuruecksetzen,
        # sonst liefe der Job erst im Runner in einen Fehler
        app._apply_band_availability(3)
        assert str(app._band_combo.cget("state")) == "disabled"
        assert app._band_mode() == "keep"
        assert "3 Band" in app._band_hint_lbl.cget("text")

        # Unbekannte Bandzahl (keine Datei-Info): frei waehlbar
        app._apply_band_availability(None)
        assert str(app._band_combo.cget("state")) == "readonly"

        # Die NoData-Auswahl folgt der Bandzahl der AUSGABE: bei 4-BAND (RGBN)
        # vier Werte, bei einem 3-Band-Auszug drei. Der Runner fuellt fehlende
        # Werte ohnehin auf - die Anzeige soll die Ausgabe nur nicht falsch
        # beschreiben.
        app._apply_band_availability(4)
        assert app._nodata_var.get() == "0 0 0 0"
        assert list(app._nodata_combo.cget("values")) == ["0 0 0 0", "255 255 255 255"]
        app._band_var.set(gui_mod.BAND_RGB)
        assert app._nodata_var.get() == "0 0 0"
        app._band_var.set(gui_mod.BAND_KEEP)
        assert app._nodata_var.get() == "0 0 0 0"
        # Weisses NoData bleibt weiss, nur die Anzahl folgt
        app._nodata_var.set("255 255 255 255")
        app._band_var.set(gui_mod.BAND_NRG)
        assert app._nodata_var.get() == "255 255 255"
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
        assert "4-BAND" in str(e)

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


# ── Tab "[2b] DMC DSM - LASconverter [LN02]" ──────────────────────────────────
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
        assert ".copc.laz" not in text
        # Kein Thinning-Feld mehr - ausgeduennt wird ausschliesslich im Tab [LHN95]
        assert not hasattr(app, "_ln02_thin_var")

        # Das QC-COPC ist unabhaengig vom Raster zuschaltbar; die VPC gibt es nicht mehr
        assert not hasattr(app, "_ln02_create_vpc_var")
        app._ln02_create_copc_var.set(True)
        app._update_ln02_name_preview()
        text = app._ln02_name_preview_lbl.cget("text")
        assert "copc_QC\\2026_GUPPENFIRN_checkData_LV95_LN02.copc.laz" in text
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
    assert "warp_source = _fill_raster_nodata(" in src
    # Der Pfad des rohen Hillshade wird hineingereicht - erzeugt wird er dort, wo es den
    # vollgefuellten Zwischenstand gibt (siehe test_hillshade_comes_from_fully_filled_mosaic)
    assert "str(raw_hillshade_path), log)" in src
    assert src.index("_fill_raster_nodata(") < src.index("cutlineDSName=clip_shape_path")
    # Geclippt wird das gefuellte Raster, nicht mehr das rohe VRT
    assert "gdal.Warp(output_path, warp_source" in src
    # Die AUSGELIEFERTEN Raster bleiben komprimiert - LAS_STAGING_COMPRESS betrifft
    # ausschliesslich die Wegwerf-Zwischenraster im Staging.
    assert '"COMPRESS=LZW", "PREDICTOR=3", "BIGTIFF=YES", "TFW=YES"' in src

    fill = inspect.getsource(runner_mod._fill_raster_nodata)
    # Zurueck kommt das DSM mit ehrlichen Luecken; der Hillshade entsteht nebenbei
    # unter dem hineingereichten Pfad (frueher als zweite, vollgefuellte Kopie).
    assert "return str(filled_path)" in fill
    assert "raw_hillshade_path: str" in fill
    # Der Hillshade muss aus dem vollgefuellten Stand kommen, also VOR dem
    # Ruecksetzen der grossen Loecher gerechnet werden.
    assert fill.index("gdal.DEMProcessing(") < fill.index("arr[big] = LAS_CELL_NODATA")
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
    fill = inspect.getsource(runner_mod._fill_raster_nodata)

    # Quelle des Hillshade ist der vollgefuellte Stand: gdaldem bekommt das offene
    # Dataset direkt nach FillNodata - und damit zwingend vor dem Ruecksetzen.
    assert 'str(raw_hillshade_path), ds, "hillshade"' in fill
    assert fill.index("gdal.FillNodata(") < fill.index("gdal.DEMProcessing(")
    assert fill.index("gdal.DEMProcessing(") < fill.index("arr[big] = LAS_CELL_NODATA")
    # Niemals aus dem fertigen (geclippten) DSM
    assert 'str(raw_hillshade_path), output_path' not in src
    assert "output_path" not in fill

    # Weil die Quelle jetzt das Mosaik ist, muss das Zielgitter erzwungen werden
    hs_opts = src[src.index("hs_warp_options = gdal.WarpOptions("):]
    assert "outputBounds=(snap_minx, snap_miny, snap_maxx, snap_maxy)" in hs_opts
    assert "xRes=gsd, yRes=gsd" in hs_opts

    # Kontrolle statt Annahme: Deckungsgleichheit wird geprueft, nicht angenommen
    assert "nicht auf demselben Gitter" in src


def test_hillshade_has_no_unit_type():
    """Der Hillshade ist ein einheitenloser Grauwert. Aus den PDAL-Zellrastern
    (Vertikal-CRS) erbt er sonst 'Unit Type: metre' - gemessen an GUPPENFIRN.
    Die Einheit wird auf dem fertigen Hillshade entfernt, am DSM bleibt sie."""
    import inspect
    runner_mod = _runner()
    src = inspect.getsource(runner_mod._mosaic_las_raster)

    hs_part = src[src.index("hs_out_ds = gdal.Warp("):]
    assert 'hs_out_ds.GetRasterBand(1).SetUnitType("")' in hs_part
    # vor dem Schliessen, sonst wirkt es nicht auf die Datei
    assert hs_part.index("SetUnitType(") < hs_part.index("hs_out_ds = None")
    # nur am Hillshade, nie am DSM
    assert src.count("SetUnitType(") == 1
    assert "GetUnitType()" in src


def test_clearing_unit_type_after_warp_persists(tmp_path):
    """SetUnitType("") direkt auf dem von gdal.Warp zurueckgegebenen Dataset muss in
    der Datei ankommen - ohne .aux.xml und ohne den NoData-Wert anzutasten."""
    import os
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    gdal.UseExceptions()

    src_path = str(tmp_path / "hs_raw.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src_path, 4, 4, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((2714000.0, 0.5, 0.0, 1206002.0, 0.0, -0.5))
    ds.SetProjection("EPSG:2056")
    ds.GetRasterBand(1).SetUnitType("metre")
    ds.GetRasterBand(1).WriteArray(np.full((4, 4), 128, dtype="uint8"))
    ds = None

    out_path = str(tmp_path / "hs.tif")
    out = gdal.Warp(out_path, src_path, srcSRS="EPSG:2056", dstSRS="EPSG:2056",
                    dstNodata=255, creationOptions=["TILED=YES", "COMPRESS=LZW"])
    assert out.GetRasterBand(1).GetUnitType() == "metre"   # Warp reicht sie weiter
    out.GetRasterBand(1).SetUnitType("")
    out.FlushCache()
    out = None

    chk = gdal.Open(out_path)
    assert chk.GetRasterBand(1).GetUnitType() == ""
    assert chk.GetRasterBand(1).GetNoDataValue() == 255
    chk = None
    assert not os.path.exists(out_path + ".aux.xml")


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

    assert runner_mod._prepare_hillshade_values(path) == (4, 2)   # 4x255, 2x NoData

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


def test_las_tile_compression_is_explicit_for_laz(tmp_path):
    """Seit dem GeoSuite-Update (09.09.2026) liest REFRAME LAS 1.4 PF6/PF7 auch als
    .laz, die Zwischenstufe ist deshalb per Default LAZ. Die Kompression wird explizit
    gesetzt statt PDALs Endungs-Heuristik zu vertrauen: sonst koennte bei out_format
    'laz' ein unkomprimiertes LAS mit .laz-Endung entstehen, das GeoSuite ablehnt.
    Bei out_format 'las' darf die Option NICHT gesetzt sein."""
    import json

    runner_mod = _runner()
    captured = {}

    def make_fake(out_file):
        def fake_run(pdal_exe, pipeline_path, metadata_path=None):
            captured["stages"] = json.loads(
                open(pipeline_path, encoding="utf-8").read())["pipeline"]
            out_file.write_bytes(b"dummy")
        return fake_run

    runner_mod._pdal_info_metadata = lambda exe, path: {
        "count": 42, "minor_version": 4, "dataformat_id": 7}

    stem = "2026_G_TIN_raw_2713_1206_LV95_LHN95"
    job = {"stem": stem, "cell_bounds": (2713000.0, 1206000.0, 2714000.0, 1207000.0),
           "tiles": [os.path.join("X:", "in", "a.laz")]}

    for out_format, expected in (("laz", "laszip"), ("las", None)):
        out_file = tmp_path / f"{stem}.{out_format}"
        runner_mod._run_pdal_pipeline = make_fake(out_file)
        status, _name, err = runner_mod._las_cell_worker(
            (job, str(tmp_path), str(tmp_path), "pdal.exe",
             "POLYGON((0 0,1 0,1 1,0 0))", None, out_format, 1))
        assert (status, err) == ("written", None)
        writer = captured["stages"][-1]
        assert writer["filename"].endswith(f".{out_format}")
        assert writer.get("compression") == expected
        # Alles andere ist vom Container unabhaengig - Format, Gitter und CRS-Tag
        # bleiben in beiden Faellen identisch.
        assert (writer["minor_version"], writer["dataformat_id"]) == (4, 7)
        assert writer["scale_x"] == 0.01
        assert (writer["offset_x"], writer["offset_y"]) == (2713000.0, 1206000.0)


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
# ══════════════════════ QC-Ansichtsprodukte (COPC / COG) ══════════════════════

def _compound_srs_md(h=2056, v=5728):
    comps = [{"type": "ProjectedCRS", "id": {"authority": "EPSG", "code": h}}]
    if v:
        comps.append({"type": "VerticalCRS", "id": {"authority": "EPSG", "code": v}})
    return {"srs": {"json": {"components": comps}}}


def test_qc_copc_untwine_command():
    """untwine bekommt jede Kachel einzeln (reiner Dateiname, untwine laeuft im
    Kachelordner), das CRS explizit und einen Temp-Ordner im Staging - nie einen
    Ordner als Input, der alles darin mitlesen wuerde."""
    runner_mod = _runner()
    cmd = runner_mod._untwine_command(
        "untwine.exe", ["a_2713_1206_LV95_LN02.laz", "b_2714_1206_LV95_LN02.laz"],
        "X:/out/copc_QC/x_tmp.copc.laz", "Y:/staging/untwine_tmp", "EPSG:2056+5728")
    assert cmd[0] == "untwine.exe"
    assert cmd[cmd.index("-o") + 1] == "X:/out/copc_QC/x_tmp.copc.laz"
    assert cmd[cmd.index("--a_srs") + 1] == "EPSG:2056+5728"
    assert cmd[cmd.index("--temp_dir") + 1] == "Y:/staging/untwine_tmp"
    # Nur Optionen, die das untwine von QGIS 3.42 kennt (external/untwine, addArgs).
    # '--threads' gibt es erst in neueren Versionen - 3.42 bricht damit ab
    # ("Unexpected argument 'threads'", so in der Firmenumgebung aufgetreten).
    qgis342 = {"o", "output_dir", "output_file", "i", "files", "s", "single_file",
               "temp_dir", "cube", "level", "file_limit", "progress_fd", "progress_debug",
               "dims", "stats", "a_srs", "metadata", "no_srs"}
    assert {a.lstrip("-") for a in cmd[1:] if a.startswith("-")} <= qgis342
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs == ["a_2713_1206_LV95_LN02.laz", "b_2714_1206_LV95_LN02.laz"]


def test_detect_untwine_in_qgis_apps(tmp_path, monkeypatch):
    """QGIS 3.42 legt untwine direkt in <root>\\apps\\qgis ab, nicht in bin (so in der
    Firmenumgebung vorgefunden) - die Suche muss es dort finden."""
    gui_mod = load_module_from_path(
        "gui_module",
        os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"),
    )
    exe = tmp_path / "apps" / "qgis" / "untwine.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    monkeypatch.setenv("OSGEO4W_ROOT", str(tmp_path))
    found = gui_mod._detect_untwine_exe("")
    assert os.path.normcase(found) == os.path.normcase(str(exe))


def test_untwine_env_puts_qgis_bin_on_path(tmp_path, monkeypatch):
    """untwine aus <root>\\apps\\qgis braucht die DLLs aus <root>\\bin - der Ordner kommt
    vorne in den PATH. Liegt untwine selbst in bin, bleibt der PATH unveraendert."""
    runner_mod = _runner()
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", r"C:\Windows")
    env = runner_mod._untwine_env(str(tmp_path / "apps" / "qgis" / "untwine.exe"))
    assert env["PATH"].split(os.pathsep) == [str(tmp_path / "bin"), r"C:\Windows"]
    env = runner_mod._untwine_env(str(tmp_path / "bin" / "untwine.exe"))
    assert env["PATH"] == r"C:\Windows"


def test_qc_copc_validation():
    """Vollstaendig (Punktanzahl = Summe der Kacheln) und LV95/LN02 - sonst Fehler."""
    runner_mod = _runner()
    ok = dict(_compound_srs_md(), count=1000)
    assert runner_mod._validate_copc(ok, 1000) == []
    assert runner_mod._validate_copc(dict(ok, copc=True), 1000) == []
    assert "Punktanzahl" in runner_mod._validate_copc(ok, 1001)[0]
    assert runner_mod._validate_copc(dict(ok, copc=False), 1000)
    lhn95 = dict(_compound_srs_md(v=5729), count=1000)
    assert "EPSG:2056+5729" in runner_mod._validate_copc(lhn95, 1000)[0]
    # Ohne Hoehenbezug (z.B. Tiles aus Tab [LHN95]) - wenn so erwartet, in Ordnung
    assert runner_mod._validate_copc(dict(_compound_srs_md(v=None), count=5), 5,
                                     (2056, None)) == []


def test_qc_copc_is_placed_only_after_validation(tmp_path, monkeypatch):
    """Das COPC entsteht als Temp-Datei und kommt erst nach bestandener Pruefung an
    seinen Platz. Ein alter Stand wird vorher entfernt - eine veraltete Kontrolle
    soll nie liegen bleiben, auch nicht, wenn der neue Bau scheitert."""
    import pytest
    runner_mod = _runner()
    tiles_dir = tmp_path / "tiles"
    tiles_dir.mkdir()
    tiles = []
    for e in (2713, 2714):
        p = tiles_dir / f"2026_G_TIN_raw_{e}_1206_LV95_LN02.laz"
        p.write_bytes(b"x")
        tiles.append(str(p))
    untwine = tmp_path / "untwine.exe"
    untwine.write_bytes(b"")
    run_dir = tmp_path / "staging"
    run_dir.mkdir()
    copc_path = str(tiles_dir / "copc_QC" / "2026_G_checkData_LV95_LN02.copc.laz")
    tmp_copc = copc_path.replace(".copc.laz", "_tmp.copc.laz")
    calls = {}

    def fake_untwine(cmd, cwd):
        calls["cmd"], calls["cwd"] = cmd, cwd
        with open(cmd[cmd.index("-o") + 1], "wb") as f:
            f.write(b"copc")
        return 0, ""

    monkeypatch.setattr(runner_mod, "_collect_tile_facts",
                        lambda exe, paths, n: (500, [(2056, 5728)]))
    monkeypatch.setattr(runner_mod, "_run_untwine", fake_untwine)
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path, driver=None: dict(_compound_srs_md(), count=500))

    n = runner_mod._write_copc(tiles, copc_path, str(untwine), "pdal.exe",
                                  run_dir, 2, lambda msg: None, crs=(2056, 5728))
    assert n == 500
    assert os.path.isfile(copc_path)
    assert not os.path.exists(tmp_copc)
    assert calls["cwd"] == str(tiles_dir)
    assert [calls["cmd"][i + 1] for i, a in enumerate(calls["cmd"]) if a == "-i"] == \
        [os.path.basename(t) for t in tiles]
    assert not (run_dir / "untwine_tmp").exists()

    # Unvollstaendig -> Fehler, und weder der alte noch der neue Stand bleibt liegen
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path, driver=None: dict(_compound_srs_md(), count=499))
    with pytest.raises(RuntimeError, match="Punktanzahl"):
        runner_mod._write_copc(tiles, copc_path, str(untwine), "pdal.exe",
                                  run_dir, 2, lambda msg: None, crs=(2056, 5728))
    assert not os.path.exists(copc_path)
    assert not os.path.exists(tmp_copc)


def test_copc_tile_facts_worker_reports_errors_instead_of_raising(monkeypatch):
    """Eine unlesbare Kachel wird zurueckgemeldet, statt den Worker zu sprengen."""
    runner_mod = _runner()

    def boom(exe, path):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(runner_mod, "_pdal_info_metadata", boom)
    assert runner_mod._copc_tile_facts_worker(("pdal.exe", "x.laz")) == \
        ("x.laz", None, None, None, "kaputt")
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path: dict(_compound_srs_md(), count=12))
    assert runner_mod._copc_tile_facts_worker(("pdal.exe", "y.laz")) == \
        ("y.laz", 12, 2056, 5728, None)


def test_qc_products_never_fail_the_run():
    """COPC und COG sind Kontrollen neben der Lieferung: ein Fehler beim Bau gibt eine
    WARNUNG, der Lauf bleibt erfolgreich - die Kacheln sind das Produkt."""
    import inspect
    runner_mod = _runner()

    ln02 = inspect.getsource(runner_mod._process_las_ln02)
    copc_block = ln02.split("if create_copc:", 1)[1].split("Schritt 6", 1)[0]
    tiff = inspect.getsource(runner_mod._process)
    cog_block = tiff.split("if create_cog and written:", 1)[1].split("if not keep_staging", 1)[0]
    for block in (copc_block, cog_block):
        assert "except Exception" in block
        assert "WARNUNG" in block
        assert "raise" not in block
    # Die VPC ist ersetzt, nicht nur ausgeblendet
    assert not hasattr(runner_mod, "_write_vpc")
    assert "create_vpc" not in ln02


def test_cog_creation_options():
    """Profil wie das Mosaik in topo-COGTIFFconverter. QUALITY nur bei JPEG (auch fuer
    die Overviews), PREDICTOR=2 nur bei verlustfreier Kompression."""
    runner_mod = _runner()
    jpeg = runner_mod._cog_creation_options("JPEG", 85)
    for o in ("COMPRESS=JPEG", "QUALITY=85", "OVERVIEW_QUALITY=85", "OVERVIEWS=AUTO",
              "OVERVIEW_RESAMPLING=AVERAGE", "BLOCKSIZE=256", "BIGTIFF=YES"):
        assert o in jpeg
    assert not any(o.startswith("PREDICTOR") for o in jpeg)

    deflate = runner_mod._cog_creation_options("deflate", 85)
    assert "COMPRESS=DEFLATE" in deflate and "PREDICTOR=2" in deflate
    assert not any(o.startswith("QUALITY") for o in deflate)
    none = runner_mod._cog_creation_options("NONE")
    assert not any(o.startswith(("PREDICTOR", "QUALITY")) for o in none)


def test_qc_cog_end_to_end(tmp_path):
    """Echter GDAL-Lauf (nur unter OSGeo4W): 4-BAND-Kacheln mit NoData-Rand und einem
    als Alpha getaggten Band 4 -> COG mit JPEG, interner Maske und NIR als normalem
    Band, ohne NoData-Tag."""
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    from osgeo import osr
    gdal.UseExceptions()
    runner_mod = _runner()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2056)
    tiles = []
    for i, x0 in enumerate((2713000.0, 2713100.0)):
        p = str(tmp_path / f"2026_G_DOP_10cm_{2713 + i}_1206_LV95.tif")
        ds = gdal.GetDriverByName("GTiff").Create(p, 1000, 1000, 4, gdal.GDT_Byte,
                                                   ["PHOTOMETRIC=RGB", "ALPHA=YES"])
        ds.SetGeoTransform((x0, 0.1, 0, 1207000.0, 0, -0.1))
        ds.SetProjection(srs.ExportToWkt())
        for b in range(1, 5):
            arr = np.full((1000, 1000), 100 + b, dtype=np.uint8)
            arr[:, :100] = 0          # NoData-Rand links in jeder Kachel
            band = ds.GetRasterBand(b)
            band.WriteArray(arr)
            band.SetNoDataValue(0)
        ds = None
        tiles.append(p)

    run_dir = tmp_path / "staging"
    run_dir.mkdir()
    cog = str(tmp_path / "cog_QC" / "2026_G_DOP_10cm_checkData_LV95.tif")
    runner_mod._write_cog_mosaic(tiles, cog, run_dir, lambda m: None, lambda *a: 1,
                                 compress="JPEG", quality=90, nodata_val=0.0,
                                 srs="EPSG:2056")

    assert runner_mod._check_cog(cog, 4, "JPEG", True) == []
    ds = gdal.Open(cog)
    mask = ds.GetRasterBand(1).GetMaskBand().ReadAsArray()
    assert mask[:, :100].max() == 0          # Rand ungueltig
    assert mask[:, 200:900].min() == 255     # Inhalt gueltig
    assert ds.GetRasterBand(4).GetColorInterpretation() != gdal.GCI_AlphaBand
    assert ds.GetRasterBand(1).GetNoDataValue() is None
    ds = None
    assert not os.path.exists(str(run_dir / "cog_scratch.tif"))
    assert not os.path.exists(cog.replace(".tif", "_tmp.tif"))

    # Verlustfrei ebenfalls Maske statt NoData-Tag - hier 3 Werte auf 4 Baender
    cog2 = str(tmp_path / "cog_QC" / "mosaik_deflate.tif")
    runner_mod._write_cog_mosaic(tiles, cog2, run_dir, lambda m: None, lambda *a: 1,
                                 compress="DEFLATE", nodata_val=[0, 0, 0], srs="EPSG:2056")
    assert runner_mod._check_cog(cog2, 4, "DEFLATE", True) == []
    ds = gdal.Open(cog2)
    mask = ds.GetRasterBand(1).GetMaskBand().ReadAsArray()
    assert mask[:, :100].max() == 0 and mask[:, 200:900].min() == 255
    assert ds.GetRasterBand(1).GetNoDataValue() is None
    ds = None


def test_qc_cog_mask_matches_tile_nodata(tmp_path):
    """QC-COG des TIFFconverters: ungueltig, sobald EIN Band NoData traegt - wie der
    NoData-Tag der Kacheln (gilt pro Band). 'Create COGTIFF' bleibt bei 'alle Baender'."""
    import inspect
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    gdal.UseExceptions()
    runner_mod = _runner()

    pixels = [(0, 0, 0), (0, 12, 7), (12, 0, 7), (5, 5, 5)]
    masks = {}
    for any_band in (False, True):
        p = str(tmp_path / f"mask_{any_band}.tif")
        ds = gdal.GetDriverByName("GTiff").Create(p, len(pixels), 1, 3, gdal.GDT_Byte)
        for b in range(3):
            ds.GetRasterBand(b + 1).WriteArray(
                np.array([[px[b] for px in pixels]], dtype=np.uint8))
        runner_mod._write_nodata_mask(ds, [0.0, 0.0, 0.0], any_band=any_band)
        ds = None
        ds = gdal.Open(p)
        masks[any_band] = ds.GetRasterBand(1).GetMaskBand().ReadAsArray()[0].tolist()
        ds = None
    assert masks[False] == [0, 255, 255, 255]
    assert masks[True] == [0, 0, 0, 255]

    assert "mask_any_band=True" in inspect.getsource(runner_mod._process)
    assert "mask_any_band" not in inspect.getsource(runner_mod._create_cog)


# ══════════════════════ Tabs "Create COGTIFF" / "Create COPC" ══════════════════════

def test_copc_crs_from_tiles():
    """Tab 'Create COPC': das CRS kommt von den Kacheln. Widersprechen sie sich, ist
    das ein Fehler; traegt keine eines, wird EPSG:2056 mit Warnung gesetzt."""
    import pytest
    runner_mod = _runner()
    logged = []
    assert runner_mod._copc_crs_from_tiles([(2056, 5728)], logged.append) == (2056, 5728)
    assert runner_mod._copc_crs_from_tiles([(2056, None)], logged.append) == (2056, None)
    assert not logged
    assert runner_mod._copc_crs_from_tiles([(None, None)], logged.append) == (2056, None)
    assert "WARNUNG" in logged[-1]
    assert runner_mod._copc_crs_from_tiles([(2056, 5728), (None, None)],
                                           logged.append) == (2056, 5728)
    with pytest.raises(ValueError, match="verschiedene CRS"):
        runner_mod._copc_crs_from_tiles([(2056, 5728), (2056, 5729)], logged.append)
    assert runner_mod._fmt_crs(2056, 5728) == "EPSG:2056+5728"
    assert runner_mod._fmt_crs(2056) == "EPSG:2056"


def test_write_copc_takes_crs_from_tiles(tmp_path, monkeypatch):
    """Ohne Vorgabe bekommt untwine das CRS der Kacheln explizit, und die Pruefung
    erwartet genau dieses - hier LV95 ohne Hoehenbezug (z.B. Tiles aus Tab [LHN95])."""
    runner_mod = _runner()
    tile = tmp_path / "a.laz"
    tile.write_bytes(b"x")
    untwine = tmp_path / "untwine.exe"
    untwine.write_bytes(b"")
    calls = {}

    def fake_untwine(cmd, cwd):
        calls["cmd"] = cmd
        with open(cmd[cmd.index("-o") + 1], "wb") as f:
            f.write(b"copc")
        return 0, ""

    monkeypatch.setattr(runner_mod, "_collect_tile_facts",
                        lambda exe, paths, n: (10, [(2056, None)]))
    monkeypatch.setattr(runner_mod, "_run_untwine", fake_untwine)
    monkeypatch.setattr(runner_mod, "_pdal_info_metadata",
                        lambda exe, path, driver=None: dict(_compound_srs_md(v=None), count=10))
    out = str(tmp_path / "out" / "x.copc.laz")
    assert runner_mod._write_copc([str(tile)], out, str(untwine), "pdal.exe",
                                  tmp_path, 1, lambda m: None) == 10
    assert calls["cmd"][calls["cmd"].index("--a_srs") + 1] == "EPSG:2056"
    assert os.path.isfile(out)


def test_create_copc_excludes_output_from_inputs(tmp_path, monkeypatch):
    """Liegt die Ausgabe im Input-Ordner, darf ein alter Stand (und seine Temp-Datei)
    nicht wieder ins neue COPC eingehen. Das CRS bleibt 'von den Kacheln'."""
    runner_mod = _runner()
    src = tmp_path / "tiles"
    src.mkdir()
    for name in ("a.laz", "b.las", "out.copc.laz", "out_tmp.copc.laz"):
        (src / name).write_bytes(b"x")
    pdal = tmp_path / "pdal.exe"
    pdal.write_bytes(b"")
    captured = {}

    def fake_write_copc(tiles, copc_path, untwine_exe, pdal_exe, run_dir, n, log, crs=None):
        captured["tiles"], captured["crs"] = tiles, crs
        with open(copc_path, "wb") as f:
            f.write(b"copc")
        return 1

    monkeypatch.setattr(runner_mod, "_write_copc", fake_write_copc)
    staging = tmp_path / "staging"
    runner_mod._create_copc({
        "input_dir": str(src), "output_path": str(src / "out.copc.laz"),
        "untwine_exe": "untwine.exe", "pdal_exe": str(pdal),
        "staging_dir": str(staging), "num_workers": 1, "keep_staging": False})
    assert [os.path.basename(t) for t in captured["tiles"]] == ["a.laz", "b.las"]
    assert captured["crs"] is None
    assert not any(staging.iterdir())          # Staging aufgeraeumt


def _fake_osgeo(monkeypatch):
    """Minimales 'osgeo'-Modul, damit Runner-Funktionen mit 'from osgeo import gdal'
    ohne OSGeo4W testbar sind (die GDAL-Arbeit selbst ist dann gemockt)."""
    import sys
    import types
    osgeo = types.ModuleType("osgeo")
    osgeo.gdal = types.SimpleNamespace(UseExceptions=lambda: None)
    osgeo.ogr = types.SimpleNamespace(UseExceptions=lambda: None)
    osgeo.osr = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "osgeo", osgeo)


def test_create_cog_excludes_output_and_passes_settings(tmp_path, monkeypatch):
    """Tab 'Create COGTIFF': Bandauswahl, Kompression, Qualitaet, NoData-Werte und CRS
    gehen an die Mosaik-Funktion; eine Ausgabe im Input-Ordner wird nicht mitgemosaikt."""
    import pytest
    runner_mod = _runner()
    _fake_osgeo(monkeypatch)
    src = tmp_path / "tiles"
    src.mkdir()
    for name in ("t1.tif", "t2.tiff", "mosaik.tif", "mosaik_tmp.tif"):
        (src / name).write_bytes(b"x")
    captured = {}

    def fake_mosaic(tiles, cog_path, work_dir, log, progress, **kw):
        captured["tiles"], captured["kw"] = tiles, kw
        with open(cog_path, "wb") as f:
            f.write(b"cog")

    monkeypatch.setattr(runner_mod, "_raster_facts", lambda p: ("Byte", 0.0, 4))
    monkeypatch.setattr(runner_mod, "_resolve_tiles_srs", lambda tiles, log: "EPSG:2056")
    monkeypatch.setattr(runner_mod, "_write_cog_mosaic", fake_mosaic)
    cfg = {"input_dir": str(src), "output_path": str(src / "mosaik.tif"),
           "band_mode": "rgb", "compress": "JPEG", "quality": 85, "nodata": "0 0 0",
           "staging_dir": str(tmp_path / "staging"), "keep_staging": False}
    runner_mod._create_cog(cfg)
    assert [os.path.basename(t) for t in captured["tiles"]] == ["t1.tif", "t2.tiff"]
    assert captured["kw"] == {"band_mode": "rgb", "compress": "JPEG", "quality": 85,
                              "nodata_val": [0.0, 0.0, 0.0], "srs": "EPSG:2056"}

    # Maske auch verlustfrei; 3 Werte auf RGBN -> der letzte gilt auch fuer Band 4
    runner_mod._create_cog(dict(cfg, band_mode="keep", compress="DEFLATE"))
    assert captured["kw"]["nodata_val"] == [0.0, 0.0, 0.0, 0.0]
    # "(keine Maske)" kommt als None an -> keine Maske, der Kachel-Tag bleibt
    runner_mod._create_cog(dict(cfg, nodata=None))
    assert captured["kw"]["nodata_val"] is None
    with pytest.raises(ValueError, match="nur 3 Band"):
        runner_mod._create_cog(dict(cfg, nodata="0 0 0 0"))
    with pytest.raises(ValueError, match="passt nicht in Byte"):
        runner_mod._create_cog(dict(cfg, nodata="256"))

    # JPEG geht nur mit 8 bit - klare Meldung statt eines GDAL-Fehlers mitten im Lauf
    monkeypatch.setattr(runner_mod, "_raster_facts", lambda p: ("UInt16", None, 4))
    with pytest.raises(ValueError, match="8-bit"):
        runner_mod._create_cog(cfg)


def test_nodata_values():
    """GUI-Feld 'NoData-Werte': ein Wert je Band, fehlende = letzter Wert (0 0 0 auf
    RGBN = 0 0 0 0); mehr Werte als Baender oder ausserhalb des Datentyps -> Fehler."""
    import pytest
    runner_mod = _runner()
    assert runner_mod._parse_nodata("0 0 0") == [0.0, 0.0, 0.0]
    assert runner_mod._parse_nodata(" 255  255 255 ") == [255.0] * 3
    assert runner_mod._parse_nodata(0) == [0.0]
    assert runner_mod._parse_nodata(None) == runner_mod._parse_nodata("") == []
    with pytest.raises(ValueError, match="Leerzeichen"):
        runner_mod._parse_nodata("0,0,0")

    per_band = runner_mod._nodata_per_band
    assert per_band([0.0, 0.0, 0.0], 4, "Byte") == [0.0] * 4
    assert per_band([0.0], 3, "Byte") == [0.0] * 3
    assert per_band([-3.4028235e+38], 1, "Float32") == [-3.4028235e+38]
    with pytest.raises(ValueError, match="nur 3"):
        per_band([0.0] * 4, 3, "Byte")
    for bad in (256.0, -1.0, 0.5):
        with pytest.raises(ValueError, match="passt nicht in Byte"):
            per_band([bad], 3, "Byte")


def test_tiff_process_needs_one_nodata_value(tmp_path, monkeypatch):
    """TIFFconverter: die Kacheln tragen EINEN NoData-Tag fuer alle Baender (GeoTIFF) -
    verschiedene Werte je Band oder ein leeres Feld brechen vor dem ersten
    Schreibzugriff ab."""
    import pytest
    runner_mod = _runner()
    _fake_osgeo(monkeypatch)
    out = tmp_path / "out"
    cfg = {"jahr": "2026", "area": "G", "gsd": "10cm", "input_dir": str(tmp_path),
           "output_dir": str(out), "clip_shape_path": "clip.shp",
           "grid_shape_path": "grid.shp", "staging_dir": str(tmp_path / "staging"),
           "nodata": "0 0 255"}
    with pytest.raises(ValueError, match="denselben Wert"):
        runner_mod._process(cfg)
    with pytest.raises(ValueError, match="fehlen"):
        runner_mod._process(dict(cfg, nodata=""))
    assert not out.exists() and not (tmp_path / "staging").exists()


def test_cog_and_copc_tabs():
    """Die beiden eigenstaendigen Tabs: Defaults, JPEG-Qualitaet nur bei JPEG,
    Bandauswahl gesperrt bei 3-Band-Input, Ausgabenamen mit richtiger Endung."""
    import pytest
    gui_mod = load_module_from_path(
        "gui_module", os.path.join(PROJECT_ROOT, "GUI_DMCdataConverter.py"))
    assert gui_mod._normalize_cog_path("X:/a/mosaik") == "X:/a/mosaik.tif"
    assert gui_mod._normalize_cog_path("X:/a/mosaik.TIF") == "X:/a/mosaik.TIF"
    assert gui_mod._normalize_copc_path("X:/a/wolke") == "X:/a/wolke.copc.laz"
    assert gui_mod._normalize_copc_path("X:/a/wolke.laz") == "X:/a/wolke.copc.laz"
    assert gui_mod._normalize_copc_path("X:/a/wolke.copc.laz") == "X:/a/wolke.copc.laz"

    app = gui_mod.DMCConverterApp()
    try:
        n_tabs = app._notebook.index("end")
        tabs = [app._notebook.tab(i, "text") for i in range(n_tabs)]
        states = [str(app._notebook.tab(i, "state")) for i in range(n_tabs)]
        assert tabs[-2:] == ["Create COGTIFF", "Create COPC"]

        # Zwischen den Haupt-Tabs [1]/[2a]/[2b] und den optionalen Zusatz-Tabs steht ein
        # leerer, deaktivierter Platzhalter - rein optische Trennung. ttk kann Tabs nicht
        # anders auseinanderruecken: 'padding' wirkt INNERHALB eines Tabs und macht ihn
        # bloss breiter, der Rahmen waechst mit.
        spacer = [i for i, s in enumerate(states) if s == "disabled"]
        assert len(spacer) == 1
        assert tabs[spacer[0]].strip() == ""
        assert tabs[spacer[0] - 1].startswith("[2b]")
        assert tabs[spacer[0] + 1] == "Create DSM-Raster"

        # Jede Scroll-Flaeche ist registriert (Theming + Mausrad), auch die des DSM-Tabs.
        # Gezaehlt werden nur Tabs MIT Inhalt - der Platzhalter baut keine auf.
        inhalt = [t for t, s in zip(tabs, states) if s == "normal"]
        assert len(app._scroll_areas) == len(inhalt)
        assert app._canvas_for_widget(app._sf_dsm) is app._canvas_dsm

        assert app._cog_compress_var.get() == "JPEG"
        assert app._cog_quality_var.get() == "90"
        assert str(app._cog_quality_entry.cget("state")) == "normal"
        app._cog_compress_var.set("DEFLATE")
        assert str(app._cog_quality_entry.cget("state")) == "disabled"
        assert "NoData" not in app._cog_compress_hint_lbl.cget("text")

        # NoData-Werte: in beiden Tabs Default 0 0 0 0 - die Vorgabe ist 4-BAND
        # (RGBN), und das Feld soll die Ausgabe beschreiben, nicht drei Baender
        assert app._nodata_var.get() == app._cog_nodata_var.get() == "0 0 0 0"
        assert gui_mod._parse_nodata_text(" 0  0 0 ") == [0.0, 0.0, 0.0]
        with pytest.raises(ValueError):
            gui_mod._parse_nodata_text("0,0,0")

        app._apply_band_availability(3, app._cog_band_combo, app._cog_band_var,
                                     app._cog_band_hint_lbl)
        assert str(app._cog_band_combo.cget("state")) == "disabled"
        assert app._cog_band_var.get() == gui_mod.BAND_KEEP
        assert str(app._band_combo.cget("state")) == "readonly"   # TIFF-Tab unberuehrt
    finally:
        app.destroy()


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


def test_alpha_band_would_destroy_rgb_without_normalisation(tmp_path):
    """Kernregression der 4-BAND-Umstellung (RGBN).

    Band 4 eines RGBN-TIFF traegt haeufig den Alpha-Tag. GDAL wertet ein Alpha-Band
    als Gueltigkeitsmaske: gdal.BuildVRT schreibt dann in JEDES Band ein
    <UseMaskBand>, und das VRT liefert ueberall dort Nullen, wo das NIR 0 ist - in
    allen vier Baendern. Im NIR ist 0 aber ein plausibler Messwert (Wasser reflektiert
    im nahen Infrarot praktisch nicht), die RGB-Werte solcher Flaechen waeren damit
    still verloren. Bei 3-Band RGB konnte der Fall nicht auftreten: kein viertes Band.

    Der Test haelt beide Haelften fest: dass der Rohzustand die Daten zerstoert, und
    dass _normalize_alpha_band sie rettet.
    """
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    osr = pytest.importorskip("osgeo.osr")
    gdal.UseExceptions()
    runner_mod = _runner()

    src = str(tmp_path / "rgbn.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, 64, 64, 4, gdal.GDT_Byte)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2056)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((2600000, 0.1, 0, 1200000, 0, -0.1))
    for b in range(1, 5):
        arr = np.full((64, 64), b * 50, dtype=np.uint8)
        if b == 4:
            arr[10:20, 10:20] = 0          # "Wasser": NIR = 0, RGB gueltig
        ds.GetRasterBand(b).WriteArray(arr)
    for b, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand,
                            gdal.GCI_BlueBand, gdal.GCI_AlphaBand), 1):
        ds.GetRasterBand(b).SetColorInterpretation(ci)
    ds = None

    vrt = str(tmp_path / "mosaic.vrt")
    gdal.BuildVRT(vrt, [src]).FlushCache()

    def water_pixel(path):
        d = gdal.Open(path)
        vals = [int(d.GetRasterBand(i).ReadAsArray(15, 15, 1, 1)[0, 0])
                for i in range(1, 5)]
        ci = d.GetRasterBand(4).GetColorInterpretation()
        d = None
        return vals, ci

    # Rohzustand: das Alpha-Band loescht RGB dort, wo das NIR 0 ist
    vals, _ = water_pixel(vrt)
    assert vals == [0, 0, 0, 0], f"Erwartet: unkorrigiertes VRT zerstoert RGB, war {vals}"

    # Nach der Korrektur bleiben die RGB-Werte stehen und der Alpha-Tag ist weg
    fixed = runner_mod._normalize_alpha_band(vrt, tmp_path, lambda *_: None)
    assert fixed != vrt, "Es muss eine korrigierte Kopie im Staging entstehen"
    vals, ci = water_pixel(fixed)
    assert vals == [50, 100, 150, 0], f"RGB muss erhalten bleiben, war {vals}"
    assert ci != gdal.GCI_AlphaBand, "Band 4 darf nicht mehr als Alpha getaggt sein"
    # Der Input bleibt unberuehrt - dort kann ein geliefertes True_Ortho.vrt liegen
    assert water_pixel(vrt)[0] == [0, 0, 0, 0]

    # Ohne Alpha-Tag ist nichts zu tun: die Quelle wird unveraendert durchgereicht
    assert runner_mod._normalize_alpha_band(fixed, tmp_path, lambda *_: None) == fixed


def test_nodata_collisions_are_resolved(tmp_path):
    """Wasser darf in der Lieferkachel NICHT als NoData gelten - nur echtes NoData.

    Ein GeoTIFF traegt EINEN NoData-Wert, und der wirkt pro Band: sobald ein Band ihn
    traegt, gilt das Pixel dort als NoData. Im NIR ist 0 aber ein echter Messwert
    (Wasser reflektiert im nahen Infrarot praktisch nicht). Solche Pixel werden darum
    um einen Digitalwert angehoben; Pixel, die den Wert in JEDEM Band tragen, sind
    echtes NoData (Rand, Clip-Ausschluss) und bleiben unangetastet.
    """
    import pytest
    gdal = pytest.importorskip("osgeo.gdal", reason="GDAL nur unter OSGeo4W verfuegbar")
    np = pytest.importorskip("numpy")
    gdal.UseExceptions()
    runner_mod = _runner()

    def build(path, nodata):
        """Wie eine fertige Lieferkachel: NoData-Tag gesetzt, Band 4 als 'Undefined'
        (sonst liest GDAL es als Alpha und die Maske waere das NIR selbst)."""
        ds = gdal.GetDriverByName("GTiff").Create(path, 100, 100, 4, gdal.GDT_Byte)
        for b in range(1, 5):
            arr = np.full((100, 100), b * 50, dtype=np.uint8)
            arr[:10, :] = nodata            # echter Rand: ALLE Baender
            if b == 4:
                arr[20:40, 20:40] = nodata  # "Wasser": nur NIR, 400 Pixel
            ds.GetRasterBand(b).WriteArray(arr)
            ds.GetRasterBand(b).SetNoDataValue(float(nodata))
        for i, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand,
                                gdal.GCI_BlueBand, gdal.GCI_Undefined), 1):
            ds.GetRasterBand(i).SetColorInterpretation(ci)
        ds = None

    p = str(tmp_path / "t.tif")
    build(p, 0)
    lines = []
    changed = runner_mod._resolve_nodata_collisions(p, 0.0, "RGBN", lines.append)
    assert changed == 400, f"nur die 400 Wasser-Pixel, war {changed}"

    ds = gdal.Open(p)
    bands = [ds.GetRasterBand(i).ReadAsArray() for i in range(1, 5)]
    # Wasser: NIR angehoben, RGB unangetastet - das Pixel ist jetzt vollstaendig gueltig
    assert bands[3][30, 30] == 1
    assert [int(b[30, 30]) for b in bands] == [50, 100, 150, 1]
    assert all(int(ds.GetRasterBand(i).GetMaskBand().ReadAsArray(30, 30, 1, 1)[0, 0]) == 255
               for i in range(1, 5)), "Wasser darf in keinem Band als NoData gelten"
    # Echter Rand: alle Baender bleiben auf 0 und damit NoData
    assert [int(b[5, 5]) for b in bands] == [0, 0, 0, 0]
    ds = None

    text = "\n".join(lines)
    assert "Band 4 (NIR)" in text and "400 Pixel" in text
    assert "Band 1" not in text              # die uebrigen Baender sind unauffaellig

    # Am oberen Rand des Datentyps wird nach unten ausgewichen (255 -> 254)
    q = str(tmp_path / "w.tif")
    build(q, 255)
    assert runner_mod._resolve_nodata_collisions(q, 255.0, "RGBN", lambda *_: None) == 400
    ds = gdal.Open(q)
    assert int(ds.GetRasterBand(4).ReadAsArray()[30, 30]) == 254
    assert int(ds.GetRasterBand(4).ReadAsArray()[5, 5]) == 255     # echter Rand bleibt
    ds = None

    # Ohne Kollision bleibt alles unberuehrt und das Log still
    c = str(tmp_path / "clean.tif")
    ds = gdal.GetDriverByName("GTiff").Create(c, 50, 50, 4, gdal.GDT_Byte)
    for b in range(1, 5):
        arr = np.full((50, 50), b * 50, dtype=np.uint8)
        arr[:5, :] = 0                       # nur gemeinsamer Rand
        ds.GetRasterBand(b).WriteArray(arr)
    ds = None
    lines = []
    assert runner_mod._resolve_nodata_collisions(c, 0.0, "RGBN", lines.append) == 0
    assert lines == []
