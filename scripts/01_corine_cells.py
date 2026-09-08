#!/usr/bin/env python3
"""
01_corine_cells.py — Aggrega Corine Land Cover (100 m) sulle celle 0.1 gradi.

COSA FA:
  Legge il raster CLC2018 (proiezione EPSG:3035, pixel da 100 m, quindi 1 ettaro
  ciascuno), lo scorre a blocchi di righe, converte il centro di ogni pixel in
  latitudine/longitudine, capisce in quale cella della nostra griglia cade, e
  conta quanti pixel di ciascuna classe di interesse ci sono dentro.

  Il risultato e' la COMPOSIZIONE del bosco in ogni cella, in frazione 0..1.

PERCHE' NON UN CICLO CELLA PER CELLA:
  3668 letture windowed con riproiezione sarebbero lentissime. Scorrere il raster
  una volta sola e usare np.bincount e' O(pixel) e ci mette qualche minuto.

CLASSI CHE CI INTERESSANO (codici CLC a 3 cifre):
  311 latifoglie      -> faggio, castagno, querce = ospiti micorrizici del porcino
  312 conifere        -> abete rosso/bianco, pini = ospiti micorrizici del porcino
  313 bosco misto
  321 praterie naturali    \
  322 brughiere/cespuglieti | non sono bosco, ma servono per capire quanto una
  323 macchia sclerofilla   | cella e' "aperta" e per i margini
  324 aree in transizione  /

  ATTENZIONE: Corine dice "latifoglie" ma non dice QUALE specie. La distinzione
  faggeta / castagneto / querceto la ricostruiremo nello script 03 incrociando
  classe CLC + fascia altimetrica + regione biogeografica (Alpi / Appennino /
  Mediterraneo). E' un'approssimazione dichiarata, non un dato osservato.

INPUT (da scaricare a mano una volta sola, vedi README):
  data/raw/clc/U2018_CLC2018_V2020_20u1.tif

OUTPUT:
  data/static/cells_corine.csv

ESECUZIONE:
    python scripts/01_corine_cells.py --clc data/raw/clc/U2018_CLC2018_V2020_20u1.tif
"""

import argparse
import os

import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import transform as warp_transform

STEP = 0.1   # valore predefinito, sovrascritto da --passo

# CLC distribuisce il raster 100 m con valori "GRID_CODE" 1..44 (48 = NODATA),
# non con i codici a 3 cifre. Questa e' la corrispondenza ufficiale.
GRID_CODE_TO_CLC = {
    1: 111, 2: 112, 3: 121, 4: 122, 5: 123, 6: 124, 7: 131, 8: 132, 9: 133,
    10: 141, 11: 142, 12: 211, 13: 212, 14: 213, 15: 221, 16: 222, 17: 223,
    18: 231, 19: 241, 20: 242, 21: 243, 22: 244, 23: 311, 24: 312, 25: 313,
    26: 321, 27: 322, 28: 323, 29: 324, 30: 331, 31: 332, 32: 333, 33: 334,
    34: 335, 35: 411, 36: 412, 37: 421, 38: 422, 39: 423, 40: 511, 41: 512,
    42: 521, 43: 522, 44: 523,
}

# Classi che teniamo, nell'ordine in cui finiranno nelle colonne di output.
KEEP = [311, 312, 313, 321, 322, 323, 324]
COLNAMES = {
    311: "clc_311_latifoglie",
    312: "clc_312_conifere",
    313: "clc_313_misto",
    321: "clc_321_praterie",
    322: "clc_322_brughiere",
    323: "clc_323_sclerofille",
    324: "clc_324_transizione",
}


