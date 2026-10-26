#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test: il download funziona da un IP DIVERSO (tramite un proxy)?

Serve a decidere se la modalita' multi-connessione con rotazione di IP e' utile:
se l'autorizzazione del file e' legata all'IP (cookie/token IP-bound), le richieste
da un IP diverso daranno 403 e il multi-IP non e' fattibile.

USO:
    python proxytest.py host:porta                 # proxy SOCKS5
    python proxytest.py socks5h://host:porta
    python proxytest.py http://host:porta

Scarica i primi 20 MB attraverso il proxy e stampa un verdetto.
"""
import sys
import time

from curl_cffi import requests

import download as d

TEST_BYTES = 20 * 1024 * 1024  # 20 MB


def main() -> int:
    if len(sys.argv) < 2:
        print("USO: python proxytest.py <host:porta | scheme://host:porta>")
        return 1
    proxy = sys.argv[1]
    proxy_url = proxy if "://" in proxy else "socks5h://" + proxy
    proxies = {"http": proxy_url, "https": proxy_url}
    print(f"Proxy: {proxy_url}")

    headers = d.read_headers()

    # Risolvo il link tramite la connessione normale (senza proxy)
    try:
        landing = d.read_landing()
        real = d.resolve_direct_url(landing, headers, "chrome")
        print(f"link risolto: {real[:80]}...")
    except Exception as e:
        print(f"[!] risoluzione fallita (link scaduto?): {e}")
        return 1

    # Scarico i primi 20 MB ATTRAVERSO il proxy (quindi da un IP diverso)
    h = dict(headers)
    h["Accept-Encoding"] = "identity"
    h["Range"] = f"bytes=0-{TEST_BYTES - 1}"
    t0 = time.time()
    got = 0
    try:
        r = requests.get(real, headers=h, proxies=proxies, impersonate="chrome",
                         stream=True, timeout=60)
    except Exception as e:
        print(f"[!] richiesta via proxy fallita: {e}")
        return 1
    print(f"status: {r.status_code}  (206 o 200 = accettato)")
    for k in ("Content-Range", "Content-Length", "Content-Type"):
        if k in r.headers:
            print(f"  {k}: {r.headers[k]}")
    ctype = r.headers.get("Content-Type", "").lower()
    for ch in r.iter_content(chunk_size=256 * 1024):
        got += len(ch)
        if got >= TEST_BYTES:
            break
    r.close()
    dt = max(time.time() - t0, 1e-6)
    print(f"scaricati {got/1e6:.1f} MB in {dt:.1f}s = {got/1e6/dt:.2f} MB/s su questa connessione")

    ok = r.status_code in (200, 206) and got > 1_000_000 and "text/html" not in ctype
    print("\nVERDETTO:",
          "FUNZIONA da IP diverso -> multi-IP fattibile"
          if ok else
          "BLOCCATO (auth legata all'IP o HTML) -> multi-IP non fattibile")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
