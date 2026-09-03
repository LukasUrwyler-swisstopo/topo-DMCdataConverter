# DMC Werkzeuge

Converter-Tool für rohe DMC-Daten aus RealityStudio (True-DOP und Punktwolken, technische
200m-Kacheln) ins swisstopo-Format "ch.spezialbefliegungen" (GDWH-STAC-ready), als GUI mit
zwei Tabs:

- **DMC - TIFFconverter** (GDAL) — clippt ein technisches DOP-Kachel-Mosaik auf eine manuell
  erfasste gueltige Flaeche (Randverzerrungen entfernen) und schneidet es anschliessend
  parallelisiert ins publikationsfaehige 1km x 1km-Grid um (Dateiname aus Attribut `NAME`).
- **DMC - LASconverter [LHN95]** (PDAL) — croppt technische LAZ-Kacheln (Punktwolke) per
  AOI-Shape, thinnt optional, schneidet sie parallelisiert ins 1km x 1km-Grid um (`.las`/`.laz`)
  und rastert optional zusaetzlich ein Gesamt-DSM (`.tif`/`.tfw`) fuer die ganze AOI. Hoehe
  bleibt LHN95 (kein Reframe im Tool — siehe unten); die `.las`-Ausgabe ist fuer den
  nachgelagerten Reframe LHN95→LN02 via GeoSuite gedacht.

Struktur und Styling analog zu `topo-COGTIFFconverter`.

## GUI starten

```bash
python GUI_DMCdataConverter.py
```

<img width="642" height="761" alt="image" src="https://github.com/user-attachments/assets/b93eb0db-9d16-40df-b315-3df98e56a77c" />


Beim ersten Start erkennt das GUI automatisch die OSGeo4W/QGIS-Installation. Der Pfad kann
ueber die Schaltflaeche **Aendern…** manuell gesetzt werden und wird in
`process_scripts/_dmc_config.json` gespeichert.

---

## Pipeline — Tab "DMC - TIFFconverter"

1. **Projekt-Parameter**: Jahr, AREA/AOI-Name, GSD (z.B. `10cm`) — ergeben zusammen mit dem
   Attribut `NAME` des Grid-Shapes die Ausgabebenennung:
   ```
   <JAHR>_<AREA>_DOP_<GSD>_<NAME>_LV95.tif  (+ .tfw)
   ```
   Beispiel: `2026_GUPPENFIRN_DOP_10cm_2713_1206_LV95.tif`

2. **Input-Ordner**: Ordner mit den technischen 200m x 200m-Kacheln (`.tif` + `.tfw`).
   Enthaelt der Ordner bereits ein Mosaik-VRT (z.B. `True_Ortho.vrt`), wird dieses direkt
   uebernommen — sonst wird automatisch ein frisches VRT aus allen gefundenen `.tif`-Kacheln
   gebaut (`gdalbuildvrt`-Aequivalent).

3. **Clip-Shape (gueltige Flaeche)**: Polygon-Shape, das die manuell erfasste gueltige Flaeche
   des Orthophotos beschreibt. Alles ausserhalb wird per Cutline-Clip (`gdal.Warp`) zu
   NoData — die Quellkacheln tragen bereits NoData=0 je Band, der Clip verwendet denselben Wert.

4. **Grid-Shape (1km x 1km)**: Shapefile mit Attributfeld `NAME`, liefert Geometrie und
   Benennung der Ausgabekacheln. Standardmaessig vorausgefuellt mit dem mitgelieferten
   `swissGRID_1km2_shp/chGRID_1km2.shp`. Wird bei Bedarf automatisch nach EPSG:2056
   reprojiziert.

5. **Staging & Parallelisierung**: Zwischenergebnisse (VRT, geclipptes Mosaik) werden in einem
   Staging-Ordner abgelegt (Standard `Y:\02_DMC_tempProcessingFolder`), damit mehrere Kerne
   parallel auf dieselbe geclippte Rasterquelle zugreifen koennen. **CPU-Kerne** steuert die
   Anzahl paralleler Prozesse fuer den Grid-Zuschnitt (Standard: 6). Nach erfolgreichem Lauf
   wird der projektspezifische Staging-Unterordner automatisch geloescht, sofern nicht
   **"Staging-Dateien behalten"** aktiviert ist.

6. **Ausgabe-Format** (kein GUI-Feld, automatisch): klassisches TIFF (kein COG) +
   `.tfw`-Weltdatei je Ausgabekachel, Blockgroesse fix 256, NoData fix 0 (alle Baender
   gleichermassen). Die Kompression wird von der ersten gefundenen Input-Kachel automatisch
   uebernommen (LZW/DEFLATE/ZSTD/unkomprimiert) — nie verlustbehaftet: liegt eine Input-Kachel
   ausnahmsweise JPEG-komprimiert vor, weicht die Ausgabe auf LZW aus, damit sie nie schlechter
   als der Input wird.

7. **DMC TIFF KONVERTIEREN** starten.

