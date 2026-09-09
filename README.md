# DMC Werkzeuge

Converter-Tool für rohe DMC-Daten aus RealityStudio (True-DOP und Punktwolken, technische
200m-Kacheln) ins swisstopo-Format „ch.spezialbefliegungen" (GDWH-STAC-ready). GUI mit vier Tabs;
Struktur und Styling analog zu `topo-COGTIFFconverter`.

## GUI starten

```bash
python GUI_DMCdataConverter.py
```

<img width="855" height="1386" alt="image" src="https://github.com/user-attachments/assets/1118d9c9-4098-4e85-a1af-1316d6fb9918" />



Beim ersten Start erkennt das GUI automatisch die OSGeo4W/QGIS-Installation. Der Pfad kann
ueber die Schaltflaeche **Aendern…** manuell gesetzt werden und wird in
`process_scripts/_dmc_config.json` gespeichert.

## Was macht welcher Tab?

| Tab | Input | Verarbeitung | Output |
|---|---|---|---|
| **DMC - TIFFconverter** | technische 200m-DOP-Kacheln (`.tif`), meist 4-Band RGBN | Mosaik → optionaler Bandauszug (RGB / NRG) → Clip auf die gültige Fläche → Zuschnitt ins 1km-Grid | 1km-DOP-Kacheln (`.tif`)<br>4-Band RGBN (Standard) oder 3-Band RGB / NRG |
| **DMC - LASconverter [LHN95]** | technische 200m-Punktwolken (`.laz`) | Kacheln je Gitterzelle mergen → Crop auf Zelle + AOI → optional ausdünnen | 1km-Kacheln `…_LV95_LHN95.las` (LAS 1.4 / PF7, mit RGB)<br>optional DSM + Hillshade |
| **DMC - LASconverter [LN02]** | die von GeoSuite nach LN02 reframten 1km-Kacheln | requantisieren → CRS-VLRs byte-exakt injizieren → vollständig validieren | 1km-Kacheln `…_LV95_LN02.laz` (GDWH-tauglich)<br>optional `.vpc` für QGIS, DSM + Hillshade |
| **Create DSM-Raster** | beliebiger Ordner mit `.las`/`.laz` | zellweise IDW-Rasterung → mosaikieren → Löcher füllen → AOI-Maske → Hillshade | ein DSM + ein Hillshade (`.tif` + `.tfw`) |

### Reihenfolge bei den Punktwolken

Die beiden LASconverter-Tabs gehören zusammen — dazwischen liegt ein Schritt **ausserhalb**
dieses Tools:

```
technische 200m-LAZ  (RealityStudio)
      │
      ▼   Tab [LHN95]        AOI-Crop · Thinning · 1km-Grid
…_LV95_LHN95.las             LAS 1.4 / PF7, mit RGB
      │
      ▼   GeoSuite / REFRAME     ←  ausserhalb dieses Tools, transformiert NUR die Höhe
…_LV95_LN02.las
      │
      ▼   Tab [LN02]         GDWH-Header · CRS-VLRs · Validierung
…_LV95_LN02.laz              →  Lieferung
```

Das DOP läuft unabhängig davon über den **TIFFconverter**. **Create DSM-Raster** ist ein Zusatz,
den man auf jeden fertigen Kachelordner ansetzen kann — unabhängig von den anderen Tabs.

### Was die Tabs bewusst NICHT tun

- **Kein Reframe LHN95→LN02.** Die Höhentransformation macht ausschliesslich GeoSuite/REFRAME;
  im Tool gibt es bewusst kein `filters.reprojection` zwischen den Höhenrahmen.
- **Kein Re-Tiling, kein Thinning, kein Crop im Tab [LN02].** Dort ist der Input bereits das
  fertige 1km-Grid; verändert werden nur Header und CRS-Tags.
- **Keine Höhenumrechnung beim Rastern.** Die Z-Werte werden nirgends angefasst.
- **Keine Klassifizierung, keine STAC-Metadaten.** Letztere macht `topo-importDATAtoGDWH-STAC`.

