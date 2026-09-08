#!/usr/bin/env python3
"""
00b_subgriglia.py — Costruisce la sottogriglia fine dentro le celle attive.

L'IDEA:
  Il meteo ha una risoluzione nativa di circa 9 km e la nostra griglia da 0.1
  gradi ci coincide gia'. Infittirla non aggiungerebbe informazione meteo.
  Ma Corine e' a 100 metri e il DEM a 90: dentro una cella da 9 km convivono un
  versante nord a faggeta a 1200 metri e un fondovalle a 400, e oggi ricevono lo
  stesso identico punteggio.

  Quindi tagliamo ogni cella attiva in 16 sotto-celle da 0.025 gradi (circa
  2.8 x 2.1 km) e ricalcoliamo SOLO gli strati statici alla risoluzione fine.
  Il meteo resta quello del genitore, con la temperatura del suolo corretta per
  la quota reale della sotto-cella. Zero chiamate API in piu'.

PERCHE' SOLO DENTRO LE CELLE ATTIVE:
  Suddividere tutta Italia darebbe 54.637 sotto-celle, di cui la stragrande
  maggioranza senza bosco. Partendo dalle 2065 celle gia' selezionate ne
  generiamo 33.040, che il filtro forestale ridurra' ancora.

OUTPUT:
  data/grid/subcells.csv  con le stesse colonne di cells.csv piu' `parent_id`,
  cosi' gli script 01 e 02 possono girarci sopra senza modifiche concettuali.

ESECUZIONE:
    python scripts/00b_subgriglia.py
"""

import argparse
import json
import os
import urllib.request

import numpy as np
import pandas as pd
from shapely.geometry import box, shape
from shapely.ops import unary_union
from shapely import STRtree

BOUNDARY_URL = ("https://raw.githubusercontent.com/openpolis/geojson-italy/master/"
                "geojson/limits_IT_regions.geojson")


def carica_italia(path):
    if not os.path.exists(path):
        print(f"[download] confini regionali")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        urllib.request.urlretrieve(BOUNDARY_URL, path)
    gj = json.load(open(path))
    return unary_union([shape(f["geometry"]) for f in gj["features"]]).buffer(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attive", default="data/static/cells_attive_porcino.csv")
    ap.add_argument("--out", default="data/grid/subcells.csv")
    ap.add_argument("--passo-padre", type=float, default=0.1)
    ap.add_argument("--passo", type=float, default=0.025)
    ap.add_argument("--min-terra", type=float, default=0.05)
    args = ap.parse_args()

    padri = pd.read_csv(args.attive)
    n_lato = int(round(args.passo_padre / args.passo))
    print(f"[padri] {len(padri)} celle attive -> {n_lato}x{n_lato} = "
          f"{n_lato**2} sotto-celle ciascuna")

    italia = carica_italia("data/raw/limits_IT_regions.geojson")
    parti = list(italia.geoms) if italia.geom_type == "MultiPolygon" else [italia]
    tree = STRtree(parti)

    area = args.passo * args.passo
    righe = []
    for k, r in enumerate(padri.itertuples()):
        # Angolo sud-ovest del genitore, ricavato dal centro.
        y0 = round(r.lat - args.passo_padre / 2, 4)
        x0 = round(r.lon - args.passo_padre / 2, 4)
        for i in range(n_lato):
            for j in range(n_lato):
                lon_min = round(x0 + i * args.passo, 4)
                lat_min = round(y0 + j * args.passo, 4)
                c = box(lon_min, lat_min, lon_min + args.passo, lat_min + args.passo)
                idx = tree.query(c)
                if len(idx) == 0:
                    continue
                terra = 0.0
                for m in idx:
                    p = tree.geometries[m]
                    if p.intersects(c):
                        terra += p.intersection(c).area
                frac = terra / area
                if frac < args.min_terra:
                    continue
                lat_c = round(lat_min + args.passo / 2, 5)
                lon_c = round(lon_min + args.passo / 2, 5)
                righe.append({
                    "cell_id": f"{int(round(lat_c*1000)):06d}_{int(round(lon_c*1000)):06d}",
                    "parent_id": r.cell_id,
                    "lat": lat_c, "lon": lon_c,
                    "lat_min": lat_min, "lon_min": lon_min,
                    "land_frac": round(min(frac, 1.0), 4),
                })
        if (k + 1) % 400 == 0:
            print(f"  {k+1}/{len(padri)} genitori, {len(righe)} sotto-celle", flush=True)

    df = pd.DataFrame(righe).sort_values("cell_id").reset_index(drop=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    df.to_csv(args.out, index=False)

    print(f"\n[ok] {len(df)} sotto-celle -> {args.out}")
    print(f"     su {len(padri)*n_lato**2} teoriche "
          f"({100*len(df)/(len(padri)*n_lato**2):.0f}% supera il filtro terraferma)")
    print(f"     genitori rappresentati: {df.parent_id.nunique()}")
    print(f"     lato cella: circa {args.passo*111.1:.1f} x "
          f"{args.passo*111.1*np.cos(np.radians(df.lat.mean())):.1f} km")


if __name__ == "__main__":
    main()
