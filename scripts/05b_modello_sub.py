#!/usr/bin/env python3
"""
05b_modello_sub.py — L'indice alla risoluzione fine, con meteo del genitore.

COME FUNZIONA IL DOWNSCALING:

  Il meteo lo abbiamo per la cella da 9 km. Per ogni sotto-cella da 2.8 km
  riusiamo quello stesso meteo, con una sola correzione: la temperatura del
  suolo viene traslata in base al dislivello fra la quota della sotto-cella e
  la quota media del genitore, con un gradiente di circa mezzo grado ogni
  cento metri.

  Non e' un dettaglio marginale. La finestra termica e' il fattore che oggi
  azzera il punteggio ovunque sotto i 900 metri. Dentro una cella da 9 km il
  dislivello puo' superare i mille metri, quindi la stessa cella puo' contenere
  un versante alto pienamente in finestra e un fondovalle fuori di sei gradi.
  Finora ricevevano lo stesso punteggio.

  Pioggia e umidita' NON vengono corrette. Non abbiamo un modo difendibile per
  ridistribuirle dentro la cella, e inventarne uno darebbe precisione finta.
  Va detto nella pagina delle informazioni della mappa.

EFFICIENZA:
  Il calcolo del trigger dipende SOLO dal genitore, perche' pioggia e umidita'
  antecedente sono le sue. Lo facciamo una volta per genitore (2065 volte) e lo
  riusiamo per le sue 16 sotto-celle, invece di ripeterlo 32.000 volte.

FORMATO DI USCITA:
  Non piu' GeoJSON. Con 25.000 celle la sola impalcatura ripetuta di GeoJSON
  peserebbe piu' dei dati. Usiamo un formato compatto ad array, con i poligoni
  ricostruiti dal browser: stesso contenuto, un quarto del peso.

ESECUZIONE:
    python scripts/05b_modello_sub.py --specie config/porcino.yml
"""

import argparse
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd
import yaml


def rampa(x, basso, alto, minimo=0.0):
    y = np.clip((x - basso) / (alto - basso), 0.0, 1.0)
    return minimo + (1.0 - minimo) * y


def trapezio(x, zb, pb, pa, za):
    x = np.asarray(x, dtype=float)
    return np.minimum(np.clip((x - zb) / (pb - zb), 0, 1),
                      np.clip((za - x) / (za - pa), 0, 1))


def campana(g, picco, sigma, gmin, gmax):
    g = np.asarray(g, dtype=float)
    return np.where((g >= gmin) & (g <= gmax),
                    np.exp(-0.5 * ((g - picco) / sigma) ** 2), 0.0)