Vor dem Cutline-Clip prueft das Tool, ob der Pixelursprung des Mosaiks exakt auf ein Vielfaches
der Pixelgroesse faellt (sauberes Pixelraster, z.B. bei 10cm GSD auf `.0/.1/.2/…`-Koordinaten).
Nur wenn das zutrifft, treffen die 1km-Grid-Kachelgrenzen exakt auf bestehende Pixelkanten,
ohne dass GDAL rundet — bei einer Abweichung erscheint eine WARNUNG im Log. Cutline-Clip und
Grid-Zuschnitt behalten das Quell-Pixelraster explizit bei (kein implizites Resampling).

Kacheln, die nach dem Zuschnitt zu 100% aus einem konstanten Wert bestehen (reines NoData,
z.B. ausserhalb der gueltigen Flaeche oder ausserhalb des Befliegungsgebiets), werden
automatisch geloescht (`.tif` + `.tfw`).

---

## Pipeline — Tab "DMC - LASconverter [LHN95]"

Verarbeitet technische 200m-LAZ-Kacheln (Punktwolke, Koordinatensystem CH1903+/LV95 + LHN95)
via [PDAL](https://pdal.io/) (nicht GDAL — GDAL kennt keine Punktwolken). `pdal.exe` wird
automatisch erkannt (PATH, OSGeo4W-/QGIS-Installationspfade), kein eigenes GUI-Feld dafuer.

1. **Projekt-Parameter**: Jahr, AREA/AOI-Name, **Thinning** (Dropdown: kein Thinning / 0.1m /
   0.2m / 0.4m / 1m / 2m — Poisson-Disk-Sampling via `filters.sample`, Mindestabstand nach
   Reduktion), **Create Raster from LAZ** (Checkbox — blendet bei Aktivierung das GSD-Feld und
   den Raster-Output-Ordner ein). Ergibt zusammen mit dem Attribut `NAME` des Grid-Shapes die
   Ausgabebenennung:
   ```
   <JAHR>_<AREA>_TIN_[thinnedout<NN>_]raw_<NAME>_LV95_LHN95.<las|laz>   (pro 1km-Kachel)
   <JAHR>_<AREA>_TIN_[thinnedout<NN>_]raw_LV95_LHN95.tif  (+ .tfw)      (Gesamt-Raster, optional)
   ```
   `<NN>` = Thinning-Wert in Dezimetern, zweistellig (z.B. `04` bei 0.4m).
   Beispiel: `2026_GUPPENFIRN_TIN_thinnedout04_raw_2713_1206_LV95_LHN95.las`

2. **Input-Ordner**: Ordner mit den technischen 200m x 200m-LAZ-Kacheln.

3. **Output-Ordner (Punktwolken-Kacheln)** + **Ausgabeformat** (Dropdown `las`/`laz`, Default
   `las`): Ziel fuer die 1km-Grid-Kacheln. Default `las`, da die Weiterverarbeitung (Reframe
   LHN95→LN02) via GeoSuite unkomprimiertes LAS erwartet.

4. **Output-Ordner (DSM-Raster)**: nur sichtbar, wenn "Create Raster from LAZ" aktiv ist. Ziel
   fuer das eine Gesamt-TIFF+TFW der AOI.

5. **Clip-Shape (AOI)**: Bei den LAZ-Kacheln ein echter Crop (Punkte ausserhalb werden aus der
   Punktwolke entfernt), beim Raster eine Maskierung (ausserhalb -> NoData, Extent bleibt).

6. **Grid-Shape (1km x 1km)**: wie bei Tab 1 — Attribut `NAME`, Standard `chGRID_1km2.shp`,
   nur fuer die LAZ/LAS-Ausgabe relevant (das Raster ist ein einzelnes Gesamtbild ohne Kachelung).

7. **Staging & Parallelisierung**: analog Tab 1, eigener Staging-Unterordner (`<AREA>_<JAHR>_LAS`).

8. **DMC LAS KONVERTIEREN** starten.

### Ablauf im Detail

1. Metadaten (Bounding Box) aller Input-Kacheln parallel einlesen (`pdal info --metadata`,
   headerbasiert, kein Decompress der Punktdaten).
2. *(falls "Create Raster" aktiv, laeuft als Hintergrund-Thread parallel zu Schritt 3, nicht
   seriell davor)*: alle Kacheln mergen, optional thinnen, als Float32-Raster rastern
   (`writers.gdal`, IDW-Interpolation) — Pixelursprung auf ein sauberes GSD-Vielfaches gesnappt,
   danach per AOI-Cutline maskiert (NoData = `-3.4028235e+38`, analog GDWH-Konvention bei
   SB_DSM). Vor dem Staging-Aufraeumen wird auf diesen Hintergrund-Thread gewartet.
3. Pro 1km-Grid-Zelle (parallelisiert, `ProcessPoolExecutor`): ueberlappende Input-Kacheln
   mergen → Crop auf Zellgrenzen → Crop auf AOI-Polygon → optional thinnen → als `.las`/`.laz`
   schreiben. Zellen mit 0 Punkten nach dem Clip werden verworfen.

### Fachliche Absicherungen

- **SRS-Erzwingung**: Alle Reader setzen `override_srs = EPSG:2056+5729` (LV95 + LHN95) explizit
  — eine Input-Kachel mit fehlendem/falschem SRS-Tag fliesst nicht still mit falscher Referenz
  in den Merge ein.
- **Kein Reframe im Tool**: Hoehe bleibt LHN95. swisstopo selbst beschreibt die Transformation
  LHN95→LN02 als Naeherung ohne exakte Loesung (cm–dm-Genauigkeit, gebietsabhaengig) — dafuer
  wird bewusst die amtliche GeoSuite/REFRAME-Software separat verwendet (`.las`-Output).
- **`scale_x/y/z = 0.01`** fix in den Output-Kacheln gesetzt (Schweizer Konvention, keine
  uebertriebene Nachkommastellen-Praezision).
- **Punktzahl-Check**: nach dem Schreiben wird die Punktzahl der Ausgabekachel geprueft — 0
  Punkte nach Clip → Kachel wird verworfen statt einer leeren Datei.

---

## Architektur

```
GUI_DMCdataConverter.py            (Standard-Python, tkinter)
        │
        │  JSON-Config (tempfile)
        ▼
process_scripts/_osgeo_runner.py   (OSGeo4W Python, GDAL/OGR)
    Aktion "process"      (Tab "DMC - TIFFconverter"):
        │  1) Mosaik-VRT (uebernommen oder frisch gebaut)
        │  2) Cutline-Clip auf gueltige Flaeche  -> Staging
        │  3) Grid-Zuschnitt, parallelisiert (ProcessPoolExecutor)
        │
    Aktion "process_las"  (Tab "DMC - LASconverter [LHN95]"):
        │  1) Metadaten-Scan aller Kacheln, parallel (ProcessPoolExecutor)
        │  2) Raster-Build (optional) -> pdal.exe Subprocess, Hintergrund-Thread
        │  3) Grid-Zuschnitt, parallel  -> je 1 pdal.exe Subprocess pro Zelle
        │
        │  stdout → live ins GUI-Log + Logdatei
        ▼
    logs/*.log
```

Die Trennung ermoeglicht es, das GUI mit jeder Standard-Python-Installation zu starten, ohne
OSGeo4W-Abhaengigkeiten im GUI-Prozess. Der Grid-Zuschnitt laeuft in mehreren eigenen Prozessen
(nicht Threads), da GDAL-Lesezugriffe so am zuverlaessigsten parallelisiert werden koennen —
jeder Worker oeffnet das geclippte Zwischenraster (Tab 1) bzw. seine zugewiesenen LAZ-Kacheln
(Tab 2) read-only fuer genau seine Zuweisung. Punktwolken-Operationen laufen nicht ueber
GDAL/OGR (kennt keine Punktwolken), sondern als `pdal.exe`-Subprocess-Aufrufe mit generierten
JSON-Pipelines — orchestriert vom selben OSGeo4W-Python-Prozess.

---

## Voraussetzungen

- **GUI:** Python >= 3.6 (Standard-Installation, nur `tkinter` benoetigt).
- **GDAL-Verarbeitung (Tab 1, Orchestrierung Tab 2):** [OSGeo4W](https://trac.osgeo.org/osgeo4w/)
  oder QGIS-Installation mit `python3.exe`/`python.exe` und `osgeo`-Paket. GDAL >= 3.1.
- **PDAL-Verarbeitung (Tab 2):** `pdal.exe` im PATH oder Teil der OSGeo4W-/QGIS-Installation
  (wird automatisch erkannt, kein eigenes GUI-Feld). Entwickelt gegen PDAL 2.8 — beim ersten
  Lauf lohnt sich ein Blick ins Log auf die "Pixelraster-Check"-Zeile beim Raster-Build
  (prueft, ob `writers.gdal` die angeforderten `bounds` in dieser PDAL-Version unterstuetzt).
- **Staging-Laufwerk:** Schreibzugriff auf den Staging-Ordner (Standard `Y:\02_DMC_tempProcessingFolder`).

---

## Koordinatensystem

Fest **EPSG:2056** (CH1903+ / LV95), massgebend fuer swisstopo-Daten. Kachel-TIFFs mit
`.tfw`-Begleitdatei tragen i.d.R. keine eingebettete CRS-Information. Bei den Punktwolken-Daten
(Tab 2) ist die Hoehe fest **LHN95** (EPSG:5729) — Input wie Output; ein Reframe nach LN02
findet nicht im Tool statt (siehe Tab-2-Abschnitt oben).

---

## Tests

```bash
python -m pytest -q
```

Leichtgewichtige Import-/Sanity-Checks (keine GDAL-Operationen, laufen auch ohne OSGeo4W).
