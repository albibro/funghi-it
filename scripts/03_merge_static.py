#!/usr/bin/env python3
"""
03_merge_static.py — Unisce i tre livelli statici e sceglie le celle attive.

COSA FA:
  1. Unisce griglia + Corine + DEM su cell_id.
  2. Assegna a ogni cella una macroregione (alpino / prealpino / appenninico /
     mediterraneo) da latitudine e quota media.
  3. Calcola due punteggi indipendenti:
       qualita_bosco  quanto la copertura forestale della cella e' un ospite
                      micorrizico plausibile, pesando le classi Corine
       idoneita_quota quanto la distribuzione altimetrica della cella cade
                      nella fascia buona per la specie, in quella macroregione
     e li moltiplica in idoneita_ospite.
  4. Applica le soglie e scrive l'elenco delle celle da interrogare ogni giorno.

PERCHE' MOLTIPLICARE INVECE DI SOMMARE:
  Sono due condizioni necessarie, non due indizi che si compensano. Un pascolo
  d'alta quota alla quota perfetta ma senza bosco non produce porcini, e una
  faggeta a 100 m sul livello del mare in Puglia nemmeno. Il prodotto porta
  a zero se manca uno dei due; una somma darebbe punteggi medi a entrambi.

ATTENZIONE, LIMITE DICHIARATO:
  Corine non dice quale specie arborea c'e'. Faggio, castagno e quercia stanno
  tutti nella classe 311. La distinzione la stiamo INFERENDO dalla quota e dalla
  macroregione: e' un'ipotesi, non un'osservazione. Se il backtesting su GBIF
  mostrera' che sbagliamo sistematicamente in certe aree, il rimedio e'
  aggiungere il dataset EU-Forest, che ha la presenza delle singole specie
  arboree su griglia da 1 km.

OUTPUT:
  data/static/cells_static.csv       tutte le 3668 celle con tutti gli attributi
  data/static/cells_attive_porcino.csv   solo le celle che superano le soglie
  data/static/cells_attive_porcino.json  le stesse, nel formato che serve al
                                         downloader meteo (cell_id, lat, lon)

ESECUZIONE:
    python scripts/03_merge_static.py --specie config/porcino.yml
"""

import argparse
import json
import os

import numpy as np
import pandas as pd
import yaml

BAND_COLS = ["q_000_300", "q_300_600", "q_600_900", "q_900_1200",
             "q_1200_1600", "q_1600_2000", "q_2000_su"]


