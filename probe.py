#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnostica rapida: verifica che l'impronta TLS e gli header accedano al file.

STEP 1: risolve il link diretto dall'URL in url.txt (segue hx-redirect / Location).
STEP 2: richiede i primi 64 KB del file con una Range request.

Un esito 206 allo Step 2 significa che il server accetta l'impronta e supporta il resume.
"""
import sys
import pathlib

from curl_cffi import requests

here = pathlib.Path(__file__).parent
landing = (here / "url.txt").read_text(encoding="utf-8").strip().splitlines()[0]

# Legge tutti gli header da headers.txt ("Nome: Valore", '#' = commento)
hdr = {}
for line in (here / "headers.txt").read_text(encoding="utf-8").splitlines():
    t = line.strip()
    if not t or t.startswith("#") or ":" not in t:
        continue
    name, value = t.split(":", 1)
    if name.strip() and value.strip():
        hdr[name.strip()] = value.strip()

print("== Step 1: risolvo il link diretto ==")
r = requests.get(landing, headers=hdr, impersonate="chrome", allow_redirects=False, timeout=30)
print("status:", r.status_code)
real = r.headers.get("hx-redirect") or r.headers.get("location")
print("redirect ->", (real[:90] + "...") if real else None)
if not real:
    # Se non c'e' redirect, il landing stesso potrebbe gia' essere il file diretto.
    if r.headers.get("Content-Disposition") or r.headers.get("Content-Type", "").startswith("application/"):
        real = landing
    else:
        print("Nessun link diretto. Body (primi 300 char):", r.text[:300])
        sys.exit(1)

print("\n== Step 2: Range request sul file (primi 64 KB) ==")
h2 = dict(hdr)
h2["Range"] = "bytes=0-65535"
got = 0
resp = requests.get(real, headers=h2, impersonate="chrome", stream=True, timeout=60)
print("status:", resp.status_code, "(206 = resume supportato)")
for k in ("Content-Range", "Content-Length", "Accept-Ranges", "Content-Type", "Content-Disposition"):
    if k in resp.headers:
        print(f"  {k}: {resp.headers[k]}")
for chunk in resp.iter_content(chunk_size=16384):
    got += len(chunk)
resp.close()
print("byte ricevuti:", got)
print("\nRISULTATO:", "OK - impronta accettata e resume disponibile" if got > 1000 else "NON scarica dati")
