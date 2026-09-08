# funghi-it — indice previsionale di crescita dei funghi

Mappa statica dell'Italia con un indice 0-100 di probabilita' di buttata, per
cella di 0.1 gradi, ricalcolato una volta al giorno da un job schedulato.
Nessun modello linguistico a runtime: il punteggio esce da una formula
deterministica, versionata, ispezionabile.

**Non e' e non sara' mai un riconoscitore di specie da fotografia.**

---

## 1. Account da creare

| Servizio | Serve per | Costo | Obbligatorio |
|---|---|---|---|
| **GitHub** | codice, job giornaliero (Actions), hosting (Pages) | 0 € | si' |
| **Copernicus CLMS** (land.copernicus.eu) | scaricare Corine Land Cover, una volta sola | 0 € | si' |
| **Cloudflare** | hosting alternativo a GitHub Pages | 0 € | no |

Cose che **non** richiedono account: Open-Meteo (nessuna API key), il DEM
Copernicus GLO-90 (bucket AWS pubblico), le API di ricerca di GBIF e iNaturalist.

> Tieni il repository **pubblico**: su repo pubblici i minuti di GitHub Actions
> sono illimitati e GitHub Pages e' incluso. Su repo privato avresti 2000
> minuti/mese e Pages solo a pagamento.

---

## 2. Preparazione della macchina (una volta sola)

Serve Python 3.11 o superiore.

**macOS**
```bash
brew install python@3.12 git
```

**Windows** — installa Python da python.org spuntando "Add python.exe to PATH",
poi installa Git da git-scm.com. I comandi qui sotto vanno lanciati in
PowerShell.

**Linux (Ubuntu/Debian)**
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

Poi, nella cartella dove vuoi tenere il progetto:

