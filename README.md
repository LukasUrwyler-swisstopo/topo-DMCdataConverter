# DMC Werkzeuge

Converter-Tool für rohe DMC-Daten aus RealityStudio (True-DOP und Punktwolken, technische
200m-Kacheln) ins swisstopo-Format "ch.spezialbefliegungen" (GDWH-STAC-ready), als GUI mit
drei Tabs:

- **DMC - TIFFconverter** (GDAL) — clippt ein technisches DOP-Kachel-Mosaik auf eine manuell
  erfasste gueltige Flaeche (Randverzerrungen entfernen) und schneidet es anschliessend
  parallelisiert ins publikationsfaehige 1km x 1km-Grid um (Dateiname aus Attribut `NAME`).
- **DMC - LASconverter [LHN95]** (PDAL) — croppt technische LAZ-Kacheln (Punktwolke) per
  AOI-Shape, thinnt optional, schneidet sie parallelisiert ins 1km x 1km-Grid um (`.las`/`.laz`)
  und rastert optional zusaetzlich ein Gesamt-DSM + Hillshade (`.tif`/`.tfw`) fuer die ganze
  AOI. Hoehe bleibt LHN95 (kein Reframe im Tool — siehe unten); die `.las`-Ausgabe ist fuer den
  nachgelagerten Reframe LHN95→LN02 via GeoSuite gedacht.
- **DMC - LASconverter [LN02]** (PDAL) — nimmt die via GeoSuite nach LN02 reframten 1km-Kacheln
  entgegen und bringt sie unveraendert (kein Thinning, kein Crop, keine Neu-Kachelung) in die
  GDWH-taugliche Form: LAS 1.4 / Point Data Record Format 6,
  `global_encoding` 17, `scale` 0.01, Offset = Kachelursprung, CRS-Tag LV95+LN02 als byte-exakte
  Referenz-VLRs (identisch zu `SB_DSM_PUNKTWOLKE`). Optional ebenfalls DSM + Hillshade.

Struktur und Styling analog zu `topo-COGTIFFconverter`.

## GUI starten

```bash
python GUI_DMCdataConverter.py
```

<img width="635" height="764" alt="image" src="https://github.com/user-attachments/assets/1cac941f-ceaf-44b9-bade-fc79a711d6c9" />


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
   Reduktion), **Create DSM-Raster from LAZ** (Checkbox — blendet bei Aktivierung das GSD-Feld
   und den Raster-Output-Ordner ein; erzeugt neben dem DSM automatisch auch ein Hillshade, kein
   separates Haekchen dafuer noetig). Ergibt zusammen mit dem Attribut `NAME` des Grid-Shapes
   bzw. der GSD die Ausgabebenennung:
   ```
   <JAHR>_<AREA>_TIN_[thinnedout<NN>_]raw_<NAME>_LV95_LHN95.<las|laz>   (pro 1km-Kachel)
   <JAHR>_<AREA>_DSM_<GSD>cm_LV95_LHN95.tif  (+ .tfw)                   (DSM, optional)
   <JAHR>_<AREA>_hillshade_<GSD>cm_LV95_LHN95.tif  (+ .tfw)             (Hillshade, optional)
   ```
   `<NN>` = Thinning-Wert in Dezimetern, zweistellig (z.B. `04` bei 0.4m). `<GSD>cm` = Raster-
   Aufloesung in Zentimetern (z.B. `50cm` bei 0.5m).
   Beispiel: `2026_GUPPENFIRN_TIN_thinnedout04_raw_2713_1206_LV95_LHN95.las`,
   `2026_GUPPENFIRN_DSM_50cm_LV95_LHN95.tif`, `2026_GUPPENFIRN_hillshade_50cm_LV95_LHN95.tif`

2. **Input-Ordner**: Ordner mit den technischen 200m x 200m-LAZ-Kacheln.

3. **Output-Ordner (Punktwolken-Kacheln)** + **Ausgabeformat** (Dropdown `las`/`laz`, Default
   `las`): Ziel fuer die 1km-Grid-Kacheln. Default `las`, da die Weiterverarbeitung (Reframe
   LHN95→LN02) via GeoSuite unkomprimiertes LAS erwartet. Geschrieben wird **LAS 1.2 / Point
   Data Record Format 1** mit CRS-Tag `EPSG:2056` — siehe „GeoSuite-Kompatibilitaet" unten.

