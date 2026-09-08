#!/usr/bin/env python3
"""
04_fetch_meteo.py (v2) — Scarico meteo con governatore del ritmo e checkpoint.

COSA E' CAMBIATO RISPETTO ALLA v1 E PERCHE':

  La v1 contava le richieste HTTP invece del loro PESO. Open-Meteo applica tre
  limiti, tutti sul peso, non sul numero di richieste:
      600 al minuto, 5000 all'ora, 10000 al giorno.
  Una richiesta con 100 celle e 45 giorni pesa da sola 321 chiamate: due di fila
  sfondano il limite al minuto in tre secondi. Ed e' anche il motivo per cui il
  caricamento iniziale, che pesa 6638, non puo' stare dentro un'ora sola.

  La v2 aggiunge:
   1. un GOVERNATORE che tiene una finestra scorrevole del peso consumato negli
      ultimi 60 secondi e negli ultimi 60 minuti, e si mette in pausa da solo
      prima di superare i limiti invece di scoprirlo con un errore;
   2. un CONTATORE GIORNALIERO su file, cosi' due esecuzioni nello stesso giorno
      non si ignorano a vicenda;
   3. un CHECKPOINT dopo ogni lotto: se il processo cade o lo interrompi, al
      riavvio riprende dal lotto successivo invece di ricominciare da capo;
   4. lotti da 50 celle invece di 100, per dare al governatore una grana piu'
      fine su cui lavorare.

  Il caricamento iniziale ora richiede circa un'ora e mezza. Non e' un rallenta-
  mento introdotto da noi: e' il tempo che i limiti impongono. L'aggiornamento
  giornaliero pesa 2065 e si esaurisce in cinque minuti.

ESECUZIONE:
    python scripts/04_fetch_meteo.py --bootstrap 45     # solo la prima volta
    python scripts/04_fetch_meteo.py                    # tutti i giorni

  Se una sessione si interrompe, rilancia lo stesso identico comando: riprende.
  Con --riparti-da-zero ignora il checkpoint e ricomincia.
"""

import argparse
import collections
import json
import os
import sys
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

API = "https://api.open-meteo.com/v1/forecast"

HOURLY = [
    "precipitation",
    "soil_temperature_6cm",
    "soil_temperature_18cm",
    "soil_moisture_3_to_9cm",
    "soil_moisture_9_to_27cm",
    "temperature_2m",
]

STORICO = "data/meteo/storico.csv.gz"
CHECKPOINT = "data/meteo/.parziale.csv"
CONTATORE = "data/meteo/.quota.json"

GIORNI_DA_TENERE = 60
BATCH = 50

# Margine di sicurezza sotto i limiti dichiarati (600 / 5000 / 10000).
LIM_MINUTO = 500
LIM_ORA = 4300
LIM_GIORNO = 9200


