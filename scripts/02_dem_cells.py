#!/usr/bin/env python3
"""
02_dem_cells.py — Quota, pendenza ed esposizione per cella, da Copernicus DEM GLO-90.

PERCHE' GLO-90 E NON ALTRO:
  E' su un bucket AWS pubblico: nessun account, nessuna API key, download diretto
  via HTTPS. Risoluzione 90 m: piu' che sufficiente, visto che poi aggreghiamo
  tutto su celle da ~9 km.

  Nota onesta: GLO-90 e' un DSM (modello della SUPERFICIE), quindi in bosco fitto
  la quota include le chiome, +15/25 m. Sulla media di cella e' irrilevante; sulla
  pendenza introduce un po' di rumore, che mitighiamo lisciando prima di derivare.

COSA CALCOLA PER OGNI CELLA:
  quota media, mediana, minima, massima e dislivello interno;
  frazione di cella in ciascuna fascia altimetrica (serve per inferire il tipo
    di bosco nello script 03: castagno in basso, faggio in mezzo, abete in alto);
  pendenza media in gradi;
  frazione di cella esposta a ciascuno degli 8 settori (N, NE, E, SE, S, SO, O, NO);
  "northness" = media del coseno dell'esposizione, da -1 (tutto a sud) a +1
    (tutto a nord). E' la variabile che useremo davvero nel modello: i versanti
    nord trattengono umidita' e sfasano la fruttificazione rispetto ai sud.

  L'esposizione viene calcolata SOLO sui pixel con pendenza >= 3 gradi: su un
  pianoro l'esposizione e' un numero casuale e sporcherebbe la media.

ESECUZIONE:
    python scripts/02_dem_cells.py            # scarica i tile mancanti e processa
    python scripts/02_dem_cells.py --keep-tiles   # non cancella i .tif dopo l'uso

Spazio disco: ~500 MB temporanei se non usi --keep-tiles (i tile vengono
cancellati mano a mano). Tempo: 20-40 minuti sulla prima esecuzione.
"""

import argparse
import os
import time

import numpy as np
import pandas as pd
import rasterio
import requests

STEP = 0.1
BASE = "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com"
TILE_DIR = "data/raw/dem"

# Fasce altimetriche (m slm). I limiti superiori; l'ultima e' aperta.
ELEV_BANDS = [300, 600, 900, 1200, 1600, 2000, 9000]
BAND_NAMES = ["q_000_300", "q_300_600", "q_600_900", "q_900_1200",
              "q_1200_1600", "q_1600_2000", "q_2000_su"]

SECTORS = ["n", "ne", "e", "se", "s", "so", "o", "no"]
MIN_SLOPE_FOR_ASPECT = 3.0   # gradi