4. **Output-Ordner (DSM-Raster)**: nur sichtbar, wenn "Create DSM-Raster from LAZ" aktiv ist.
   Ziel fuer das eine DSM-TIFF+TFW und das Hillshade-TIFF+TFW der AOI (beide im selben Ordner).

5. **Clip-Shape (AOI)**: Bei den LAZ-Kacheln ein echter Crop (Punkte ausserhalb werden aus der
   Punktwolke entfernt), bei DSM und Hillshade je eine Maskierung (ausserhalb -> NoData, Extent
   bleibt).

6. **Grid-Shape (1km x 1km)**: wie bei Tab 1 — Attribut `NAME`, Standard `chGRID_1km2.shp`.
   Bestimmt die Kachelung der LAZ/LAS-Ausgabe und intern auch die Zellen des Raster-Builds;
   DSM und Hillshade werden trotzdem als je ein einzelnes Gesamtbild ausgeliefert (die
   Zell-Raster sind reine Zwischenprodukte im Staging-Ordner).

7. **Staging & Parallelisierung**: analog Tab 1, eigener Staging-Unterordner (`<AREA>_<JAHR>_LAS`).

8. **DMC LAS KONVERTIEREN** starten.

### Ablauf im Detail

1. Metadaten (Bounding Box) aller Input-Kacheln parallel einlesen (`pdal info --metadata`,
   headerbasiert, kein Decompress der Punktdaten).
2. Pro 1km-Grid-Zelle zwei Job-Arten, die gemeinsam in **einem** Prozess-Pool laufen
   (`ProcessPoolExecutor`, je Job ein eigener `pdal.exe`-Subprocess):
   - **Punktwolken-Kachel**: ueberlappende Input-Kacheln mergen → Crop auf Zellgrenzen →
     Crop auf AOI-Polygon → optional thinnen → als `.las`/`.laz` schreiben. Zellen mit 0
     Punkten nach dem Clip werden verworfen.
   - **DSM-Zelle** *(nur falls "Create DSM-Raster" aktiv)*: dieselbe Zelle, aber mit Puffer
     gecroppt (vollstaendige IDW-Nachbarschaft am Zellrand → nahtloses Mosaik), optional
     thinnen, als Float32-Raster rastern (`writers.gdal`, IDW). Geschrieben wird exakt der
     auf das GSD gesnappte Zellausschnitt, damit sich die Zell-Raster luecken- und
     ueberlappungsfrei zusammensetzen lassen.

   Jobs, die fehlschlagen, werden anschliessend **seriell wiederholt** (ein `pdal.exe` mit dem
   vollen Arbeitsspeicher) — bleibt der Fehler, ist er echt und der Lauf endet mit Fehler.
3. *(falls "Create DSM-Raster" aktiv)*: Zell-Raster als VRT mosaikieren, per AOI-Cutline
   maskieren (NoData = `-3.4028235e+38`, analog GDWH-Konvention bei SB_DSM) und aus diesem
   fertigen (bereits geclippten) DSM den Hillshade rechnen (`gdal.DEMProcessing`), ebenfalls
   per AOI-Cutline maskiert (NoData = `255`). Beim Mosaikieren wird ausserdem der
   NoData-Sentinel der Zell-Raster auf den GDWH-Sentinel umgesetzt — siehe unten.

Alle Punktwolken-Zugriffe lesen direkt aus den komprimierten `.laz`-Inputs (PDAL entpackt
on-the-fly, kein Zwischenschritt "erst alles zu LAS konvertieren"). Ob eine Punktwolken-Kachel
als `.las` oder `.laz` geschrieben wird, entscheidet sich rein an der Dateiendung des letzten
`writers.las`-Schritts — also erst nach Crop, AOI-Clip und Thinning, nicht davor.

### Fachliche Absicherungen

- **SRS-Erzwingung**: Alle Reader setzen `override_srs = EPSG:2056+5729` (LV95 + LHN95) explizit
  — eine Input-Kachel mit fehlendem/falschem SRS-Tag fliesst nicht still mit falscher Referenz
  in den Merge ein.
