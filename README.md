# DMC Werkzeuge

GDAL- converter Tool für DMC-Daten DOP und DSM aus Reality Studio (technische 200m-Kacheln), als GUI mit zwei Tabs:

- **DMC - TIFFconverter** — clippt ein technisches DOP-Kachel-Mosaik auf eine manuell erfasste
  gueltige Flaeche (Randverzerrungen entfernen) und schneidet es anschliessend parallelisiert
  ins publikationsfaehige 1km x 1km-Grid um (Dateiname aus Attribut `NAME`).
- **DMC - LASconverter [LHN95]** — (LAS-Konvertierung für GeoSuite/reframe [LHN95 to LN02]).

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

6. **TIFF-Optionen**: klassisches TIFF (kein COG) + `.tfw`-Weltdatei je Ausgabekachel —
   Kompression, JPEG-Qualitaet, Blockgroesse, NoData-Wert.

7. **DMC TIFF KONVERTIEREN** starten.

Kacheln, die nach dem Zuschnitt zu 100% aus einem konstanten Wert bestehen (reines NoData,
z.B. ausserhalb der gueltigen Flaeche oder ausserhalb des Befliegungsgebiets), werden
automatisch geloescht (`.tif` + `.tfw`).

---

## Architektur

```
GUI_DMCdataConverter.py            (Standard-Python, tkinter)
        │
        │  JSON-Config (tempfile)
        ▼
process_scripts/_osgeo_runner.py   (OSGeo4W Python, GDAL)
    Aktionen: info / process
        │  1) Mosaik-VRT (uebernommen oder frisch gebaut)
        │  2) Cutline-Clip auf gueltige Flaeche  -> Staging
        │  3) Grid-Zuschnitt, parallelisiert (ProcessPoolExecutor)
        │
        │  stdout → live ins GUI-Log + Logdatei
        ▼
    logs/*.log
```

Die Trennung ermoeglicht es, das GUI mit jeder Standard-Python-Installation zu starten, ohne
OSGeo4W-Abhaengigkeiten im GUI-Prozess. Der Grid-Zuschnitt (Schritt 3) laeuft in mehreren
eigenen Prozessen (nicht Threads), da GDAL-Lesezugriffe so am zuverlaessigsten parallelisiert
werden koennen — jeder Worker oeffnet das geclippte Zwischenraster read-only fuer genau seine
zugewiesenen Kacheln.

---

## Voraussetzungen

- **GUI:** Python >= 3.6 (Standard-Installation, nur `tkinter` benoetigt).
- **GDAL-Verarbeitung:** [OSGeo4W](https://trac.osgeo.org/osgeo4w/) oder QGIS-Installation mit
  `python3.exe`/`python.exe` und `osgeo`-Paket. GDAL >= 3.1.
- **Staging-Laufwerk:** Schreibzugriff auf den Staging-Ordner (Standard `Y:\02_DMC_tempProcessingFolder`).

---

## Koordinatensystem

Fest **EPSG:2056** (CH1903+ / LV95), massgebend fuer swisstopo-Daten. Kachel-TIFFs mit
`.tfw`-Begleitdatei tragen i.d.R. keine eingebettete CRS-Information.

---

## Tests

```bash
python -m pytest -q
```

Leichtgewichtige Import-/Sanity-Checks (keine GDAL-Operationen, laufen auch ohne OSGeo4W).