class Governatore:
    """Tiene il ritmo sotto i limiti di Open-Meteo, che sono sul PESO.

    Mantiene una coda di (istante, peso) e, prima di ogni richiesta, calcola
    quanto aspettare perche' aggiungere quel peso non sfondi ne' la finestra da
    60 secondi ne' quella da un'ora. Il conto giornaliero sta su file, perche'
    deve sopravvivere fra un'esecuzione e l'altra.
    """

    def __init__(self, path_contatore, peso_gia_usato=0.0):
        self.eventi = collections.deque()   # (timestamp, peso)
        self.path = path_contatore
        self.oggi = date.today().isoformat()
        self.giorno = self._leggi() + peso_gia_usato
        if peso_gia_usato:
            print(f"[quota] parto da {self.giorno:.0f} chiamate gia' consumate oggi")

    def _leggi(self):
        if not os.path.exists(self.path):
            return 0.0
        try:
            d = json.load(open(self.path))
            return float(d["peso"]) if d.get("data") == self.oggi else 0.0
        except Exception:
            return 0.0

    def _scrivi(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump({"data": self.oggi, "peso": round(self.giorno, 1)},
                  open(self.path, "w"))

    def _consumo(self, finestra, ora):
        return sum(p for t, p in self.eventi if ora - t < finestra)

    def prenota(self, peso):
        """Blocca finche' non e' sicuro spendere `peso`.
        Ritorna False se il limite giornaliero non lo consente affatto."""
        if self.giorno + peso > LIM_GIORNO:
            return False
        while True:
            ora = time.time()
            while self.eventi and ora - self.eventi[0][0] > 3600:
                self.eventi.popleft()
            if not self.eventi:
                return True
            m = self._consumo(60, ora)
            h = self._consumo(3600, ora)
            attese = []
            if m + peso > LIM_MINUTO:
                piu_vecchio = min(t for t, _ in self.eventi if ora - t < 60)
                attese.append(60 - (ora - piu_vecchio) + 1)
            if h + peso > LIM_ORA:
                attese.append(3600 - (ora - self.eventi[0][0]) + 1)
            if not attese:
                return True
            att = max(1.0, min(attese))
            print(f"    ritmo: attendo {att:.0f}s "
                  f"(minuto {m:.0f}/{LIM_MINUTO}, ora {h:.0f}/{LIM_ORA})", flush=True)
            time.sleep(att)

    def registra(self, peso):
        self.eventi.append((time.time(), peso))
        self.giorno += peso
        self._scrivi()


def carica_celle(path):
    with open(path) as f:
        d = json.load(f)
    celle = pd.DataFrame(d["celle"])
    print(f"[celle] {len(celle)} celle attive per '{d['specie']}'")
    return celle


def quote_per_cella(celle, static_path):
    if not os.path.exists(static_path):
        print("[quota] cells_static.csv non trovato: uso la quota di Open-Meteo")
        return None
    st = pd.read_csv(static_path, usecols=["cell_id", "quota_media"])
    m = celle.merge(st, on="cell_id", how="left")
    if m["quota_media"].isna().any():
        print("[quota] alcune celle senza quota: uso la quota di Open-Meteo")
        return None
    return m["quota_media"].round(0).astype(int).values


def chiedi(params, tentativi=5):
    for k in range(1, tentativi + 1):
        try:
            r = requests.get(API, params=params, timeout=120)
            if r.status_code == 429:
                attesa = 90 * k
                print(f"    limite superato lo stesso, aspetto {attesa}s", flush=True)
                time.sleep(attesa)
                continue
            if r.status_code == 400:
                raise ValueError(r.text[:300])
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if k == tentativi:
                raise
            attesa = 5 * k
            print(f"    rete instabile ({e.__class__.__name__}), riprovo fra {attesa}s")
            time.sleep(attesa)
    raise RuntimeError("troppi tentativi falliti")


def aggrega(cell_id, hourly):
    """Da valori orari a una riga per giorno."""
    t = pd.Series(pd.to_datetime(hourly["time"]))
    df = pd.DataFrame({k: hourly.get(k) for k in HOURLY})
    df["giorno"] = t.dt.date.values
    g = df.groupby("giorno")
    out = pd.DataFrame({
        "prec_mm": g["precipitation"].sum(min_count=1),
        "st6_med": g["soil_temperature_6cm"].mean(),
        "st6_min": g["soil_temperature_6cm"].min(),
        "st6_max": g["soil_temperature_6cm"].max(),
        "st18_med": g["soil_temperature_18cm"].mean(),
        "sm9_med": g["soil_moisture_3_to_9cm"].mean(),
        "sm27_med": g["soil_moisture_9_to_27cm"].mean(),
        "t2m_min": g["temperature_2m"].min(),
    }).reset_index()
    out.insert(0, "cell_id", cell_id)
    # I giorni agli estremi possono essere monchi: falserebbero somme e medie.
    ore = g.size()
    return out[ore.reindex(out["giorno"]).values >= 20]


def scarica(celle, quote, past_days, forecast_days, gov, riparti):
    n_batch = int(np.ceil(len(celle) / BATCH))
    giorni = past_days + forecast_days

    fatti = set()
    if not riparti and os.path.exists(CHECKPOINT):
        vecchio = pd.read_csv(CHECKPOINT)
        fatti = set(vecchio["cell_id"].unique())
        print(f"[checkpoint] {len(fatti)} celle gia' scaricate, riprendo da li'")

    usa_quota = quote is not None
    for b in range(n_batch):
        lo, hi = b * BATCH, min((b + 1) * BATCH, len(celle))
        sub = celle.iloc[lo:hi]
        if set(sub["cell_id"]).issubset(fatti):
            continue

        peso = len(sub) * max(1.0, giorni / 14.0)
        if not gov.prenota(peso):
            print(f"\n[stop] limite giornaliero raggiunto al lotto {b + 1}/{n_batch}.")
            print("       Il lavoro fatto e' salvato. Rilancia domani lo stesso comando.")
            return False

        params = {
            "latitude": ",".join(f"{v:.4f}" for v in sub["lat"]),
            "longitude": ",".join(f"{v:.4f}" for v in sub["lon"]),
            "hourly": ",".join(HOURLY),
            "past_days": past_days,
            "forecast_days": forecast_days,
            "timezone": "Europe/Rome",
            "cell_selection": "nearest",
        }
        if usa_quota:
            params["elevation"] = ",".join(str(int(v)) for v in quote[lo:hi])

        try:
            data = chiedi(params)
        except ValueError as e:
            if usa_quota:
                print(f"    l'API rifiuta il parametro quota: {e}")
                print("    riprovo senza; le temperature saranno meno precise in quota.")
                usa_quota = False
                params.pop("elevation", None)
                data = chiedi(params)
            else:
                raise
        gov.registra(peso)

        loc = data if isinstance(data, list) else [data]
        if len(loc) != len(sub):
            raise RuntimeError(
                f"risposta incoerente: chieste {len(sub)}, ricevute {len(loc)}")

        parte = pd.concat([aggrega(c, l["hourly"])
                           for c, l in zip(sub["cell_id"].values, loc)],
                          ignore_index=True)
        parte.to_csv(CHECKPOINT, mode="a",
                     header=not os.path.exists(CHECKPOINT), index=False)

        print(f"  lotto {b + 1}/{n_batch}  celle {lo}-{hi}  "
              f"peso oggi {gov.giorno:.0f}/{LIM_GIORNO}", flush=True)
    return True


def unisci_storico(nuovo, path, giorni_da_tenere):
    nuovo["giorno"] = pd.to_datetime(nuovo["giorno"]).dt.date
    nuovo = nuovo.drop_duplicates(subset=["cell_id", "giorno"], keep="last")
    if os.path.exists(path):
        vecchio = pd.read_csv(path)
        vecchio["giorno"] = pd.to_datetime(vecchio["giorno"]).dt.date
        chiavi = set(zip(nuovo["cell_id"], nuovo["giorno"]))
        tieni = ~pd.Series(list(zip(vecchio["cell_id"], vecchio["giorno"]))).isin(chiavi)
        storico = pd.concat([vecchio[tieni.values], nuovo], ignore_index=True)
        print(f"[storico] {len(vecchio)} righe esistenti, "
              f"{(~tieni).sum()} sostituite, {len(nuovo)} aggiunte")
    else:
        storico = nuovo
        print(f"[storico] nuovo archivio, {len(nuovo)} righe")

    limite = date.today() - timedelta(days=giorni_da_tenere)
    prima = len(storico)
    storico = storico[storico["giorno"] >= limite]
    if prima != len(storico):
        print(f"[storico] scartate {prima - len(storico)} righe troppo vecchie")

    storico = storico.sort_values(["cell_id", "giorno"]).reset_index(drop=True)
    for c in storico.columns:
        if c not in ("cell_id", "giorno"):
            storico[c] = storico[c].astype(float).round(3)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    storico.to_csv(path, index=False, compression="gzip")
    return storico


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--celle", default="data/static/cells_attive_porcino.json")
    ap.add_argument("--static", default="data/static/cells_static.csv")
    ap.add_argument("--out", default=STORICO)
    ap.add_argument("--bootstrap", type=int, default=0)
    ap.add_argument("--past-days", type=int, default=5)
    ap.add_argument("--forecast-days", type=int, default=7)
    ap.add_argument("--peso-gia-usato", type=float, default=0.0,
                    help="chiamate gia' consumate oggi fuori da questo script")
    ap.add_argument("--riparti-da-zero", action="store_true")
    args = ap.parse_args()

    if args.riparti_da_zero and os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
        print("[checkpoint] azzerato")

    celle = carica_celle(args.celle)
    quote = quote_per_cella(celle, args.static)

    if args.bootstrap:
        past, fut = args.bootstrap, 0
        print(f"[modo] caricamento iniziale: {past} giorni di storico")
    else:
        past, fut = args.past_days, args.forecast_days
        print(f"[modo] aggiornamento: {past} passati + {fut} previsti")

    giorni = past + fut
    stima = len(celle) * max(1.0, giorni / 14.0)
    minuti = max(stima / LIM_ORA * 60, stima / LIM_MINUTO)
    print(f"[quota] peso totale stimato {stima:.0f} chiamate")
    print(f"[tempo] i limiti di Open-Meteo impongono almeno {minuti:.0f} minuti")

    gov = Governatore(CONTATORE, args.peso_gia_usato)
    t0 = time.time()
    completo = scarica(celle, quote, past, fut, gov, args.riparti_da_zero)

    if not os.path.exists(CHECKPOINT):
        print("[stop] nessun dato scaricato")
        return 1

    nuovo = pd.read_csv(CHECKPOINT)
    if not completo:
        print(f"[parziale] {nuovo.cell_id.nunique()} celle su {len(celle)} salvate.")
        return 2

    storico = unisci_storico(nuovo, args.out, GIORNI_DA_TENERE)
    os.remove(CHECKPOINT)

    mb = os.path.getsize(args.out) / 1e6
    print(f"\n[ok] {len(storico)} righe in {args.out} ({mb:.1f} MB)")
    print(f"     periodo: {storico.giorno.min()} .. {storico.giorno.max()}")
    print(f"     celle: {storico.cell_id.nunique()}")
    print(f"     peso consumato oggi: {gov.giorno:.0f}")
    print(f"     tempo: {(time.time() - t0) / 60:.0f} minuti")

    nan = storico[["prec_mm", "st6_med", "st18_med"]].isna().mean()
    if (nan > 0.01).any():
        print(f"[attenzione] valori mancanti: {dict(nan.round(3))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