```bash
git clone https://github.com/TUO-UTENTE/funghi-it.git
cd funghi-it
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`source .venv/bin/activate` va rilanciato ogni volta che apri un terminale
nuovo: attiva l'ambiente isolato dove sono installate le librerie.

---

## 3. Costruzione dei dati statici

Questi tre passaggi si fanno **una volta sola**. I risultati sono file CSV
piccoli che finiscono nel repository; il job giornaliero non li ricalcola mai.

### 3.1 Griglia

```bash
python scripts/00_build_grid.py
```

Scarica i confini regionali, ritaglia una griglia 0.1 gradi e tiene le celle con
almeno il 5% di terraferma. Risultato: **3668 celle** in `data/grid/cells.csv`.
Richiede meno di un minuto.

### 3.2 Corine Land Cover

Il download di Corine non e' automatizzabile senza credenziali, quindi si fa a
mano una volta:

1. Registrati su https://land.copernicus.eu (Register, poi conferma via email).
2. Vai su **CORINE Land Cover 2018 (raster 100 m), Europe** e scarica il
   pacchetto pre-confezionato in GeoTIFF (circa 1,5 GB compressi).
3. Scompatta e copia il file principale — si chiama qualcosa come
   `U2018_CLC2018_V2020_20u1.tif` — in `data/raw/clc/`.

Poi:

```bash
python scripts/01_corine_cells.py --clc data/raw/clc/U2018_CLC2018_V2020_20u1.tif
```

Scorre il raster una volta sola e produce, per ogni cella, la frazione occupata
da latifoglie (311), conifere (312), bosco misto (313) e da alcune classi aperte.
Risultato: `data/static/cells_corine.csv`. Richiede 5-15 minuti.

### 3.3 Modello digitale del terreno

```bash
python scripts/02_dem_cells.py
```

Scarica i tile Copernicus GLO-90 che coprono l'Italia (bucket pubblico, nessuna
credenziale), calcola pendenza ed esposizione a 90 m e aggrega su cella: quota
media, dislivello interno, frazione in ciascuna fascia altimetrica, frazione
esposta a ciascuno degli 8 settori, e la "northness" (da -1 tutto a sud a +1
tutto a nord). Risultato: `data/static/cells_dem.csv`.

I tile vengono cancellati man mano per non riempire il disco; usa
`--keep-tiles` se vuoi conservarli. Richiede 20-40 minuti e ~500 MB di traffico.

### 3.4 Commit

```bash
git add data/grid/cells.csv data/static/*.csv
git commit -m "dati statici: griglia, corine, dem"
git push
```

I file `.tif` grezzi **non** vanno committati (sono gia' esclusi in `.gitignore`).

---

## 4. Il modello

Per ogni cella e per ogni giorno, il punteggio e' il prodotto di sei fattori
compresi fra 0 e 1:

| Fattore | Cosa misura |
|---|---|
| trigger | pioggia >= 25 mm in 72 ore, avvenuta 8-25 giorni fa, con picco a 14 |
| termica | temperatura del suolo a 6 e 18 cm, trapezio pieno fra 12 e 18 gradi |
| umidita | umidita' del suolo a 3-9 cm negli ultimi 3 giorni |
| ospite | bosco micorrizico compatibile, alla quota giusta per la macroregione |
| esposizione | correzione per versanti nord/sud in funzione della temperatura |
| gelo | penalita' se ci sono state gelate negli ultimi 5 giorni |

`indice = 100 x trigger x termica x umidita x ospite x esposizione x gelo`

Si moltiplicano perche' sono condizioni necessarie, non indizi che si compensano.
Il rovescio e' che i valori alti sono rari: va letto come indice relativo fra
zone nello stesso giorno, non come probabilita'.

Tutti i parametri stanno in `config/porcino.yml` e si cambiano senza toccare
il codice.

---

## 5. Andare in produzione

### 5.1 Crea il repository

Su github.com, "New repository", nome `funghi-it`, visibilita' **Public**
(su repo pubblici i minuti di Actions sono illimitati e Pages e' incluso).
Non spuntare "Add a README".

Poi, dalla cartella del progetto:

```bash
git init
git add .
git commit -m "pipeline dati, modello e mappa"
git branch -M main
git remote add origin https://github.com/TUO-UTENTE/funghi-it.git
git push -u origin main
```

### 5.2 Carica lo storico meteo come Release

Lo storico non sta in git: crescerebbe di qualche megabyte al giorno per sempre.
Vive come allegato di una Release, che si sovrascrive senza accumulare versioni.

1. Sul repository, "Releases", poi "Create a new release".
2. Tag: scrivi esattamente `dati`. Titolo: `Storico meteo`.
3. Trascina nel riquadro degli allegati il file `data/meteo/storico.csv.gz`.
4. Pubblica.

Il nome del tag deve essere `dati`: il workflow lo cerca con quel nome esatto.

### 5.3 Attiva le pagine

Settings, Pages, alla voce "Source" scegli **GitHub Actions** (non "Deploy from
a branch").

### 5.4 Prima esecuzione

Actions, "Aggiorna indice", "Run workflow". Dopo tre o quattro minuti il sito e'
online su `https://TUO-UTENTE.github.io/funghi-it/`.

Da li' in poi gira da solo ogni mattina alle 5:40 UTC.

### 5.5 Se preferisci Cloudflare Pages

Non serve: GitHub Pages basta e ha un pezzo in meno. Se lo vuoi lo stesso,
collega il repository su Cloudflare Pages con build command vuoto e directory
di output `sito`, e togli gli ultimi tre passi dal workflow.

---

## 6. Cosa puo' rompersi

**Scarico meteo rifiutato.** I runner di GitHub hanno IP condivisi con
moltissimi utenti e Open-Meteo limita per IP. Il workflow lo prevede: se lo
scarico fallisce ripubblica la mappa con lo storico del giorno prima e lascia
un avviso nel riepilogo dell'esecuzione. Se succede per giorni di fila, lancia
`python scripts/04_fetch_meteo.py` dal tuo computer e ricarica lo storico
sulla Release.

**Storico assente.** Se cancelli la Release `dati` il workflow si ferma
subito con un errore chiaro. Ricrearla richiede un nuovo caricamento iniziale
da 45 giorni, circa un'ora e mezza.

**Vincolo di licenza.** Il piano gratuito di Open-Meteo copre solo l'uso non
commerciale: niente pubblicita', niente abbonamenti. Il costo zero regge finche'
il progetto resta tale.

---

## 7. Stato

- [x] Griglia 0.1 gradi, 3668 celle
- [x] Aggregazione Corine Land Cover
- [x] Quota, pendenza, esposizione da Copernicus GLO-90
- [x] Idoneita' dell'ospite e selezione celle attive: 2065 celle
- [x] Scarico meteo incrementale con governatore dei limiti
- [x] Modello dell'indice con scomposizione dei fattori
- [x] Mappa MapLibre GL
- [x] Automazione giornaliera e pubblicazione
- [ ] Backtesting su GBIF e iNaturalist, taratura dei parametri
- [ ] Seconda specie

---

## 8. Attribuzioni obbligatorie

Nel footer della mappa:

- Meteo: **Open-Meteo.com**, CC BY 4.0
- Uso del suolo: **Corine Land Cover**, Copernicus Land Monitoring Service
- Quota: **Copernicus DEM GLO-90**, (c) DLR e.V. 2010-2014 / Airbus DS 2014-2018,
  distribuito da ESA
- Confini: openpolis/geojson-italy (ISTAT)
- Cartografia: **OpenFreeMap** e **OpenStreetMap**

## 9. Avvertenza

L'indice e' una stima statistica di condizioni favorevoli, non una previsione di
presenza. **La raccolta dei funghi in Italia e' regolata da leggi regionali e da
regolamenti di parchi, comuni e comunita' montane**: permessi, quantitativi
massimi giornalieri, giornate di divieto, aree interdette e diametro minimo
cambiano da territorio a territorio. Informati presso l'ente competente prima di
raccogliere. Non raccogliere e non consumare funghi che non sai identificare con
certezza: rivolgiti a un ispettorato micologico ASL.

Questo progetto non include e non includera' mai il riconoscimento di specie da
fotografia.
