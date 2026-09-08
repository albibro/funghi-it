#!/usr/bin/env python3
"""
05_modello.py — Calcola l'indice 0-100 per cella, con scomposizione dei fattori.

COME FUNZIONA, IN BREVE:

  Per ogni cella e per ogni giorno bersaglio (oggi e i prossimi 7), il punteggio
  e' il PRODOTTO di sei fattori, ciascuno fra 0 e 1:

    trigger    c'e' stata una pioggia abbondante al momento giusto?
    termica    il suolo e' nella finestra di temperatura utile?
    umidita    c'e' acqua in superficie adesso?
    ospite     c'e' bosco micorrizico compatibile, alla quota giusta?
    esposizione  correzione fine per versanti nord/sud
    gelo       una gelata recente ha chiuso la stagione?

  indice = 100 x trigger x termica x umidita x ospite x esposizione x gelo

PERCHE' UN PRODOTTO E NON UNA SOMMA PESATA:
  Sono condizioni necessarie, non indizi che si compensano. Una faggeta perfetta
  a 15 gradi ma senza una goccia d'acqua da un mese non produce niente, e una
  somma pesata le darebbe comunque 60 su 100. Il prodotto azzera, che e' quello
  che fa la natura.
  Il rovescio della medaglia e' che i punteggi alti sono rari: 100 significa che
  tutti e sei i fattori sono al massimo contemporaneamente, cosa che non succede
  quasi mai. Va letto come un indice relativo, non come una probabilita'.

LA PARTE CHE RENDE LA MAPPA ONESTA:
  Per ogni cella salviamo i sei fattori separati, piu' la data e l'entita' della
  pioggia che ha vinto come trigger. Cosi' l'utente non legge "68" ma
  "68 perche' 62 mm il 21 agosto, 15 giorni fa, e il suolo e' a 16.2 gradi".
  Un numero senza spiegazione, su un tema dove la gente va a camminare nei
  boschi, sarebbe irresponsabile.

NOTA SULL'ORIZZONTE:
  Per un giorno bersaglio entro 8 giorni da oggi, la pioggia trigger e' gia'
  caduta: la finestra di latenza 8-25 giorni guarda tutta nel passato. Quindi
  i giorni futuri non sono una previsione meteo travestita, sono un conto su
  dati osservati. Solo temperatura e umidita' dei giorni finali vengono dal
  forecast, ed e' la parte piu' affidabile di un modello meteo.

ESECUZIONE:
    python scripts/05_modello.py --specie config/porcino.yml
"""

import argparse
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd
import yaml


# --- Funzioni di appartenenza ------------------------------------------------

def rampa(x, basso, alto, minimo=0.0):
    """Sale linearmente da `minimo` a 1 fra `basso` e `alto`."""
    y = np.clip((x - basso) / (alto - basso), 0.0, 1.0)
    return minimo + (1.0 - minimo) * y


def trapezio(x, zero_basso, pieno_basso, pieno_alto, zero_alto):
    """Nullo agli estremi, pieno nel mezzo, con rampe lineari ai lati."""
    x = np.asarray(x, dtype=float)
    su = np.clip((x - zero_basso) / (pieno_basso - zero_basso), 0.0, 1.0)
    giu = np.clip((zero_alto - x) / (zero_alto - pieno_alto), 0.0, 1.0)
    return np.minimum(su, giu)


def campana_latenza(giorni, picco, sigma, gmin, gmax):
    """Peso della latenza: campana centrata sul picco, azzerata fuori finestra."""
    g = np.asarray(giorni, dtype=float)
    w = np.exp(-0.5 * ((g - picco) / sigma) ** 2)
    return np.where((g >= gmin) & (g <= gmax), w, 0.0)


# --- Calcolo per cella --------------------------------------------------------

