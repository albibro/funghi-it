#!/usr/bin/env python3
"""
03b_merge_subcells.py — Strati statici e selezione alla risoluzione fine.

Fa per le sotto-celle da 0.025 gradi quello che 03_merge_static.py fa per le
celle da 0.1: unisce Corine e DEM, inferisce l'idoneita' dell'ospite e applica
le soglie. In piu' porta con se' due informazioni che serviranno al modello:

  parent_id     quale cella da 9 km fornisce il meteo
  delta_quota   di quanto la sotto-cella sta sopra o sotto la quota media del
                genitore. E' il numero con cui correggeremo la temperatura del
                suolo: dentro una cella da 9 km il dislivello puo' superare i
                mille metri, e la temperatura e' il fattore piu' selettivo di
                tutto il modello.

UNA DIFFERENZA VOLUTA RISPETTO A 03:
  La macroregione viene ricalcolata sulla quota della SOTTO-cella, non ereditata
  dal genitore. Una sotto-cella a 1400 metri dentro un genitore classificato
  "mediterraneo" e' una faggeta d'altura, e va valutata con il profilo giusto.

ESECUZIONE:
    python scripts/03b_merge_subcells.py --specie config/porcino.yml
"""

import argparse
import os

import numpy as np
import pandas as pd
import yaml

BAND_COLS = ["q_000_300", "q_300_600", "q_600_900", "q_900_1200",
             "q_1200_1600", "q_1600_2000", "q_2000_su"]


def assegna_macroregione(df, cfg):
    lat_nord = cfg["macroregione"]["lat_nord"]
    q_mont = cfg["macroregione"]["quota_montana"]
    nord = df["lat"] >= lat_nord
    montano = df["quota_media"] >= q_mont
    return np.select(
        [nord & montano, nord & ~montano, ~nord & montano],
        ["alpino", "prealpino", "appenninico"], default="mediterraneo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--specie", default="config/porcino.yml")
    ap.add_argument("--grid", default="data/grid/subcells.csv")
    ap.add_argument("--corine", default="data/static/subcells_corine.csv")
    ap.add_argument("--dem", default="data/static/subcells_dem.csv")
    ap.add_argument("--padri", default="data/static/cells_static.csv")
    ap.add_argument("--outdir", default="data/static")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.specie))
    nome = cfg["nome"]
    print(f"[specie] {nome}")

    df = pd.read_csv(args.grid)
    for f, etichetta in [(args.corine, "corine"), (args.dem, "dem")]:
        t = pd.read_csv(f)
        df = df.merge(t.drop(columns=["lat", "lon", "land_frac"], errors="ignore"),
                      on="cell_id", how="left")
        print(f"[unione] {etichetta}: {len(t)} righe")

    manc = df[BAND_COLS + ["bosco_frac"]].isna().any(axis=1).sum()
    if manc:
        print(f"[attenzione] {manc} sotto-celle incomplete, messe a zero")
        df[BAND_COLS + ["bosco_frac"]] = df[BAND_COLS + ["bosco_frac"]].fillna(0.0)

    # --- Quota del genitore e dislivello --------------------------------------
    padri = pd.read_csv(args.padri, usecols=["cell_id", "quota_media"])
    padri = padri.rename(columns={"cell_id": "parent_id", "quota_media": "quota_padre"})
    df = df.merge(padri, on="parent_id", how="left")
    df["delta_quota"] = (df["quota_media"] - df["quota_padre"]).round(1)

    # --- Qualita' del bosco ---------------------------------------------------
    num = np.zeros(len(df))
    for col, w in cfg["peso_clc"].items():
        num += df[col].values * w
    den = np.maximum(df["bosco_frac"].values, 1e-9)
    df["qualita_bosco"] = np.round(np.clip(num / den, 0, 1), 4)
    df.loc[df["bosco_frac"] < 1e-6, "qualita_bosco"] = 0.0

    # --- Macroregione e idoneita' altimetrica --------------------------------
    df["macroregione"] = assegna_macroregione(df, cfg)
    idon = np.zeros(len(df))
    for regione, pesi in cfg["peso_quota"].items():
        m = (df["macroregione"] == regione).values
        if m.any():
            idon[m] = (df.loc[m, BAND_COLS].values * np.array(pesi)).sum(axis=1)
    df["idoneita_quota"] = np.round(np.clip(idon, 0, 1), 4)
    df["idoneita_ospite"] = np.round(df["qualita_bosco"] * df["idoneita_quota"], 4)

    s = cfg["soglie"]
    df["attiva"] = (df["bosco_frac"] >= s["bosco_frac_min"]) & \
                   (df["idoneita_ospite"] >= s["idoneita_ospite_min"])

    os.makedirs(args.outdir, exist_ok=True)
    tutte = os.path.join(args.outdir, "subcells_static.csv")
    df.to_csv(tutte, index=False)
    sel = df[df["attiva"]].copy()
    # Il file completo ha una quarantina di colonne e pesa parecchio: resta fuori
    # da git. Quello che serve al modello ne ha dieci, e va committato perche' il
    # job giornaliero deve trovarlo.
    colonne = ["cell_id", "parent_id", "lat_min", "lon_min", "quota_media",
               "delta_quota", "bosco_frac", "northness", "idoneita_ospite",
               "macroregione"]
    snello = os.path.join(args.outdir, f"subcells_attive_{nome}.csv")
    sel[colonne].to_csv(snello, index=False)

    print(f"\n[ok] file per il modello: {snello} "
          f"({os.path.getsize(snello)/1e6:.1f} MB)")
    print(f"[ok] {len(df)} sotto-celle totali")
    print(f"[ok] {len(sel)} sotto-celle ATTIVE "
          f"({100*len(sel)/len(df):.0f}%), su {sel.parent_id.nunique()} genitori")

    print("\n     ripartizione per macroregione:")
    for reg, n in sel["macroregione"].value_counts().items():
        q = sel.loc[sel.macroregione == reg, "quota_media"].mean()
        print(f"       {reg:14s} {n:6d}   quota media {q:6.0f} m")

    d = sel["delta_quota"]
    print(f"\n     dislivello rispetto al genitore, dove sta il guadagno:")
    print(f"       mediana |delta|      {d.abs().median():6.0f} m")
    print(f"       90esimo percentile   {d.abs().quantile(.90):6.0f} m")
    print(f"       massimo              {d.abs().max():6.0f} m")
    g6 = cfg["modello"]["downscaling"]["gradiente_6cm"]
    print(f"       corrisponde a una correzione termica mediana di "
          f"{d.abs().median()/100*g6:.1f} gradi")
    print(f"       e nel 10% dei casi oltre {d.abs().quantile(.90)/100*g6:.1f} gradi")


if __name__ == "__main__":
    main()