def tile_name(lat, lon):
    """Nome del tile Copernicus per il grado intero (lat, lon) dell'angolo SO."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_30_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def download_tile(lat, lon, out_dir, retries=3):
    """Scarica un tile. Ritorna il path, oppure None se il tile non esiste (mare).

    Usa `requests`, che porta con se' il proprio pacchetto di certificati:
    evita l'errore CERTIFICATE_VERIFY_FAILED tipico di Python installato da
    python.org su macOS.

    Scarica prima su un file .part e rinomina solo a download completato, cosi'
    un'interruzione non lascia in giro un .tif troncato che al riavvio verrebbe
    scambiato per valido.
    """
    name = tile_name(lat, lon)
    path = os.path.join(out_dir, name + ".tif")
    if os.path.exists(path):
        return path
    tmp = path + ".part"
    url = f"{BASE}/{name}/{name}.tif"

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 404:
                    # Normale: i tile interamente oceanici non esistono.
                    return None
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            os.replace(tmp, path)
            return path
        except requests.RequestException as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"    rete instabile ({e.__class__.__name__}), riprovo fra {wait}s")
            time.sleep(wait)


def slope_aspect(z, lat_centre):
    """Pendenza (gradi) ed esposizione (gradi da nord, orario) da un DEM lat/lon."""
    # Dimensione del pixel in metri. 3 arcsec = 1/1200 grado.
    dy = (1.0 / 1200.0) * 111320.0
    dx = dy * np.cos(np.radians(lat_centre))

    # Lisciatura 3x3: attenua il rumore delle chiome del DSM.
    zs = z.astype(np.float32)
    k = np.ones((3, 3), np.float32) / 9.0
    pad = np.pad(zs, 1, mode="edge")
    sm = np.zeros_like(zs)
    for i in range(3):
        for j in range(3):
            sm += k[i, j] * pad[i:i + zs.shape[0], j:j + zs.shape[1]]

    g_row, g_col = np.gradient(sm)
    dz_dx = g_col / dx            # positivo verso est
    dz_dy = -g_row / dy           # positivo verso nord (le righe crescono a sud)

    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    # Direzione di massima pendenza in discesa, in gradi bussola.
    aspect = (np.degrees(np.arctan2(-dz_dx, -dz_dy)) + 360.0) % 360.0
    return slope, aspect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="data/grid/cells.csv")
    ap.add_argument("--out", default="data/static/cells_dem.csv")
    ap.add_argument("--keep-tiles", action="store_true")
    args = ap.parse_args()

    os.makedirs(TILE_DIR, exist_ok=True)
    grid = pd.read_csv(args.grid)

    grid["irow"] = np.round(grid.lat_min / STEP).astype(int)
    grid["icol"] = np.round(grid.lon_min / STEP).astype(int)
    row_min, col_min = int(grid.irow.min()), int(grid.icol.min())
    n_row = int(grid.irow.max()) - row_min + 1
    n_col = int(grid.icol.max()) - col_min + 1
    lookup = np.full(n_row * n_col, -1, dtype=np.int32)
    lookup[(grid.irow.values - row_min) * n_col + (grid.icol.values - col_min)] = np.arange(len(grid))

    n = len(grid)
    acc_n = np.zeros(n, np.int64)
    acc_z = np.zeros(n, np.float64)
    acc_z2 = np.zeros(n, np.float64)
    acc_zmin = np.full(n, np.inf)
    acc_zmax = np.full(n, -np.inf)
    acc_slope = np.zeros(n, np.float64)
    acc_bands = np.zeros((n, len(ELEV_BANDS)), np.int64)
    acc_sect = np.zeros((n, 8), np.int64)
    acc_north = np.zeros(n, np.float64)
    acc_east = np.zeros(n, np.float64)
    acc_nsl = np.zeros(n, np.int64)      # pixel con pendenza sufficiente

    # Solo i tile di 1 grado che contengono almeno una nostra cella.
    tiles = sorted({(int(np.floor(r.lat_min)), int(np.floor(r.lon_min)))
                    for r in grid.itertuples()})
    print(f"[dem] {len(tiles)} tile da 1 grado da processare")

    for k, (tlat, tlon) in enumerate(tiles, 1):
        path = download_tile(tlat, tlon, TILE_DIR)
        if path is None:
            print(f"  [{k}/{len(tiles)}] N{tlat} E{tlon}: tile assente (mare)")
            continue
        with rasterio.open(path) as src:
            z = src.read(1).astype(np.float32)
            nodata = src.nodata
            valid = np.isfinite(z)
            if nodata is not None:
                valid &= (z != nodata)
            # In Copernicus DEM il mare ha quota 0 o nodata; teniamo 0 come terra
            # bassa, tanto il filtro bosco successivo eliminera' il mare.
            slope, aspect = slope_aspect(z, tlat + 0.5)

            rows, cols = np.nonzero(valid)
            if rows.size == 0:
                continue
            xs, ys = rasterio.transform.xy(src.transform, rows, cols)
            lon = np.asarray(xs); lat = np.asarray(ys)

            ir = np.floor(lat / STEP).astype(int) - row_min
            ic = np.floor(lon / STEP).astype(int) - col_min
            inside = (ir >= 0) & (ir < n_row) & (ic >= 0) & (ic < n_col)
            if not inside.any():
                continue
            pos = lookup[ir[inside] * n_col + ic[inside]]
            ok = pos >= 0
            pos = pos[ok]
            if pos.size == 0:
                continue

            zv = z[rows, cols][inside][ok].astype(np.float64)
            sv = slope[rows, cols][inside][ok].astype(np.float64)
            av = aspect[rows, cols][inside][ok].astype(np.float64)

            np.add.at(acc_n, pos, 1)
            np.add.at(acc_z, pos, zv)
            np.add.at(acc_z2, pos, zv * zv)
            np.minimum.at(acc_zmin, pos, zv)
            np.maximum.at(acc_zmax, pos, zv)
            np.add.at(acc_slope, pos, sv)

            band = np.searchsorted(ELEV_BANDS, zv, side="left")
            band = np.clip(band, 0, len(ELEV_BANDS) - 1)
            np.add.at(acc_bands, (pos, band), 1)

            steep = sv >= MIN_SLOPE_FOR_ASPECT
            if steep.any():
                p2 = pos[steep]; a2 = np.radians(av[steep])
                np.add.at(acc_nsl, p2, 1)
                np.add.at(acc_north, p2, np.cos(a2))
                np.add.at(acc_east, p2, np.sin(a2))
                sect = (np.floor(((av[steep] + 22.5) % 360.0) / 45.0)).astype(int)
                np.add.at(acc_sect, (p2, sect), 1)

        if not args.keep_tiles:
            os.remove(path)
        print(f"  [{k}/{len(tiles)}] N{tlat} E{tlon} ok", flush=True)

    cnt = np.maximum(acc_n, 1)
    out = grid[["cell_id", "lat", "lon"]].copy()
    out["dem_px"] = acc_n
    out["quota_media"] = np.round(acc_z / cnt, 1)
    out["quota_min"] = np.where(np.isfinite(acc_zmin), np.round(acc_zmin, 1), np.nan)
    out["quota_max"] = np.where(np.isfinite(acc_zmax), np.round(acc_zmax, 1), np.nan)
    out["quota_sd"] = np.round(np.sqrt(np.maximum(acc_z2 / cnt - (acc_z / cnt) ** 2, 0)), 1)
    out["pendenza_media"] = np.round(acc_slope / cnt, 2)
    for i, name in enumerate(BAND_NAMES):
        out[name] = np.round(acc_bands[:, i] / cnt, 4)
    cnt_sl = np.maximum(acc_nsl, 1)
    for i, s in enumerate(SECTORS):
        out[f"esp_{s}"] = np.round(acc_sect[:, i] / cnt_sl, 4)
    out["northness"] = np.round(acc_north / cnt_sl, 4)
    out["eastness"] = np.round(acc_east / cnt_sl, 4)
    out["frac_pendente"] = np.round(acc_nsl / cnt, 4)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out.to_csv(args.out, index=False)
    print(f"\n[ok] {len(out)} celle -> {args.out}")
    print(f"     celle senza dati DEM: {(out.dem_px == 0).sum()}")
    print(f"     quota media nazionale: {out.quota_media[out.dem_px > 0].mean():.0f} m")


if __name__ == "__main__":
    main()
