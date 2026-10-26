#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test: does the download work from a DIFFERENT IP (through a proxy)?

Use it to decide whether multi-connection mode with IP rotation is worthwhile:
if the file authorization is bound to the IP (IP-bound cookie/token), requests
from a different IP will return 403 and multi-IP is not feasible.

USAGE:
    python proxytest.py host:port                  # SOCKS5 proxy
    python proxytest.py socks5h://host:port
    python proxytest.py http://host:port

Downloads the first 20 MB through the proxy and prints a verdict.
"""
import sys
import time

from curl_cffi import requests

import download as d

TEST_BYTES = 20 * 1024 * 1024  # 20 MB


def main() -> int:
    if len(sys.argv) < 2:
        print("USAGE: python proxytest.py <host:port | scheme://host:port>")
        return 1
    proxy = sys.argv[1]
    proxy_url = proxy if "://" in proxy else "socks5h://" + proxy
    proxies = {"http": proxy_url, "https": proxy_url}
    print(f"Proxy: {proxy_url}")

    headers = d.read_headers()

    # Resolve the link over the normal connection (no proxy)
    try:
        landing = d.read_landing()
        real = d.resolve_direct_url(landing, headers, "chrome")
        print(f"resolved link: {real[:80]}...")
    except Exception as e:
        print(f"[!] resolution failed (expired link?): {e}")
        return 1

    # Download the first 20 MB THROUGH the proxy (i.e. from a different IP)
    h = dict(headers)
    h["Accept-Encoding"] = "identity"
    h["Range"] = f"bytes=0-{TEST_BYTES - 1}"
    t0 = time.time()
    got = 0
    try:
        r = requests.get(real, headers=h, proxies=proxies, impersonate="chrome",
                         stream=True, timeout=60)
    except Exception as e:
        print(f"[!] request via proxy failed: {e}")
        return 1
    print(f"status: {r.status_code}  (206 or 200 = accepted)")
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
    print(f"downloaded {got/1e6:.1f} MB in {dt:.1f}s = {got/1e6/dt:.2f} MB/s on this connection")

    ok = r.status_code in (200, 206) and got > 1_000_000 and "text/html" not in ctype
    print("\nVERDICT:",
          "WORKS from a different IP -> multi-IP feasible"
          if ok else
          "BLOCKED (IP-bound auth or HTML) -> multi-IP not feasible")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