def prepara_genitore(serie, p, indici_bersaglio):
    """Tutto cio' che dipende solo dal genitore: trigger, medie meteo, gelo.

    Ritorna un dizionario per ciascun giorno bersaglio."""
    prec = serie["prec_mm"].values
    st6 = serie["st6_med"].values
    st18 = serie["st18_med"].values
    sm9 = serie["sm9_med"].values
    sm27 = serie["sm27_med"].values
    tmin = serie["t2m_min"].values
    idx = serie.index

    fin = p["trigger"]["finestra_ore"] // 24
    p72 = pd.Series(prec).rolling(fin, min_periods=fin).sum().values
    lat = p["latenza"]
    ua = p["umidita_antecedente"]
    te = p["termica"]
    uc = p["umidita_corrente"]
    ge = p["gelo"]

    out = []
    for i in indici_bersaglio:
        # --- trigger -----------------------------------------------------
        best, info = 0.0, None
        for L in range(lat["giorni_min"], lat["giorni_max"] + 1):
            j = i - L
            if j < 0 or np.isnan(p72[j]) or p72[j] < p["trigger"]["soglia_mm"]:
                continue
            w_lat = float(campana(L, lat["giorni_picco"], lat["sigma"],
                                  lat["giorni_min"], lat["giorni_max"]))
            w_mm = float(rampa(p72[j], p["trigger"]["soglia_mm"],
                               p["trigger"]["mm_saturazione"], p["trigger"]["peso_minimo"]))
            k0 = max(0, j - ua["giorni"])
            ante = np.nanmean(sm27[k0:j]) if j > k0 else np.nan
            w_ante = 1.0 if np.isnan(ante) else float(
                rampa(ante, ua["secco"], ua["saturo"], ua["peso_minimo"]))
            s = w_lat * w_mm * w_ante
            if s > best:
                best, info = s, {"data": str(idx[j].date()),
                                 "mm72": round(float(p72[j]), 1),
                                 "giorni_fa": int(L)}

        # --- medie meteo del genitore -------------------------------------
        k0 = max(0, i - te["giorni_finestra"] + 1)
        m6 = float(np.nanmean(st6[k0:i + 1]))
        m18 = float(np.nanmean(st18[k0:i + 1]))
        k0 = max(0, i - uc["giorni"] + 1)
        msm = float(np.nanmean(sm9[k0:i + 1]))
        f_umid = float(rampa(msm, uc["secco"], uc["saturo"], uc["peso_minimo"]))
        k0 = max(0, i - ge["giorni"] + 1)
        gelato = bool(np.nanmin(tmin[k0:i + 1]) < ge["soglia_c"])

        out.append({"trigger": best, "info": info, "m6": m6, "m18": m18,
                    "umid": f_umid, "sm9": msm,
                    "gelo": ge["penalita"] if gelato else 1.0, "gelato": gelato})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--specie", default="config/porcino.yml")
    ap.add_argument("--storico", default="data/meteo/storico.csv.gz")
    ap.add_argument("--sub", default="data/static/subcells_attive_porcino.csv")
    ap.add_argument("--outdir", default="data/output")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.specie))
    p = cfg["modello"]
    nome = cfg["nome"]
    te, es, dw = p["termica"], p["esposizione"], p["downscaling"]

    df = pd.read_csv(args.storico)
    df["giorno"] = pd.to_datetime(df["giorno"])
    sub = pd.read_csv(args.sub)
    print(f"[dati] {len(sub)} sotto-celle, {sub.parent_id.nunique()} genitori")

    ultimo, primo = df["giorno"].max(), df["giorno"].min()
    oggi = pd.Timestamp.today().normalize()
    if not (primo <= oggi <= ultimo):
        print(f"[nota] oggi fuori dallo storico: uso {ultimo.date()}")
        oggi = ultimo
    bersagli = [oggi + timedelta(days=k) for k in range(0, p["giorni_previsti"] + 1)]
    bersagli = [b for b in bersagli if b <= ultimo]
    print(f"[calcolo] {len(bersagli)} giorni: {bersagli[0].date()} .. {bersagli[-1].date()}")

    # --- Fase 1: una volta per genitore ---------------------------------------
    per_padre = {}
    for pid, g in df.groupby("cell_id"):
        s = g.set_index("giorno").sort_index()
        pos = {d: i for i, d in enumerate(s.index)}
        idxs = [pos[b] for b in bersagli if b in pos]
        if len(idxs) != len(bersagli):
            continue
        per_padre[pid] = prepara_genitore(s, p, idxs)
    print(f"[fase 1] {len(per_padre)} genitori preparati")

    # --- Fase 2: per sotto-cella ----------------------------------------------
    sub = sub[sub.parent_id.isin(per_padre)].reset_index(drop=True)
    celle, indici, fattori = [], [], []
    padri_id, padri_dett = [], []
    pos_padre = {}

    for r in sub.itertuples():
        gg = per_padre[r.parent_id]
        if r.parent_id not in pos_padre:
            pos_padre[r.parent_id] = len(padri_id)
            padri_id.append(r.parent_id)
            padri_dett.append({"pioggia": gg[0]["info"],
                               "suolo_6cm": round(gg[0]["m6"], 1),
                               "umidita_9cm": round(gg[0]["sm9"], 3),
                               "gelata": gg[0]["gelato"]})

        # Correzione termica: solo se il dislivello e' plausibile. Oltre il
        # limite il gradiente lineare non regge piu' e preferiamo non correggere.
        d = float(r.delta_quota) if pd.notna(r.delta_quota) else 0.0
        d = 0.0 if abs(d) > dw["delta_max"] else d
        c6 = -d / 100.0 * dw["gradiente_6cm"]
        c18 = -d / 100.0 * dw["gradiente_18cm"]

        vals, f0 = [], None
        for k, gd in enumerate(gg):
            m6 = gd["m6"] + c6
            m18 = gd["m18"] + c18
            f6 = float(trapezio(m6, te["t_zero_basso"], te["t_pieno_basso"],
                                te["t_pieno_alto"], te["t_zero_alto"]))
            f18 = float(trapezio(m18, te["t_zero_basso"], te["t_pieno_basso"],
                                 te["t_pieno_alto"], te["t_zero_alto"]))
            f_term = te["peso_6cm"] * f6 + te["peso_18cm"] * f18

            segno = np.tanh((m6 - es["t_neutra"]) / 3.0)
            north = float(r.northness) if pd.notna(r.northness) else 0.0
            f_esp = float(np.clip(1 + es["ampiezza"] * north * segno, .7, 1.3))

            f_osp = float(r.idoneita_ospite)
            v = 100.0 * gd["trigger"] * f_term * gd["umid"] * f_osp * f_esp * gd["gelo"]
            vals.append(int(round(np.clip(v, 0, 100))))
            if k == 0:
                f0 = [round(gd["trigger"], 2), round(f_term, 2), round(gd["umid"], 2),
                      round(f_osp, 2), round(f_esp, 2), round(gd["gelo"], 2)]

        celle.append([int(round(r.lat_min * 1000)), int(round(r.lon_min * 1000)),
                      int(round(r.quota_media)), int(round(r.bosco_frac * 100)),
                      pos_padre[r.parent_id], int(round(d))])
        indici.append(vals)
        fattori.append([int(round(x * 100)) for x in f0])

    # --- Uscita compatta -------------------------------------------------------
    os.makedirs(args.outdir, exist_ok=True)
    out = {
        "meta": {"specie": nome, "nome_scientifico": cfg["nome_scientifico"],
                 "generato": pd.Timestamp.now("UTC").isoformat(),
                 "giorni": [str(b.date()) for b in bersagli],
                 "passo": 0.025, "n": len(celle)},
        "campi_cella": ["lat_min_x1000", "lon_min_x1000", "quota_m",
                        "bosco_pct", "indice_padre", "delta_quota_m"],
        "campi_fattori": ["trigger", "termica", "umidita", "ospite", "esposizione", "gelo"],
        "padri": padri_id,
        "dettaglio_padri": padri_dett,
        "celle": celle, "i": indici, "f": fattori,
    }
    path = os.path.join(args.outdir, f"indice_{nome}.json")
    json.dump(out, open(path, "w"), separators=(",", ":"))

    v = np.array([x[0] for x in indici])
    mb = os.path.getsize(path) / 1e6
    print(f"\n[ok] {len(celle)} sotto-celle -> {path} ({mb:.2f} MB, "
          f"circa {os.path.getsize(path)/len(celle):.0f} byte per cella)")

    print("\n     distribuzione dell'indice di oggi:")
    for lo, hi in [(0,1),(1,10),(10,25),(25,50),(50,75),(75,101)]:
        n = int(((v >= lo) & (v < hi)).sum())
        print(f"       {lo:3d}-{hi-1:3d}  {n:6d}  {'#'*int(60*n/max(len(v),1))}")
    print(f"\n     massimo {v.max()}, celle sopra 25: {(v>=25).sum()}")

    # Il numero che dice se il downscaling e' servito a qualcosa.
    dq = np.array([c[5] for c in celle])
    corretti = (np.abs(dq) >= 100).sum()
    print(f"\n     sotto-celle con dislivello oltre 100 m dal genitore: "
          f"{corretti} ({100*corretti/len(celle):.0f}%)")
    gruppi = pd.DataFrame({"p": [c[4] for c in celle], "v": v}).groupby("p")["v"]
    spread = (gruppi.max() - gruppi.min())
    print(f"     divario di indice fra sotto-celle dello stesso genitore:")
    print(f"       mediana {spread.median():.0f}, "
          f"90esimo percentile {spread.quantile(.9):.0f}, massimo {spread.max():.0f}")
    print(f"       (a risoluzione grossa questo divario era zero per definizione)")


if __name__ == "__main__":
    main()