### Details weiter unten

Jeder Tab hat unten ein eigenes Kapitel mit allen Schritten, Zielwerten und den fachlichen
Absicherungen — zum Nachvollziehen, nicht zum Bedienen:

- [Details: TIFFconverter](#details-tiffconverter)
- [Details: LASconverter \[LHN95\]](#details-lasconverter-lhn95)
- [Details: LASconverter \[LN02\]](#details-lasconverter-ln02)
- [Details: Create DSM-Raster](#details-create-dsm-raster)
- [Architektur](#architektur) · [Voraussetzungen](#voraussetzungen) ·
  [Koordinatensystem](#koordinatensystem) · [Tests](#tests)



---

## Details: TIFFconverter

Verarbeitet technische 200m-DOP-Kacheln (`.tif`) via GDAL: aus dem Kachel-Mosaik wird die
gültige Fläche ausgeschnitten und das Ergebnis parallelisiert ins 1km-Grid zerlegt.

1. **Projekt-Parameter**: Jahr, AREA/AOI-Name, GSD (z.B. `10cm`) — ergeben zusammen mit dem
   Attribut `NAME` des Grid-Shapes die Ausgabebenennung:
   ```
   <JAHR>_<AREA>_DOP_<GSD>_<NAME>_LV95.tif  (+ .tfw)
   ```
   Beispiel: `2026_GUPPENFIRN_DOP_10cm_2713_1206_LV95.tif`

2. **Band-Ausgabe** (Auswahlfeld direkt unter der GSD-Eingabe): DMC-Orthophotos liegen
   praktisch immer als **4-Band RGBN** vor (Rot, Gruen, Blau, Nahes Infrarot). Hier wird
   gesteuert, welche Baender in die 1km-Kacheln geschrieben werden:

   | Auswahl | Quellbaender | Ergebnis |
   |---|---|---|
   | **4-Band  (RGBN, unveraendert)** — *Standard* | 1, 2, 3, 4 | alle Baender der Quelle bleiben erhalten |
   | **RGBN → RGB  (3-Band, Echtfarbe)** | 1, 2, 3 | Echtfarben-DOP ohne NIR |
   | **RGBN → NRG  (3-Band, Falschfarben-Infrarot)** | 4, 1, 2 | CIR-Komposit: NIR → Rot, Rot → Gruen, Gruen → Blau |

   - **Wird nichts gewaehlt, passiert nichts**: die Vorgabe ist „4-Band (RGBN, unveraendert)",
     die Ausgabe hat dann exakt so viele Baender wie der Input.
   - Die Auswahl ist **nur bei 4-Band-Input moeglich**. Beim Waehlen des Input-Ordners bzw.
     ueber **Datei-Info aktualisieren** liest das GUI die Bandzahl der ersten Kachel; bei
     weniger als 4 Baendern wird das Auswahlfeld gesperrt und auf „4-Band" zurueckgestellt.
     Startet man einen Lauf trotzdem mit einem 3-Band-Input (z.B. gemischter Ordner), bricht
     der Runner mit einer klaren Meldung ab, statt stillschweigend etwas Falsches zu schreiben.
   - Der Bandauszug passiert **vor** dem Cutline-Clip und wird als VRT gebaut — das kopiert
     keine Pixel. Warp und Grid-Zuschnitt arbeiten dadurch auf 3 statt 4 Baendern, also rund
     ein Viertel weniger I/O.
   - Die Ausgabe wird explizit mit `PHOTOMETRIC=RGB` und ColorInterp Rot/Gruen/Blau getaggt.
     Das ist vor allem bei NRG wichtig: Band 4 eines RGBN-TIFF ist haeufig als `Alpha` oder
     `Undefined` getaggt und wuerde sonst als Transparenzkanal in die Kachel wandern.
   - **Der Dateiname aendert sich dadurch nicht.** Wer RGB und NRG desselben Gebiets
     nebeneinander ablegen will, braucht getrennte Output-Ordner (oder einen abweichenden
     AREA-Namen).

3. **Input-Ordner**: Ordner mit den technischen 200m x 200m-Kacheln (`.tif` + `.tfw`).
   Enthaelt der Ordner bereits ein Mosaik-VRT (z.B. `True_Ortho.vrt`), wird dieses direkt
   uebernommen — sonst wird automatisch ein frisches VRT aus allen gefundenen `.tif`-Kacheln
   gebaut (`gdalbuildvrt`-Aequivalent).

4. **Clip-Shape (gueltige Flaeche)**: Polygon-Shape, das die manuell erfasste gueltige Flaeche
   des Orthophotos beschreibt. Alles ausserhalb wird per Cutline-Clip (`gdal.Warp`) zu
   NoData — die Quellkacheln tragen bereits NoData=0 je Band, der Clip verwendet denselben Wert.

5. **Grid-Shape (1km x 1km)**: Shapefile mit Attributfeld `NAME`, liefert Geometrie und
   Benennung der Ausgabekacheln. Standardmaessig vorausgefuellt mit dem mitgelieferten
   `swissGRID_1km2_shp/chGRID_1km2.shp`. Wird bei Bedarf automatisch nach EPSG:2056
   reprojiziert.

6. **Staging & Parallelisierung**: Zwischenergebnisse (VRT, Band-VRT, geclipptes Mosaik) werden in einem
   Staging-Ordner abgelegt (Standard `Y:\02_DMC_tempProcessingFolder`), damit mehrere Kerne
   parallel auf dieselbe geclippte Rasterquelle zugreifen koennen. **CPU-Kerne** steuert die
   Anzahl paralleler Prozesse fuer den Grid-Zuschnitt (Standard: 6). Nach erfolgreichem Lauf
   wird der projektspezifische Staging-Unterordner automatisch geloescht, sofern nicht
   **"Staging-Dateien behalten"** aktiviert ist.

7. **Ausgabe-Format** (kein GUI-Feld, automatisch): klassisches TIFF (kein COG) +
   `.tfw`-Weltdatei je Ausgabekachel, Blockgroesse fix 256, NoData fix 0 (alle Baender
   gleichermassen). Die Kompression wird von der ersten gefundenen Input-Kachel automatisch
   uebernommen (LZW/DEFLATE/ZSTD/unkomprimiert) — nie verlustbehaftet: liegt eine Input-Kachel
   ausnahmsweise JPEG-komprimiert vor, weicht die Ausgabe auf LZW aus, damit sie nie schlechter
   als der Input wird.

8. **DMC TIFF KONVERTIEREN** starten.

Vor dem Cutline-Clip prueft das Tool, ob der Pixelursprung des Mosaiks exakt auf ein Vielfaches
der Pixelgroesse faellt (sauberes Pixelraster, z.B. bei 10cm GSD auf `.0/.1/.2/…`-Koordinaten).
Nur wenn das zutrifft, treffen die 1km-Grid-Kachelgrenzen exakt auf bestehende Pixelkanten,
ohne dass GDAL rundet — bei einer Abweichung erscheint eine WARNUNG im Log. Cutline-Clip und
Grid-Zuschnitt behalten das Quell-Pixelraster explizit bei (kein implizites Resampling).

Kacheln, die nach dem Zuschnitt zu 100% aus einem konstanten Wert bestehen (reines NoData,
z.B. ausserhalb der gueltigen Flaeche oder ausserhalb des Befliegungsgebiets), werden
automatisch geloescht (`.tif` + `.tfw`).

---

## Details: LASconverter [LHN95]

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
   LHN95→LN02) via GeoSuite unkomprimiertes LAS erwartet. Geschrieben wird **LAS 1.4 / Point
   Data Record Format 7** (PF6 + RGB) mit CRS-Tag `EPSG:2056` — siehe „GeoSuite-Kompatibilitaet"
   unten.

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
- **Ausgabeformat der Zwischenausgabe**: die Tiles sind die Eingabe fuer den
  GeoSuite/REFRAME-Batch, deshalb wird das Format explizit gesetzt statt PDALs Defaults zu
  uebernehmen (PDAL entscheidet sonst anhand der vorhandenen Dimensionen und koennte je nach
  Quelle wechseln):

  | | hier gesetzt |
  |---|---|
  | `minor_version` | **4** (LAS 1.4) |
  | `dataformat_id` | **7** (PF6 + RGB, `point_length` 36) |
  | `global_encoding` | 17 bzw. 16 — Bit 4 (WKT) immer, Bit 0 (GPS-Time-Typ) aus der Quelle |
  | `scale` / `offset` | 0.01 / Kachelursprung (aus dem Dateinamen geparst) |
  | CRS im Header | `EPSG:2056` — nur horizontal, kein Vertikal-Key |

  **PF7 traegt die Farbe durch die ganze Kette.** Die DMC-Quelldaten aus Reality Studio sind PF2
  (RGB vorhanden, `GpsTime` nicht); PF7 ist PF6 + RGB und behaelt die Farbwerte, GeoSuite/REFRAME
  reicht sie durch, der Tab [LN02] uebernimmt sie unveraendert ins GDWH-Produkt. Fuehrt die
  Quelle gar keine Farbe, steht das als Warnung im Log — die Kacheln werden trotzdem PF7
  geschrieben, ihre RGB-Felder bleiben dann 0.

  Der Offset wird bewusst aus dem **Dateinamen** bestimmt (Kachelursprung), nicht aus dem
  Datenminimum: der Tab [LN02] macht es genauso, damit liegt die Zwischenstufe schon auf dem
  Ganzzahl-Gitter des Endprodukts und die dortige Requantisierung verschiebt keine Koordinaten.

  Der Hoehenbezug wird bewusst **nicht** getaggt: REFRAME bekommt Ein- und Ausgangsrahmen aus
  der Batch-Konfiguration, den autoritativen LV95/LN02-Tag setzt erst der Tab [LN02].

  Nach dem Schreiben wird der Header jeder Kachel geprueft (die Metadaten werden fuer den
  Punktzahl-Check ohnehin gelesen) — stimmt er nicht, wird die Kachel verworfen statt eine fuer
  REFRAME unbrauchbare Datei im Output-Ordner zu hinterlassen.

  > **Historie (nicht wieder einbauen):** Bis zum GeoSuite-Update vom 09.09.2026 las GeoSuite nur
  > klassisches LAS (1.0–1.2, PF0–PF3) und lehnte LAS 1.4/PF7 mit
  > `ERROR: File format incorrect ... unknown or unsupported format` ab — „Format" meint in LAS
  > genau das Point Data Record Format. Die Zwischenausgabe musste deshalb **LAS 1.2 / PF1** sein,
  > und die Farbe wurde ueber einen zweiten, farbfuehrenden „PF7-Master" plus Index-Join im Tab
  > [LN02] gerettet. Mit dem Update ist dieser Umweg hinfaellig; Master und Join wurden ersatzlos
  > entfernt.

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

## Details: LASconverter [LN02]

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

4. **Create Virtual Point Cloud (VPC)**: legt `_vpc\\<JAHR>_<AREA>_LV95_LN02.vpc` im
   Output-Ordner an — siehe „Virtual Point Cloud" unten. Unabhängig von der Raster-Option.

5. **Output-Ordner (DSM-Raster)** und **Footprint / AOI-Shape**: nur sichtbar bei aktivierter
   Raster-Option. DSM und Hillshade landen als je ein Gesamtbild (`.tif` + `.tfw`) im selben
   Ordner, per Cutline maskiert (DSM NoData `-3.4028235e+38`, Hillshade NoData `255`) — exakt
   wie im Tab [LHN95].

6. **Datei-Info**: zeigt zusätzlich **LAS-Version/Point-Format**, **Farbe (RGB)** und
   **global_encoding** der Quelle. Steht dort bereits `LAS 1.4 / PF7` und `17`, ist die Kachel
   schon im Zielformat und wird nur kopiert statt konvertiert. Steht bei Farbe `nein`, hat die
   Quelle kein RGB-Feld — dann kommt aus dem Lauf eine Warnung pro Kachel.

7. **Staging & Parallelisierung**: analog den anderen Tabs, eigener Unterordner
   (`<AREA>_<JAHR>_LN02`).

8. **DMC LAS KONVERTIEREN [LN02]** starten.

### Zielformat der Punktwolken-Kacheln

| Eigenschaft | Wert |
|---|---|
| LAS-Version | 1.4 |
| Point Data Record Format | **7** (`point_length` 36) — PF6 + RGB |
| `header_size` | 375 (unabhängig vom Punktformat) |
| `global_encoding` | 17 — Bit 0 (Adjusted Standard GPS Time) + Bit 4 (WKT) |
| `scale_x/y/z` | 0.01 |
| `offset_x/y/z` | Kachelursprung `<E>*1000 / <N>*1000 / 0` |
| CRS-Tag | LV95 + LN02 (EPSG:2056 + EPSG:5728), VLR 34735 + 2112 |

Der **Offset kommt aus dem Dateinamen**, nicht aus dem Datenminimum: eine AOI-gecroppte Kachel
fängt sonst irgendwo mitten in der Zelle an. Die geparsten Kilometerwerte werden gegen die
Schweizer Landesgrenzen (LV95) plausibilisiert; zeigen zwei Input-Kacheln auf dieselbe Zelle
(die Ausgaben würden sich überschreiben), bricht der Lauf vorher ab.

### Farbe

Die Farbe kommt **unverändert aus der Quelle**: der Tab [LHN95] schreibt PF7, GeoSuite/REFRAME
reicht die RGB-Werte durch, PDAL übernimmt sie beim Requantisieren nach PF7. Es wird nichts
zusammengefügt und nichts rekonstruiert.

Kontrolliert wird trotzdem, denn PF7 führt die RGB-Felder auch dann, wenn nur Nullen darin
stehen — eine farblos gewordene Lieferung fällt sonst erst im GDWH auf:

| Befund an der Quelle | Meldung |
|---|---|
| Punktformat ohne RGB-Feld (z.B. PF1) | Warnung pro Kachel — Hinweis auf einen Reframe mit einer GeoSuite-Version vor dem LAS-1.4-Update |
| RGB-Felder vorhanden, Werte durchgehend 0 | Warnung pro Kachel („schwarze Kachel") |

Gemessen wird im **ohnehin nötigen Lesedurchlauf** (`filters.stats` hängt sich als reiner
Durchlauf-Filter an) — kein zweiter Scan. `Red,Green,Blue` werden der Stage nur dann mitgegeben,
wenn das Punktformat der Quelle sie überhaupt führt: `filters.stats` bricht mit einem Fehler ab,
wenn eine angeforderte Dimension nicht existiert.

> **Höhe:** Z bleibt in jedem Fall verbatim das, was GeoSuite/REFRAME geliefert hat. Der Umweg
> über `filters.reprojection` von `EPSG:2056+5729` nach `+5728` wäre **keine** Alternative — PROJ
> kennt kein HTRANS, sondern nur den Umweg über zwei CHGeo2004-Gitter mit *unknown accuracy*, und
> fällt bei fehlenden Gittern still auf `+proj=noop` zurück: LHN95-Höhen mit LN02-Etikett.
> Deshalb gibt es in diesem Projekt bewusst kein `filters.reprojection`.

> **GpsTime:** PF7 verlangt das Feld, die DMC-Quelle (PF2) führt aber gar keine GPS-Zeit — es
> bleibt durchgehend 0. `global_encoding` 17 deklariert darüber trotzdem *Adjusted Standard GPS
> Time*, weil die Kachel strukturkongruent zu swissSURFACE3D bleiben soll. Der Typ beschreibt
> damit ein leeres Feld; das ist bewusst so und im Code an der Warnlogik dokumentiert.

> **Flussabwärts beachten:** `4_SB_DSM_PUNKTWOLKE_LAS14upgrade.py` im Projekt
> `topo-importDATAtoGDWH-STAC` validiert auf `dataformat_id == 6` und würde eine PF7-Kachel
> **still auf PF6 zurückdrehen** — die Farbe wäre dann doch wieder weg. Dort braucht es ein
> zweites Zielprofil, sonst endet die Kette wieder farblos.

### Virtual Point Cloud (VPC) — alle Kacheln als eine Ebene in QGIS

Bei aktivierter Option entsteht `_vpc\\<JAHR>_<AREA>_LV95_LN02.vpc`: eine JSON-Datei
(STAC-FeatureCollection), die alle fertigen Kacheln zu **einer** Punktwolken-Ebene zusammenfasst
— das Punktwolken-Gegenstück zum Raster-VRT. Es wird **nichts kopiert und nichts umgerechnet**;
die Datei verweist mit **relativen Pfaden** (`../<kachel>.laz`) auf die Kacheln daneben, der
Ordner lässt sich also verschieben oder kopieren, ohne dass sie bricht.

Gelesen wird sie von **QGIS ab 3.32**. **ArcGIS Pro liest kein VPC** — dafür bräuchte es ein
LAS-Dataset (`.lasd`), das ausschliesslich `arcpy` erzeugen kann; im OSGeo4W-Python des Runners
gibt es kein arcpy, deshalb ist dieser Weg bewusst nicht umgesetzt.

Die Kachel-Fakten (Punktanzahl, 3D-BBox) werden **aus den Headern der fertigen Ausgabedateien**
gelesen — parallel und ohne die Punktdaten anzufassen, bei `.laz` also ohne Dekompression. Damit
beschreibt die VPC nachweislich das, was im Ordner liegt, statt das, was der Lauf zu schreiben
glaubte; unverändert kopierte Kacheln sind ebenso erfasst wie konvertierte.

Das Format ist an einer mit `pdal_wrench build_vpc` erzeugten Referenz nachgemessen und gegen den
QGIS-Provider (3.44) gegengelesen. Zwei Dinge sind dabei nicht verhandelbar:

| Feld | warum |
|---|---|
| `proj:wkt2` | Fehlt es, lehnt QGIS die Datei ab (`isValid() == False`). |
| `geometry` / `bbox` in **WGS84** | STAC-Konvention. Mit LV95-Werten darin lädt QGIS die Ebene **ohne Fehlermeldung**, liefert aber einen unendlichen Extent — man sieht nichts und nichts weist darauf hin. Die Landeskoordinaten gehören nach `proj:bbox`. |

`pc:schemas`, `stac_extensions` und `proj:geometry` sind dagegen optional (geprüft) und bleiben
weg — die Dimensionsliste stünde sonst für jede Kachel identisch in der Datei.

Zwei Details am Rand: `proj:wkt2` trägt bewusst **nur** das horizontale `EPSG:2056` — die VPC ist
eine Kartenebene, der Höhenbezug (LN02) steckt in den CRS-VLRs der Kacheln selbst. Und
`pc:type` ist `eopc` (*electro-optical point cloud*), nicht `lidar`: die DMC-Wolken sind
photogrammetrisch abgeleitet.

Die VPC ist ein **Ansichtsprodukt neben der Lieferung**. Schlägt ihr Schreiben fehl, gibt es eine
Warnung im Log, aber der Lauf bleibt erfolgreich — die Kacheln sind das Produkt.

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

**Es müssen ALLE mitgeschleppten CRS-VLRs weichen, nicht nur die mit `user_id`
`LASF_Projection`.** PDALs `writers.las` schreibt denselben WKT nämlich **zweimal** — an einer
erzeugten Kachel nachgemessen:

| `user_id` | `record_id` | Beschreibung |
|---|---|---|
| `LASF_Projection` | 2112 | *OGC Transformation Record* |
| `liblas` | 2112 | *OGR variant of OpenGIS WKT SRS* |

Beide enthalten `PROJCS["CH1903+ / LV95"...]` — **rein horizontal, ohne LN02**. Würde nur die
erste Variante entfernt, trüge jede ausgelieferte Kachel zwei widersprechende Aussagen zum
Raumbezug, und der `liblas`-Zwilling stünde in der Datei sogar **vor** der autoritativen Angabe:
ein Leser, der schlicht den ersten VLR mit `record_id` 2112 nimmt, bekäme LV95 ohne Höhenbezug.
Entfernt wird deshalb jeder VLR, der einen Raumbezug deklariert (`LASF_Projection` komplett,
dazu `liblas` mit `record_id` 2111/2112/34735–34737). Der `laszip encoded`-VLR (22204) ist
ausdrücklich ausgenommen — ohne ihn lässt sich eine `.laz` nicht mehr dekomprimieren.

Damit eine künftige Schreiber-Variante nicht wieder still durchrutscht, ist das zusätzlich in
der Validierung verriegelt: findet sich im Ziel neben den zwei Referenz-VLRs noch ein
CRS-führender VLR, ist die Kachel ein **harter Fehler**, keine Warnung.

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
  Nullbyte), **kein weiterer CRS-VLR daneben**, CRS auflösbar als 2056 + 5728 — und
  `5729`/`LHN95` kommen im Ziel-WKT **nicht** vor (fängt ab, dass versehentlich nicht-reframte
  LHN95-Kacheln als LN02 getaggt werden).
- **„Schon fertig“-Abkürzung nur mit Beweis**: eine Kachel wird unverändert kopiert statt
  konvertiert, wenn sie bereits das Zielprodukt ist. Geprüft wird dafür nicht nur
  Version/Punktformat/`global_encoding`/CRS, sondern auch `scale` **und** die byte-exakten
  Referenz-VLRs. Grund: seit auch die Zwischenstufe LAS 1.4/PF7 ist, unterscheidet sich eine
  GeoSuite-Ausgabe vom fertigen Produkt nur noch an `scale`/`offset` und den CRS-Tags — ohne
  diese beiden Kontrollen könnte eine reframte Kachel mit GeoSuites CRS-Tags durchgereicht
  werden statt mit den autoritativen.
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
  Byte, PF6/PF7 trennen beides — genau hier könnte die Punktformat-Umwandlung die Klasse still
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

## Details: Create DSM-Raster

Der eigenständige Raster-Build: derselbe DSM-/Hillshade-Weg wie die Raster-Option der beiden
Konverter-Tabs, nur ohne deren Punktwolken-Verarbeitung. Gedacht für den Fall, dass die Kacheln
schon fertig sind (oder von woanders kommen) und nur noch ein Raster gebraucht wird.

1. **Projekt-Parameter**: Jahr, AREA/AOI-Name, **Höhenbezug** (LHN95 oder LN02) und
   **Raster-Auflösung (GSD)**, Default `0.5` m. Daraus die Benennung:
   ```
   <JAHR>_<AREA>_DSM_<GSD>cm_LV95_<LHN95|LN02>.tif        (+ .tfw)
   <JAHR>_<AREA>_hillshade_<GSD>cm_LV95_<LHN95|LN02>.tif  (+ .tfw)
   ```
   Der Höhenbezug steuert **nur die Benennung und den SRS-Tag der Reader** — die Z-Werte werden
   nirgends umgerechnet. Beim Wählen des Input-Ordners werden Jahr, AREA und Höhenbezug aus dem
   ersten Kachelnamen vorbelegt, sofern er der Konvention der Konverter-Tabs folgt; bei
   Fremddaten bleibt alles wie eingetippt.

2. **Input-Ordner (LAS/LAZ)**: alle `.las`/`.laz` im Ordner. Die Kachelung ist **beliebig** — die
   Dateinamen müssen keiner Konvention folgen (anders als im Tab [LN02], wo der Kachelursprung
   aus dem Namen kommt).

3. **Output-Ordner (Raster)**: DSM und Hillshade als je ein `.tif` + `.tfw`.

4. **Footprint / AOI-Shape**: Maskierung wie in den anderen Tabs — ausserhalb NoData, Extent
   bleibt. Kein Crop der Punkte.

5. **Staging & Parallelisierung**: eigener Unterordner (`<AREA>_<JAHR>_DSM`).

### Zellschnitt — warum, und warum anders als in den Konverter-Tabs

Gerastert wird zellweise und danach mosaikiert, aus demselben Grund wie überall im Projekt: ein
Gesamt-Merge über alle Kacheln hält die komplette Punktwolke im Speicher und bringt `pdal.exe`
bei grossen Projekten zum Absturz.

Die Konverter-Tabs holen ihre Zellen aus dem Grid-Shape bzw. aus den Dateinamen — dort sind die
Zellen ja zugleich die Ausgabegeometrie der Punktwolken-Kacheln. **Hier sind sie reine
Arbeitspakete**, deshalb wird das 1km-Raster direkt aus dem Gesamt-Extent abgeleitet: kein
Grid-Shape nötig, und die Kachelnamen dürfen beliebig sein. Das Ergebnis ist identisch, weil
ohnehin mosaikiert wird.

Zwei Eigenschaften, auf die es dabei ankommt (beide in `_dsm_cell_jobs`, mit Tests):

- **Zellen werden auf den Datenbereich beschnitten.** Ohne das rastert eine Kachel, die nur in
  einer Ecke ihrer Kilometerzelle liegt, trotzdem den ganzen Quadratkilometer — und das
  anschliessende Füllen der NoData-Löcher läuft über eine riesige leere Fläche. An einem
  Testdatensatz gemessen: **7,7 Mio. leere Pixel und über zwei Minuten statt 0 Pixel und
  ~1 Sekunde**.
- **Jede Zelle bekommt die Kacheln, die ihren *gepufferten* Ausschnitt berühren.** Der Puffer
  hält die IDW-Nachbarschaft am Zellrand vollständig; ohne ihn bliebe sie einseitig und an jeder
  Zellgrenze entstünde eine sichtbare Naht. Eine Kachel, die exakt auf einer Zellkante beginnt,
  gehört deshalb zu **beiden** Nachbarzellen. Zellen ohne beitragende Kachel entfallen ganz.

Mosaik, Löcherfüllung (klein interpoliert, gross bleibt NoData), AOI-Maskierung und Hillshade
laufen anschliessend über dieselbe Funktion wie in den anderen Tabs — inklusive der dortigen
Kontrollen (Pixelraster-Check, NoData-Kontrolle, Deckungsgleichheit DSM/Hillshade).

## Architektur

```
GUI_DMCdataConverter.py            (Standard-Python, tkinter)
        │
        │  JSON-Config (tempfile)
        ▼
process_scripts/_osgeo_runner.py   (OSGeo4W Python, GDAL/OGR)
    Aktion "process"      (Tab "DMC - TIFFconverter"):
        │  1) Mosaik-VRT (uebernommen oder frisch gebaut)
        │  1b) optionaler Bandauszug RGBN -> RGB / NRG als VRT (band_mode)
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
        │  2) Job-Pool: je Kachel Requantisierung auf LAS 1.4/PF7 + VLR-Byte-
        │     Injektion + Validierung (+ optional DSM-Zelle), Retry seriell
        │  2b) optional .vpc aus den Headern der fertigen Kacheln (QGIS)
        │  3) Zell-Raster mosaikieren (VRT) -> Cutline-Clip -> Hillshade
        │
        │
    Aktion "process_dsm"  (Tab "Create DSM-Raster"):
        │  1) Metadaten-Scan aller Kacheln, parallel -> Gesamt-Extent
        │  2) 1km-Arbeitszellen aus dem Extent, auf die Daten beschnitten
        │  3) Zellen parallel rastern (IDW), Retry seriell
        │  4) mosaikieren (VRT) -> Loecher fuellen -> Cutline-Clip -> Hillshade
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
