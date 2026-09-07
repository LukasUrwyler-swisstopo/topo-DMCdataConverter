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
    # Crop-Bereich muss die Zelle rundum ueberragen (Puffer)
    assert crop["bounds"] == "([2713998.000,2715002.000],[1206998.000,1208002.000])"
    # Pipeline-Datei wird aufgeraeumt
    assert not list(tmp_path.glob("pipeline_*.json"))