def assegna_macroregione(df, cfg):
    """Nord/sud per latitudine, montagna/collina per quota media."""
    lat_nord = cfg["macroregione"]["lat_nord"]
    q_mont = cfg["macroregione"]["quota_montana"]
    nord = df["lat"] >= lat_nord
    montano = df["quota_media"] >= q_mont
    return np.select(
        [nord & montano, nord & ~montano, ~nord & montano],
        ["alpino", "prealpino", "appenninico"],
        default="mediterraneo",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--specie", default="config/porcino.yml")
    ap.add_argument("--grid", default="data/grid/cells.csv")
    ap.add_argument("--corine", default="data/static/cells_corine.csv")
    ap.add_argument("--dem", default="data/static/cells_dem.csv")
    ap.add_argument("--outdir", default="data/static")
    args = ap.parse_args()

    with open(args.specie) as f:
        cfg = yaml.safe_load(f)
    nome = cfg["nome"]
    print(f"[specie] {nome} ({cfg['nome_scientifico']})")

    grid = pd.read_csv(args.grid)
    corine = pd.read_csv(args.corine)
    dem = pd.read_csv(args.dem)

    # Uniamo su cell_id. Le colonne lat/lon sono ripetute nei tre file: teniamo
    # solo quelle della griglia, che e' la fonte autorevole.
    df = grid.merge(corine.drop(columns=["lat", "lon", "land_frac"], errors="ignore"),
                    on="cell_id", how="left")
    df = df.merge(dem.drop(columns=["lat", "lon"], errors="ignore"),
                  on="cell_id", how="left")

    mancanti = df[BAND_COLS + ["bosco_frac"]].isna().any(axis=1).sum()
    if mancanti:
        print(f"[attenzione] {mancanti} celle senza dati completi, messe a zero")
        df[BAND_COLS + ["bosco_frac"]] = df[BAND_COLS + ["bosco_frac"]].fillna(0.0)

    # --- Qualita' del bosco ---------------------------------------------------
    # Media dei pesi Corine pesata sulla composizione. Il denominatore e' la
    # frazione boscata totale, quindi il risultato dice "com'e' fatto il bosco
    # che c'e'", indipendentemente da quanto ce n'e'.
    num = np.zeros(len(df))
    for col, w in cfg["peso_clc"].items():
        num += df[col].values * w
    den = np.maximum(df["bosco_frac"].values, 1e-9)
    df["qualita_bosco"] = np.round(np.clip(num / den, 0, 1), 4)
    df.loc[df["bosco_frac"] < 1e-6, "qualita_bosco"] = 0.0

    # --- Macroregione e idoneita' altimetrica --------------------------------
    df["macroregione"] = assegna_macroregione(df, cfg)

    idon_q = np.zeros(len(df))
    for regione, pesi in cfg["peso_quota"].items():
        if len(pesi) != len(BAND_COLS):
            raise ValueError(f"peso_quota[{regione}] deve avere {len(BAND_COLS)} valori")
        m = (df["macroregione"] == regione).values
        if not m.any():
            continue
        idon_q[m] = (df.loc[m, BAND_COLS].values * np.array(pesi)).sum(axis=1)
    df["idoneita_quota"] = np.round(np.clip(idon_q, 0, 1), 4)

    # --- Punteggio combinato --------------------------------------------------
    df["idoneita_ospite"] = np.round(df["qualita_bosco"] * df["idoneita_quota"], 4)

    # --- Selezione ------------------------------------------------------------
    s = cfg["soglie"]
    attive = (df["bosco_frac"] >= s["bosco_frac_min"]) & \
             (df["idoneita_ospite"] >= s["idoneita_ospite_min"])
    df["attiva"] = attive

    os.makedirs(args.outdir, exist_ok=True)
    df.to_csv(os.path.join(args.outdir, "cells_static.csv"), index=False)

    sel = df[attive].copy()
    sel_path = os.path.join(args.outdir, f"cells_attive_{nome}.csv")
    sel.to_csv(sel_path, index=False)

    json_path = os.path.join(args.outdir, f"cells_attive_{nome}.json")
    with open(json_path, "w") as f:
        json.dump(
            {
                "specie": nome,
                "n_celle": int(len(sel)),
                "celle": sel[["cell_id", "lat", "lon"]].to_dict(orient="records"),
            },
            f,
        )

    # --- Riepilogo ------------------------------------------------------------
    print(f"\n[ok] {len(df)} celle totali -> cells_static.csv")
    print(f"[ok] {len(sel)} celle ATTIVE -> {os.path.basename(sel_path)}")
    print("\n     ripartizione per macroregione:")
    for reg, n in sel["macroregione"].value_counts().items():
        q = sel.loc[sel.macroregione == reg, "quota_media"].mean()
        print(f"       {reg:14s} {n:5d} celle   quota media {q:6.0f} m")

    print("\n     costo giornaliero Open-Meteo con scarico incrementale:")
    print(f"       {len(sel)} chiamate su 10000 disponibili "
          f"({100 * len(sel) / 10000:.0f}% della quota)")
    print("     costo del caricamento iniziale (45 giorni di storico, peso 3.21):")
    costo = len(sel) * 45 / 14
    print(f"       {costo:.0f} chiamate -> "
          f"{'entra in una giornata' if costo < 9000 else 'DA SPEZZARE su piu giorni'}")


if __name__ == "__main__":
    main()