def calcola_cella(serie, statica, p, giorni_bersaglio):
    """serie: DataFrame di una cella, indicizzato per giorno, ordinato.
    Ritorna una lista di dizionari, uno per giorno bersaglio."""

    idx = serie.index
    prec = serie["prec_mm"].values
    st6 = serie["st6_med"].values
    st18 = serie["st18_med"].values
    sm9 = serie["sm9_med"].values
    sm27 = serie["sm27_med"].values
    tmin = serie["t2m_min"].values

    # Pioggia cumulata sulle 72 ore che finiscono in ciascun giorno.
    fin = p["trigger"]["finestra_ore"] // 24
    p72 = pd.Series(prec).rolling(fin, min_periods=fin).sum().values

    pos = {d: i for i, d in enumerate(idx)}
    out = []

    for D in giorni_bersaglio:
        if D not in pos:
            continue
        i = pos[D]

        # ---- 1. Trigger: cerchiamo il migliore nella finestra di latenza -----
        lat = p["latenza"]
        best = 0.0
        best_info = None
        for L in range(lat["giorni_min"], lat["giorni_max"] + 1):
            j = i - L
            if j < 0 or np.isnan(p72[j]) or p72[j] < p["trigger"]["soglia_mm"]:
                continue

            w_lat = float(campana_latenza(L, lat["giorni_picco"], lat["sigma"],
                                          lat["giorni_min"], lat["giorni_max"]))
            w_mm = float(rampa(p72[j], p["trigger"]["soglia_mm"],
                               p["trigger"]["mm_saturazione"],
                               p["trigger"]["peso_minimo"]))

            ua = p["umidita_antecedente"]
            k0 = max(0, j - ua["giorni"])
            ante = np.nanmean(sm27[k0:j]) if j > k0 else np.nan
            w_ante = 1.0 if np.isnan(ante) else float(
                rampa(ante, ua["secco"], ua["saturo"], ua["peso_minimo"]))

            s = w_lat * w_mm * w_ante
            if s > best:
                best = s
                best_info = {
                    "data": str(idx[j].date()),
                    "mm72": round(float(p72[j]), 1),
                    "giorni_fa": int(L),
                    "peso_latenza": round(w_lat, 3),
                    "peso_pioggia": round(w_mm, 3),
                    "peso_umidita_pre": round(w_ante, 3),
                }
        f_trigger = best

        # ---- 2. Finestra termica del suolo ------------------------------------
        te = p["termica"]
        k0 = max(0, i - te["giorni_finestra"] + 1)
        m6 = float(np.nanmean(st6[k0:i + 1]))
        m18 = float(np.nanmean(st18[k0:i + 1]))
        f6 = float(trapezio(m6, te["t_zero_basso"], te["t_pieno_basso"],
                            te["t_pieno_alto"], te["t_zero_alto"]))
        f18 = float(trapezio(m18, te["t_zero_basso"], te["t_pieno_basso"],
                             te["t_pieno_alto"], te["t_zero_alto"]))
        f_termica = te["peso_6cm"] * f6 + te["peso_18cm"] * f18

        # ---- 3. Umidita' corrente ---------------------------------------------
        uc = p["umidita_corrente"]
        k0 = max(0, i - uc["giorni"] + 1)
        m_sm9 = float(np.nanmean(sm9[k0:i + 1]))
        f_umid = float(rampa(m_sm9, uc["secco"], uc["saturo"], uc["peso_minimo"]))

        # ---- 4. Gelo -----------------------------------------------------------
        ge = p["gelo"]
        k0 = max(0, i - ge["giorni"] + 1)
        gelato = bool(np.nanmin(tmin[k0:i + 1]) < ge["soglia_c"])
        f_gelo = ge["penalita"] if gelato else 1.0

        # ---- 5. Esposizione ----------------------------------------------------
        es = p["esposizione"]
        north = float(statica.get("northness", 0.0) or 0.0)
        # Suolo caldo: il nord aiuta. Suolo freddo: il nord penalizza.
        segno = np.tanh((m6 - es["t_neutra"]) / 3.0)
        f_esp = float(np.clip(1.0 + es["ampiezza"] * north * segno, 0.7, 1.3))

        # ---- 6. Ospite ---------------------------------------------------------
        f_ospite = float(statica.get("idoneita_ospite", 0.0) or 0.0)

        indice = 100.0 * f_trigger * f_termica * f_umid * f_ospite * f_esp * f_gelo

        out.append({
            "giorno": str(D.date()),
            "indice": int(round(np.clip(indice, 0, 100))),
            "fattori": {
                "trigger": round(f_trigger, 3),
                "termica": round(f_termica, 3),
                "umidita": round(f_umid, 3),
                "ospite": round(f_ospite, 3),
                "esposizione": round(f_esp, 3),
                "gelo": round(f_gelo, 3),
            },
            "dettaglio": {
                "pioggia": best_info,
                "suolo_6cm": round(m6, 1),
                "suolo_18cm": round(m18, 1),
                "umidita_9cm": round(m_sm9, 3),
                "gelata_recente": gelato,
            },
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--specie", default="config/porcino.yml")
    ap.add_argument("--storico", default="data/meteo/storico.csv.gz")
    ap.add_argument("--static", default="data/static/cells_static.csv")
    ap.add_argument("--outdir", default="data/output")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.specie))
    p = cfg["modello"]
    nome = cfg["nome"]

    df = pd.read_csv(args.storico)
    df["giorno"] = pd.to_datetime(df["giorno"])
    st = pd.read_csv(args.static).set_index("cell_id")
    st = st[st["attiva"]] if "attiva" in st.columns else st

    ultimo = df["giorno"].max()
    primo = df["giorno"].min()

    # "Oggi" e' la data reale, non l'ultimo giorno dello storico. Confonderle era
    # un errore: con lo storico esteso alla previsione, l'ultimo giorno e' fra una
    # settimana, e prendendolo come oggi ogni giorno bersaglio successivo cadeva
    # oltre la fine dei dati e veniva scartato. Restava sempre un giorno solo.
    oggi = pd.Timestamp.today().normalize()
    if not (primo <= oggi <= ultimo):
        # Storico senza previsione (subito dopo il bootstrap) o dati vecchi:
        # ripieghiamo sull'ultimo giorno disponibile.
        print(f"[nota] oggi ({oggi.date()}) e' fuori dallo storico "
              f"({primo.date()}..{ultimo.date()}): uso l'ultimo giorno disponibile")
        oggi = ultimo

    bersagli = [oggi + timedelta(days=k) for k in range(0, p["giorni_previsti"] + 1)]
    bersagli = [b for b in bersagli if b <= ultimo]
    print(f"[dati] {df.cell_id.nunique()} celle, "
          f"{primo.date()} .. {ultimo.date()}")
    print(f"[calcolo] {len(bersagli)} giorni bersaglio: "
          f"{bersagli[0].date()} .. {bersagli[-1].date()}")
    if len(bersagli) == 1:
        print("[nota] nessun giorno di previsione nello storico: lo scarico "
              "giornaliero con --forecast-days li aggiungera'.")

    risultati = {}
    for cid, g in df.groupby("cell_id"):
        if cid not in st.index:
            continue
        serie = g.set_index("giorno").sort_index()
        risultati[cid] = calcola_cella(serie, st.loc[cid], p, bersagli)

    # --- GeoJSON per la mappa -------------------------------------------------
    lato = 0.1
    feats = []
    for cid, giorni in risultati.items():
        r = st.loc[cid]
        lat, lon = float(r["lat"]), float(r["lon"])
        y0, x0 = round(lat - lato / 2, 4), round(lon - lato / 2, 4)
        props = {
            "id": cid,
            "quota": int(r["quota_media"]) if pd.notna(r["quota_media"]) else None,
            "bosco": round(float(r["bosco_frac"]), 3),
            "macroregione": r["macroregione"],
        }
        for k, d in enumerate(giorni):
            props[f"i{k}"] = d["indice"]
        props["oggi"] = giorni[0]["indice"]
        props["fattori"] = giorni[0]["fattori"]
        props["dettaglio"] = giorni[0]["dettaglio"]
        feats.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [[
                [x0, y0], [x0 + lato, y0], [x0 + lato, y0 + lato],
                [x0, y0 + lato], [x0, y0]]]},
        })

    os.makedirs(args.outdir, exist_ok=True)
    geo = {
        "type": "FeatureCollection",
        "metadata": {
            "specie": nome,
            "nome_scientifico": cfg["nome_scientifico"],
            "generato": pd.Timestamp.now("UTC").isoformat(),
            "giorni": [str(b.date()) for b in bersagli],
            "n_celle": len(feats),
        },
        "features": feats,
    }
    gpath = os.path.join(args.outdir, f"indice_{nome}.geojson")
    json.dump(geo, open(gpath, "w"), separators=(",", ":"))

    # --- Riepilogo -------------------------------------------------------------
    v = np.array([f["properties"]["oggi"] for f in feats])
    mb = os.path.getsize(gpath) / 1e6
    print(f"\n[ok] {len(feats)} celle -> {gpath} ({mb:.2f} MB)")
    print(f"\n     distribuzione dell'indice di oggi:")
    for lo, hi in [(0, 1), (1, 10), (10, 25), (25, 50), (50, 75), (75, 101)]:
        n = int(((v >= lo) & (v < hi)).sum())
        barra = "#" * int(60 * n / max(len(v), 1))
        print(f"       {lo:3d}-{hi - 1:3d}  {n:5d}  {barra}")
    print(f"\n     massimo {v.max()}, mediana {int(np.median(v))}, "
          f"celle sopra 25: {(v >= 25).sum()}")

    print("\n     fattore che limita di piu', sulle celle con indice > 0:")
    nomi = ["trigger", "termica", "umidita", "ospite"]
    vivi = [f["properties"] for f in feats if f["properties"]["oggi"] > 0]
    if vivi:
        conta = {n: 0 for n in nomi}
        for pr in vivi:
            peggiore = min(nomi, key=lambda n: pr["fattori"][n])
            conta[peggiore] += 1
        for n, c in sorted(conta.items(), key=lambda kv: -kv[1]):
            print(f"       {n:<10s} {c:5d} celle")
    else:
        print("       nessuna cella con indice positivo")

    top = sorted(feats, key=lambda f: -f["properties"]["oggi"])[:8]
    print("\n     le 8 celle piu' alte:")
    for f in top:
        pr = f["properties"]
        d = pr["dettaglio"]["pioggia"]
        piog = f"{d['mm72']}mm {d['giorni_fa']}gg fa" if d else "nessun trigger"
        print(f"       {pr['id']}  indice {pr['oggi']:3d}  "
              f"{pr['quota']:4d}m  {pr['macroregione']:<12s} "
              f"suolo {pr['dettaglio']['suolo_6cm']:4.1f}C  {piog}")


if __name__ == "__main__":
    main()
