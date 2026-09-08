#!/usr/bin/env python3
"""
04b_verifica_storico.py — Controlla lo storico meteo prima di fidarsene.

PERCHE' ESISTE:
  Costruire il modello sopra dati che non abbiamo guardato e' il modo piu' veloce
  per passare settimane a cercare un errore nella formula quando il problema era
  una colonna vuota. Questo script non calcola niente: guarda e riferisce.

COSA CONTROLLA:
  1. completezza: ogni cella ha tutti i giorni? ci sono valori mancanti?
  2. plausibilita': i valori stanno negli intervalli fisici attesi?
  3. segnale: quante celle hanno avuto un evento di pioggia da trigger, e quante
     stanno nella finestra termica utile? Se qui uscisse zero, il modello
     produrrebbe una mappa tutta grigia e non sarebbe colpa sua.

ESECUZIONE:
    python scripts/04b_verifica_storico.py
"""

import numpy as np
import pandas as pd

STORICO = "data/meteo/storico.csv.gz"
SOGLIA_PIOGGIA = 25.0     # mm in 72 ore, evento trigger
T_MIN, T_MAX = 12.0, 18.0  # finestra termica utile, gradi


def riga(etichetta, valore):
    print(f"  {etichetta:<38s} {valore}")


def main():
    df = pd.read_csv(STORICO)
    df["giorno"] = pd.to_datetime(df["giorno"])
    df = df.sort_values(["cell_id", "giorno"])

    print("=" * 64)
    print("1. COMPLETEZZA")
    print("=" * 64)
    per_cella = df.groupby("cell_id").size()
    riga("celle", f"{df.cell_id.nunique()}")
    riga("giorni distinti", f"{df.giorno.nunique()}")
    riga("giorni per cella (min/mediana/max)",
         f"{per_cella.min()} / {int(per_cella.median())} / {per_cella.max()}")
    incomplete = (per_cella < per_cella.max()).sum()
    riga("celle con giorni mancanti", f"{incomplete}")

    print("\n  valori mancanti per colonna:")
    for c in df.columns:
        if c in ("cell_id", "giorno"):
            continue
        q = df[c].isna().mean()
        stato = "ok" if q < 0.001 else ("SOSPETTO" if q < 0.05 else "GRAVE")
        print(f"    {c:<12s} {q * 100:6.2f}%   {stato}")

    print("\n" + "=" * 64)
    print("2. PLAUSIBILITA' FISICA")
    print("=" * 64)
    attesi = {
        "prec_mm":  (0, 300, "mm/giorno"),
        "st6_med":  (-15, 45, "gradi, suolo a 6 cm"),
        "st18_med": (-10, 40, "gradi, suolo a 18 cm"),
        "sm9_med":  (0, 0.7, "m3/m3, umidita' a 3-9 cm"),
        "sm27_med": (0, 0.7, "m3/m3, umidita' a 9-27 cm"),
        "t2m_min":  (-30, 45, "gradi, aria"),
    }
    for c, (lo, hi, desc) in attesi.items():
        v = df[c].dropna()
        fuori = ((v < lo) | (v > hi)).sum()
        stato = "ok" if fuori == 0 else f"{fuori} FUORI INTERVALLO"
        print(f"    {c:<10s} {v.min():8.2f} .. {v.max():8.2f}   "
              f"mediana {v.median():7.2f}   {stato}")
        print(f"    {'':10s} atteso {lo} .. {hi} ({desc})")

    print("\n" + "=" * 64)
    print("3. C'E' SEGNALE?")
    print("=" * 64)
    # Pioggia cumulata su finestra mobile di 3 giorni, per cella.
    p = df.pivot_table(index="giorno", columns="cell_id", values="prec_mm")
    p72 = p.rolling(3, min_periods=3).sum()
    max72 = p72.max()
    con_trigger = (max72 >= SOGLIA_PIOGGIA).sum()
    riga("celle con almeno un evento >=25mm/72h", f"{con_trigger} su {p.shape[1]}")
    riga("massimo 72h: mediana fra le celle", f"{max72.median():.1f} mm")
    riga("massimo 72h: 90esimo percentile", f"{max72.quantile(0.90):.1f} mm")

    dentro = df["st6_med"].between(T_MIN, T_MAX)
    riga("giorni-cella con suolo 6cm in 12-18C", f"{dentro.mean() * 100:.1f}%")
    per_c = df.groupby("cell_id")["st6_med"].apply(
        lambda s: s.between(T_MIN, T_MAX).sum())
    riga("celle con >=5 giorni nella finestra", f"{(per_c >= 5).sum()}")

    print("\n  temperatura del suolo a 6 cm, per decile di quota:")
    st = pd.read_csv("data/static/cells_static.csv",
                     usecols=["cell_id", "quota_media"])
    m = df.merge(st, on="cell_id")
    m["fascia"] = pd.qcut(m["quota_media"], 5,
                          labels=["molto bassa", "bassa", "media", "alta", "molto alta"])
    for f, g in m.groupby("fascia", observed=True):
        pct = g["st6_med"].between(T_MIN, T_MAX).mean() * 100
        print(f"    {str(f):<12s} quota media {g.quota_media.mean():5.0f} m   "
              f"suolo medio {g.st6_med.mean():5.1f}C   "
              f"{pct:5.1f}% dei giorni in finestra")

    print("\n" + "=" * 64)
    ultimo = df.giorno.max().date()
    print(f"Ultimo giorno disponibile: {ultimo}")
    print("Se i controlli 1 e 2 sono puliti e il 3 mostra celle con trigger,")
    print("i dati reggono il modello.")


if __name__ == "__main__":
    main()