def detect_value_scheme(src, sample_rows=200):
    """Capisce se il raster usa i grid code 1..44 o i codici CLC 111..523."""
    win = rasterio.windows.Window(0, src.height // 2, src.width, min(sample_rows, src.height))
    a = src.read(1, window=win)
    vmax = int(a.max()) if a.size else 0
    scheme = "gridcode" if vmax <= 48 else "clc3"
    print(f"[clc] valore massimo campionato = {vmax} -> schema '{scheme}'")
    return scheme


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clc", required=True, help="GeoTIFF CLC2018 100 m")
    ap.add_argument("--grid", default="data/grid/cells.csv")
    ap.add_argument("--passo", type=float, default=0.1,
                    help="lato cella in gradi; 0.025 per la sottogriglia")
    ap.add_argument("--out", default="data/static/cells_corine.csv")
    ap.add_argument("--block", type=int, default=512, help="righe per blocco")
    args = ap.parse_args()

    global STEP
    STEP = args.passo
    print(f"[griglia] passo {STEP} gradi")

    grid = pd.read_csv(args.grid)
    # Indice rapido cella -> posizione nella tabella.
    # Usiamo indici interi derivati da lat/lon per evitare hash di stringhe.
    grid["irow"] = np.round(grid.lat_min / STEP).astype(int)
    grid["icol"] = np.round(grid.lon_min / STEP).astype(int)
    row_min, col_min = int(grid.irow.min()), int(grid.icol.min())
    n_row = int(grid.irow.max()) - row_min + 1
    n_col = int(grid.icol.max()) - col_min + 1

    # Mappa densa (riga, colonna) -> indice di riga nel CSV, -1 se cella non esiste.
    lookup = np.full(n_row * n_col, -1, dtype=np.int32)
    lookup[(grid.irow.values - row_min) * n_col + (grid.icol.values - col_min)] = np.arange(len(grid))

    n_cells = len(grid)
    n_class = len(KEEP)
    class_index = {c: k for k, c in enumerate(KEEP)}
    counts = np.zeros((n_cells, n_class), dtype=np.int64)
    total_valid = np.zeros(n_cells, dtype=np.int64)   # pixel CLC validi nella cella

    with rasterio.open(args.clc) as src:
        print(f"[clc] {src.width} x {src.height} px, CRS {src.crs}")
        scheme = detect_value_scheme(src)

        # Tabella di traduzione valore-raster -> indice di classe (o -1 = scarta)
        lut = np.full(1024, -1, dtype=np.int16)
        lut_valid = np.zeros(1024, dtype=bool)
        if scheme == "gridcode":
            for gc, clc in GRID_CODE_TO_CLC.items():
                lut_valid[gc] = True
                if clc in class_index:
                    lut[gc] = class_index[clc]
        else:
            for clc in GRID_CODE_TO_CLC.values():
                lut_valid[clc] = True
                if clc in class_index:
                    lut[clc] = class_index[clc]

        for r0 in range(0, src.height, args.block):
            h = min(args.block, src.height - r0)
            win = rasterio.windows.Window(0, r0, src.width, h)
            arr = src.read(1, window=win)

            # Coordinate del centro di ogni pixel del blocco, in EPSG:3035.
            cols = np.arange(src.width)
            rows = np.arange(r0, r0 + h)
            xs, ys = rasterio.transform.xy(src.transform, 
                                           np.repeat(rows, src.width),
                                           np.tile(cols, h))
            xs = np.asarray(xs); ys = np.asarray(ys)

            flat = arr.ravel()
            # Teniamo solo i pixel di classi che ci interessano: riduce di ~90%
            # il numero di riproiezioni da fare.
            keep_mask = (flat < 1024) & lut_valid[np.clip(flat, 0, 1023)]
            if not keep_mask.any():
                continue

            lon, lat = warp_transform(src.crs, "EPSG:4326",
                                      xs[keep_mask], ys[keep_mask])
            lon = np.asarray(lon); lat = np.asarray(lat)

            ir = np.floor(lat / STEP).astype(int) - row_min
            ic = np.floor(lon / STEP).astype(int) - col_min
            inside = (ir >= 0) & (ir < n_row) & (ic >= 0) & (ic < n_col)
            if not inside.any():
                continue

            cell_pos = lookup[ir[inside] * n_col + ic[inside]]
            ok = cell_pos >= 0
            if not ok.any():
                continue
            cell_pos = cell_pos[ok]

            np.add.at(total_valid, cell_pos, 1)

            cls = lut[flat[keep_mask][inside][ok]]
            has_class = cls >= 0
            if has_class.any():
                lin = cell_pos[has_class] * n_class + cls[has_class]
                counts += np.bincount(lin, minlength=n_cells * n_class).reshape(n_cells, n_class)

            if (r0 // args.block) % 10 == 0:
                print(f"  riga {r0}/{src.height}", flush=True)

    # Frazioni sulla superficie CLC valida della cella (esclude mare/nodata).
    denom = np.maximum(total_valid, 1)[:, None]
    frac = counts / denom

    out = grid[["cell_id", "lat", "lon", "land_frac"]].copy()
    for c in KEEP:
        out[COLNAMES[c]] = np.round(frac[:, class_index[c]], 5)
    out["clc_px_validi"] = total_valid
    # Un ettaro per pixel: comodo per capire quanta foresta c'e' davvero.
    out["bosco_ha"] = counts[:, [class_index[c] for c in (311, 312, 313)]].sum(axis=1)
    out["bosco_frac"] = np.round(
        out[["clc_311_latifoglie", "clc_312_conifere", "clc_313_misto"]].sum(axis=1), 5
    )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out.to_csv(args.out, index=False)

    print(f"\n[ok] {len(out)} celle -> {args.out}")
    for thr in (0.05, 0.10, 0.20, 0.30):
        print(f"     celle con bosco_frac >= {thr:.2f}: {(out.bosco_frac >= thr).sum()}")


if __name__ == "__main__":
    main()