- **Kein Reframe im Tool**: Hoehe bleibt LHN95. swisstopo selbst beschreibt die Transformation
  LHN95→LN02 als Naeherung ohne exakte Loesung (cm–dm-Genauigkeit, gebietsabhaengig) — dafuer
  wird bewusst die amtliche GeoSuite/REFRAME-Software separat verwendet (`.las`-Output).
- **`scale_x/y/z = 0.01`** fix in den Output-Kacheln gesetzt (Schweizer Konvention, keine
  uebertriebene Nachkommastellen-Praezision).
- **GeoSuite-Kompatibilitaet der Zwischenausgabe**: die Tiles sind die Eingabe fuer den
  GeoSuite/REFRAME-Batch, deshalb wird das Ausgabeformat explizit gesetzt statt PDALs Defaults
  zu uebernehmen:
  | | ohne Angabe (PDAL 2.8.3, nachgemessen) | hier gesetzt |
  |---|---|---|
  | `minor_version` | **4** (LAS 1.4) | **2** (LAS 1.2) |
  | `dataformat_id` | **7** (PF6 + RGB, `point_length` 36) | **1** |
  | `global_encoding` | 16 (WKT-Bit) | 0 |
  | CRS im Header | 2× OGC-WKT-VLR `record_id` 2112 mit `EPSG:2056+5729` | GeoTIFF-Keys `EPSG:2056` (nur horizontal) |

  GeoSuite liest klassisches LAS (1.0–1.2, PF0–PF3) und lehnt LAS 1.4/PF7 mit
  `ERROR: File format incorrect ... unknown or unsupported format` ab — „Format" meint in LAS
  genau das Point Data Record Format. LAS 1.2/PF1 ohne Vertikal-Key ist exakt das Format, in dem
  die etablierte `SB_DSM_PUNKTWOLKE`-Lieferkette ihre Tiles fuehrt (siehe
  `topo-importDATAtoGDWH-STAC`, `4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py`: *„LAS 1.2, Point Data
  Record Format 1, keine CRS-Angabe im Header"*). Der Hoehenbezug wird bewusst **nicht** getaggt:
  REFRAME bekommt Ein- und Ausgangsrahmen aus der Batch-Konfiguration, den autoritativen
  LV95/LN02-Tag setzt erst der Tab [LN02].

  Nach dem Schreiben wird der Header jeder Kachel geprueft (die Metadaten werden fuer den
  Punktzahl-Check ohnehin gelesen) — stimmt er nicht, wird die Kachel verworfen statt eine fuer
  REFRAME unbrauchbare Datei im Output-Ordner zu hinterlassen.

  **Farbe:** PF7 fuehrt RGB, PF1 nicht. Fuehrt die Quelle Farbe, erscheint ein Hinweis im Log.
  Was im GDWH ankommt, verliert dadurch nichts — das Zielformat `SB_DSM_PUNKTWOLKE` ist PF6 und
  traegt ebenfalls keine RGB-Werte.

  **GPS-Time-Typ:** `global_encoding` Bit 0 sagt, wie die `GpsTime`-Werte zu lesen sind
  (0 = GPS Week Time, 1 = Adjusted Standard GPS Time). Das ist eine Eigenschaft der Daten, keine
  Formatentscheidung — der Wert wird deshalb aus den Quell-Tiles uebernommen (beim
  Metadaten-Scan ohnehin mitgelesen) und nur gesetzt, wenn **alle** Tiles ihn fuehren. Die
  `GpsTime`-Werte selbst werden nirgends veraendert.
- **NoData-Werte unterscheiden sich bewusst**: DSM = `-3.4028235e+38` (Float32-Minimum, GDWH-
  Konvention SB_DSM), Hillshade = `255` (Byte) — da 0 im Hillshade ein legitimer Schattenwert
  ist, wird die AOI-Maskierung rein geometrisch per Cutline vorgenommen (nicht ueber einen
  Pixelwert), damit echte Schattenpixel innerhalb der AOI nicht faelschlich zu NoData werden.
- **Zwei NoData-Sentinel im Raster-Build**: die Staging-Zell-Raster aus `writers.gdal` tragen
  `-9999` (`LAS_CELL_NODATA`), erst `gdal.Warp` setzt beim Mosaikieren den GDWH-Sentinel
  `-3.4028235e+38` (`LAS_RASTER_NODATA`). Grund: PDALs `writers.gdal` prueft den `nodata`-Wert
  gegen den Float32-Wertebereich und lehnt die **Bereichsgrenze selbst** ab —
  `Invalid nodata value -3.402823466e+38 for output data_type 'float'`, deterministisch fuer
  jede Zelle (verifiziert mit PDAL aus QGIS 3.42.1; der uebergebene Wert ist bitgenau
  `-FLT_MAX`, ein anderes Zahlen-Literal hilft also nicht). GDAL kennt diese Einschraenkung
  nicht — das Endprodukt traegt deshalb unveraendert den GDWH-konformen Sentinel.
- **Punktzahl-Check**: nach dem Schreiben wird die Punktzahl der Ausgabekachel geprueft — 0
  Punkte nach Clip → Kachel wird verworfen statt einer leeren Datei.

---

## Pipeline — Tab "DMC - LASconverter [LN02]"

Der nachgelagerte Schritt zum Tab **[LHN95]**. Dessen `.las`-Kacheln werden extern mit
**GeoSuite/REFRAME** von LHN95 nach LN02 reframt (nur die Höhe, X/Y bleiben LV95); dieser Tab
bringt das Ergebnis anschliessend in die GDWH-taugliche Form — strukturell kongruent zu
swissSURFACE3D bzw. `SB_DSM_PUNKTWOLKE` (Projekt `topo-importDATAtoGDWH-STAC`).

**Kein Reframe, keine Neu-Kachelung, kein Crop der Punktwolke** im Tool: der Input ist bereits
das fertige, AOI-gecroppte 1km-Grid. Das Footprint-/AOI-Shape wird ausschliesslich für die
Raster-Maskierung gebraucht und ist nur sichtbar, wenn die Raster-Option aktiv ist.

1. **Projekt-Parameter**: Jahr, AREA/AOI-Name, **Create DSM-Raster from LAS/LAZ** (Checkbox —
   blendet GSD-Feld, Raster-Output-Ordner und Footprint-/AOI-Shape ein). **Kein Thinning-Feld**:
   ausgedünnt wird ausschliesslich im Tab [LHN95], der Token `thinnedout<NN>_` gehört damit zur
   Kachel und wird — wie die Kachelkoordinaten `<E>_<N>` — aus dem Input-Dateinamen übernommen.
   Jahr und AREA kommen aus den GUI-Feldern:
   ```
   <JAHR>_<AREA>_TIN_[thinnedout<NN>_]raw_<E>_<N>_LV95_LN02.<las|laz>   (pro 1km-Kachel)
   <JAHR>_<AREA>_DSM_<GSD>cm_LV95_LN02.tif  (+ .tfw)                    (DSM, optional)
   <JAHR>_<AREA>_hillshade_<GSD>cm_LV95_LN02.tif  (+ .tfw)              (Hillshade, optional)
   ```
   Beispiel: `2026_GUPPENFIRN_TIN_thinnedout04_raw_2713_1206_LV95_LN02.laz` aus
   `2026_GUPPENFIRN_TIN_thinnedout04_raw_2713_1206_LV95_LHN95.las`

2. **Input-Ordner**: die nach LN02 reframten 1km-Kacheln (`.las` oder `.laz`). Der Dateiname
   **muss** auf `_<E>_<N>_LV95_<LHN95|LN02>.<las|laz>` enden — daraus wird der Kachelursprung
   deterministisch geparst (siehe unten). Passt der Name bei einer Kachel nicht, bricht der Lauf
   ab, **bevor** irgendetwas geschrieben wird.

3. **Output-Ordner (Kacheln)** + **Ausgabeformat** (Dropdown `las`/`laz`, Default `laz` —
   GDWH-Auslieferungsformat analog `SB_DSM_PUNKTWOLKE`). Muss ein anderer Ordner als der Input
   sein; die Quelldateien werden nie verändert.

4. **Output-Ordner (DSM-Raster)** und **Footprint / AOI-Shape**: nur sichtbar bei aktivierter
   Raster-Option. DSM und Hillshade landen als je ein Gesamtbild (`.tif` + `.tfw`) im selben
   Ordner, per Cutline maskiert (DSM NoData `-3.4028235e+38`, Hillshade NoData `255`) — exakt
   wie im Tab [LHN95].

5. **Datei-Info**: zeigt zusätzlich **LAS-Version/Point-Format** und **global_encoding** der
   Quelle. Steht dort bereits `LAS 1.4 / PF6` und `17`, ist die Kachel schon im Zielformat und
   wird nur kopiert statt konvertiert.

6. **Staging & Parallelisierung**: analog den anderen Tabs, eigener Unterordner
   (`<AREA>_<JAHR>_LN02`).

7. **DMC LAS KONVERTIEREN [LN02]** starten.

### Zielformat der Punktwolken-Kacheln

| Eigenschaft | Wert |
|---|---|
| LAS-Version | 1.4 |
| Point Data Record Format | 6 (`point_length` 30, `header_size` 375) |
| `global_encoding` | 17 — Bit 0 (Adjusted Standard GPS Time) + Bit 4 (WKT) |
| `scale_x/y/z` | 0.01 |
| `offset_x/y/z` | Kachelursprung `<E>*1000 / <N>*1000 / 0` |
| CRS-Tag | LV95 + LN02 (EPSG:2056 + EPSG:5728), VLR 34735 + 2112 |

Der **Offset kommt aus dem Dateinamen**, nicht aus dem Datenminimum: eine AOI-gecroppte Kachel
fängt sonst irgendwo mitten in der Zelle an. Die geparsten Kilometerwerte werden gegen die
Schweizer Landesgrenzen (LV95) plausibilisiert; zeigen zwei Input-Kacheln auf dieselbe Zelle
(die Ausgaben würden sich überschreiben), bricht der Lauf vorher ab.

### Warum die CRS-Tags byte-exakt injiziert werden

Die zwei CRS-VLRs (GeoTIFF-KeyDirectory `34735` + OGC-WKT `2112`) werden **nicht** von PDAL
erzeugen lassen, sondern als Bytes aus einer verifizierten swissSURFACE3D-Referenzkachel
übernommen (identisch zu `4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py`). Empirisch getestet, nicht
angenommen:

- PDAL erzeugt bei `a_srs="EPSG:2056+5728"` einen semantisch korrekten, aber **nicht
  byte-identischen** WKT (`COMPD_CS` statt `COMPOUNDCRS`) und schreibt den GeoTIFF-VLR `34735`
  gar nicht.
- `las2las -epsg 2056 -vertical_epsg 5728 -set_ogc_wkt` lieferte in der getesteten Version
  geodätisch **falsche** Oblique-Mercator-Parameter und liess die Vertikalkomponente
  (LN02/5728) ganz weg.
- PDALs eigene `writers.las`-Option `vlrs` verwirft VLRs mit `user_id "LASF_Projection"` still.

Bei `.laz`-Ausgabe wird zusätzlich zur Header-Verschiebung die **LASzip chunk table start
position** korrigiert (int64 am Anfang des Punktbereichs). Ohne diese Korrektur bleibt die Datei
für `pdal info --metadata` lesbar, aber jeder echte Dekompressions-Durchlauf bricht mit
`Invalid version ... found in LAZ chunk table` ab.

### Fachliche Absicherungen

- **Atomares Schreiben**: die Zielkachel entsteht als Temp-Datei im Zielordner und wird erst
  nach vollständiger Validierung per `os.replace` an ihren Platz gelegt. Bei jedem Fehler bleibt
  eine evtl. vorhandene Zieldatei unangetastet; die Quelle wird nie verändert.
- **Nachkonversions-Validierung** je Kachel: Punktanzahl identisch, BBox identisch innerhalb
  1 cm, Header-Zielwerte (siehe Tabelle), beide CRS-VLRs vorhanden (VLR 2112 endet auf
  Nullbyte), CRS auflösbar als 2056 + 5728 — und `5729`/`LHN95` kommen im Ziel-WKT **nicht** vor
  (fängt ab, dass versehentlich nicht-reframte LHN95-Kacheln als LN02 getaggt werden).
- **GPS-Time-Typ**: Das Zielformat verlangt `global_encoding` 17, also Bit 0 gesetzt
  (Adjusted Standard GPS Time). Ob das gegenueber der Quelle eine Aussage veraendert, wird an den
  DATEN gemessen statt pauschal gewarnt: die `filters.stats`-Stage misst `GpsTime` im ohnehin
  noetigen Lesedurchlauf gleich mit. Ist `GpsTime` durchgehend 0 (der Normalfall bei
  photogrammetrisch abgeleiteten DSM-Punktwolken), beschreibt der Typ nichts — kein Hinweis.
  Fuehrt die Quelle echte `GpsTime`-Werte, hatte aber Bit 0 nicht gesetzt, erscheint ein Hinweis
  mit dem gemessenen Wertebereich. Die **Werte** werden in keinem Fall angetastet, nur ihre
  Typ-Angabe im Header.
- **Classification-Kontrolle**: Min/Max der `Classification`-Dimension muss vor und nach der
  Konversion gleich sein. PF1/PF3 packen die Klasse als 5-Bit-Wert zusammen mit Flag-Bits in ein
  Byte, PF6 trennt beides — genau hier könnte die Punktformat-Umwandlung die Klasse still
  verändern. Die Spanne der Quelle wird per `filters.stats` am ohnehin nötigen Lesedurchlauf
  mitgemessen (kein zweiter Durchlauf).
- **Kachelrahmen-Prüfung**: Punkte ausserhalb des nominalen 1km-Rahmens sind ein harter Fehler
  (fehlplatzierte Datei oder falsch geparste Kachelkoordinaten). Lücken *zum* Rand werden
  bewusst nicht gemeldet — die Kacheln sind AOI-gecroppt, unvollständig gefüllte Randkacheln
  sind hier der Normalfall.
- **Raster-Zellen mit Puffer**: für die DSM-Zellen werden alle Kacheln gelesen, die den
  *gepufferten* Zellausschnitt berühren. Ohne den Puffer wäre das genau eine Kachel (der Input
  ist ja schon exakt 1km-gekachelt) und die IDW-Nachbarschaft am Zellrand bliebe einseitig —
  sichtbare Naht an jeder Kilometergrenze. Entsprechend werden pro DSM-Zelle bis zu neun
  1km-Kacheln angefasst: bei knappem RAM die **CPU-Kerne** reduzieren.

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
        │  2) Job-Pool ueber alle 1km-Zellen, parallel -> je 1 pdal.exe pro Job
        │     (Punktwolken-Kachel + optional DSM-Zelle), Retry seriell
        │  3) Zell-Raster mosaikieren (VRT) -> Cutline-Clip -> Hillshade
        │
    Aktion "process_las_ln02"  (Tab "DMC - LASconverter [LN02]"):
        │  1) Kachelursprung aus allen Dateinamen parsen (Abbruch vor dem
        │     ersten Schreibzugriff), Metadaten-Scan parallel
        │  2) Job-Pool: je Kachel Requantisierung auf LAS 1.4/PF6 + VLR-Byte-
        │     Injektion + Validierung (+ optional DSM-Zelle), Retry seriell
        │  3) Zell-Raster mosaikieren (VRT) -> Cutline-Clip -> Hillshade
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
- **Arbeitsspeicher:** `filters.merge`/`filters.sample` halten die Punkte im RAM. Deshalb wird
  bewusst zellweise gerechnet statt einmal ueber das ganze Projekt — ein Gesamt-Merge ueber
  >1000 Input-Kacheln laesst `pdal.exe` hart abstuerzen (Windows-Exitcode `3221226505` =
  `0xC0000409`, Fail-Fast ohne stdout/stderr). Massgeblich ist der Speicher pro Job, also die
  Punktzahl einer 1km-Zelle mal **CPU-Kerne**; bei knappem RAM die Kernzahl reduzieren.
- **Staging-Laufwerk:** Schreibzugriff auf den Staging-Ordner (Standard `Y:\02_DMC_tempProcessingFolder`).

---

## Koordinatensystem

Fest **EPSG:2056** (CH1903+ / LV95), massgebend fuer swisstopo-Daten. Kachel-TIFFs mit
`.tfw`-Begleitdatei tragen i.d.R. keine eingebettete CRS-Information. Bei den Punktwolken-Daten
(Tab 2) ist die Hoehe fest **LHN95** (EPSG:5729) — Input wie Output; ein Reframe nach LN02
findet nicht im Tool statt (siehe Tab-2-Abschnitt oben). Tab 3 setzt fest **LN02** (EPSG:5728)
als Höhenbezug — er taggt die extern reframten Kacheln, transformiert aber selbst nichts.

---

## Tests

```bash
python -m pytest -q
```

Leichtgewichtige Import-/Sanity-Checks (keine GDAL-Operationen, laufen auch ohne OSGeo4W).
