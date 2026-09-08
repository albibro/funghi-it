#!/usr/bin/env python3
"""
00_build_grid.py — Costruisce la griglia 0.1 gradi sull'Italia.

COSA FA:
  1. Scarica i confini amministrativi delle regioni italiane (GeoJSON pubblico su GitHub).
  2. Li fonde in un unico poligono "Italia".
  3. Disegna sopra una griglia regolare di celle 0.1 x 0.1 gradi, allineata ai
     multipli esatti di 0.1 (cosi' coincide con la griglia nativa di ERA5-Land,
     che useremo per il backtesting storico).
  4. Per ogni cella calcola quanta parte e' terraferma (land_frac, 0..1).
  5. Scarta le celle quasi interamente in mare.

PERCHE' 0.1 GRADI:
  Una cella 0.1° alla latitudine dell'Italia misura circa 11.1 km (nord-sud)
  x 7.6-9.1 km (est-ovest), quindi ~85-100 km². E' la stessa risoluzione nativa
  di ERA5-Land: niente interpolazione, e il backtest storico e' "gratis".

OUTPUT:
  data/grid/cells.csv  con colonne:
    cell_id     stringa stabile, es. "4425_01075" = lat 44.25 N, lon 10.75 E
    lat, lon    coordinate del CENTRO cella (quelle che manderemo a Open-Meteo)
    lat_min, lon_min  angolo sud-ovest (per disegnare i rettangoli sulla mappa)
    land_frac   frazione di superficie su terraferma

ESECUZIONE (una tantum, poi il file va committato nel repo):
    python scripts/00_build_grid.py
"""

import json
import os
import sys
import urllib.request

import numpy as np
import pandas as pd
from shapely.geometry import box, shape
from shapely.ops import unary_union
from shapely import STRtree

# --- Parametri ---------------------------------------------------------------

STEP = 0.1                 # lato della cella in gradi
MIN_LAND_FRAC = 0.05       # scarta celle con meno del 5% di terra
BOUNDARY_URL = (
    "https://raw.githubusercontent.com/openpolis/geojson-italy/master/"
    "geojson/limits_IT_regions.geojson"
)
RAW_DIR = "data/raw"
OUT_PATH = "data/grid/cells.csv"


def download_boundary(path):
    """Scarica il GeoJSON dei confini regionali se non e' gia' in cache locale."""
    if os.path.exists(path):
        print(f"[cache] {path}")
        return
    print(f"[download] {BOUNDARY_URL}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    urllib.request.urlretrieve(BOUNDARY_URL, path)


def build_grid(italy):
    """Genera le celle che toccano la terraferma, con la loro frazione di terra."""
    minx, miny, maxx, maxy = italy.bounds
    # Allineiamo l'origine ai multipli esatti di STEP.
    lon0 = np.floor(minx / STEP) * STEP
    lat0 = np.floor(miny / STEP) * STEP
    nlon = int(np.ceil((maxx - lon0) / STEP))
    nlat = int(np.ceil((maxy - lat0) / STEP))
    print(f"[grid] bounding box {nlon} x {nlat} = {nlon * nlat} celle da testare")

    # Indice spaziale: senza questo il test di intersezione sarebbe lentissimo.
    parts = list(italy.geoms) if italy.geom_type == "MultiPolygon" else [italy]
    tree = STRtree(parts)

    cell_area = STEP * STEP
    rows = []
    for i in range(nlon):
        lon_min = round(lon0 + i * STEP, 4)
        for j in range(nlat):
            lat_min = round(lat0 + j * STEP, 4)
            cell = box(lon_min, lat_min, lon_min + STEP, lat_min + STEP)

            idx = tree.query(cell)          # candidati (bounding box overlap)
            if len(idx) == 0:
                continue

            land = 0.0
            for k in idx:
                p = tree.geometries[k]
                if p.intersects(cell):
                    land += p.intersection(cell).area
            frac = land / cell_area
            if frac < MIN_LAND_FRAC:
                continue

            lat_c = round(lat_min + STEP / 2, 4)
            lon_c = round(lon_min + STEP / 2, 4)
            # cell_id: latitudine e longitudine del centro x100, con segno implicito
            # (l'Italia e' tutta N ed E, quindi non serve gestire i negativi).
            cell_id = f"{int(round(lat_c * 100)):05d}_{int(round(lon_c * 100)):05d}"
            rows.append(
                {
                    "cell_id": cell_id,
                    "lat": lat_c,
                    "lon": lon_c,
                    "lat_min": lat_min,
                    "lon_min": lon_min,
                    "land_frac": round(min(frac, 1.0), 4),
                }
            )
    return pd.DataFrame(rows)


def main():
    boundary_path = os.path.join(RAW_DIR, "limits_IT_regions.geojson")
    download_boundary(boundary_path)

    with open(boundary_path) as f:
        gj = json.load(f)
    print(f"[boundary] {len(gj['features'])} regioni")

    # buffer(0) ripara eventuali auto-intersezioni nei poligoni sorgente.
    italy = unary_union([shape(f["geometry"]) for f in gj["features"]]).buffer(0)

    df = build_grid(italy)
    df = df.sort_values("cell_id").reset_index(drop=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    df.to_csv(OUT_PATH, index=False)

    print(f"\n[ok] {len(df)} celle scritte in {OUT_PATH}")
    print(f"     land_frac >= 0.50 : {(df.land_frac >= 0.50).sum()}")
    print(f"     land_frac >= 0.25 : {(df.land_frac >= 0.25).sum()}")
    print(f"     estensione lat {df.lat.min():.2f}..{df.lat.max():.2f}  "
          f"lon {df.lon.min():.2f}..{df.lon.max():.2f}")


if __name__ == "__main__":
    sys.exit(main())
