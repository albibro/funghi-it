#!/usr/bin/env python3
"""
06_build_sito.py — Mette insieme la cartella pubblicabile del sito.

COSA FA:
  Copia la pagina HTML e i GeoJSON dell'indice dentro `sito/`, che e' la
  cartella che GitHub Pages o Cloudflare Pages pubblicheranno cosi' com'e'.
  Non compila niente e non richiede Node: il sito e' un solo file HTML piu'
  i dati. Meno pezzi ci sono, meno cose si rompono da sole a novembre.

  Aggiunge anche:
    .nojekyll   evita che GitHub Pages ignori i file che iniziano per underscore
    robots.txt  permette l'indicizzazione

ESECUZIONE:
    python scripts/06_build_sito.py
    python -m http.server -d sito 8000      # per provarlo in locale
"""

import argparse
import os
import shutil


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default="web")
    ap.add_argument("--dati", default="data/output")
    ap.add_argument("--out", default="sito")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "data"), exist_ok=True)

    pagina = os.path.join(args.web, "index.html")
    if not os.path.exists(pagina):
        raise SystemExit(f"manca {pagina}")
    shutil.copy2(pagina, os.path.join(args.out, "index.html"))
    print(f"[sito] index.html copiato")

    n = 0
    for f in sorted(os.listdir(args.dati)):
        if f.endswith(".geojson"):
            src = os.path.join(args.dati, f)
            shutil.copy2(src, os.path.join(args.out, "data", f))
            print(f"[sito] {f}  ({os.path.getsize(src) / 1e6:.2f} MB)")
            n += 1
    if n == 0:
        raise SystemExit(f"nessun geojson in {args.dati}: lancia prima 05_modello.py")

    open(os.path.join(args.out, ".nojekyll"), "w").close()
    with open(os.path.join(args.out, "robots.txt"), "w") as f:
        f.write("User-agent: *\nAllow: /\n")

    tot = sum(os.path.getsize(os.path.join(dp, x))
              for dp, _, fs in os.walk(args.out) for x in fs)
    print(f"\n[ok] cartella '{args.out}' pronta, {tot / 1e6:.2f} MB")
    print(f"     provala con:  python -m http.server -d {args.out} 8000")
    print(f"     poi apri:     http://localhost:8000")


if __name__ == "__main__":
    main()
