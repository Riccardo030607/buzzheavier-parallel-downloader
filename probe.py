#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick diagnostic: verify that the TLS fingerprint and headers can access the file.

STEP 1: resolve the direct link from the URL in url.txt (follows hx-redirect / Location).
STEP 2: request the first 64 KB of the file with a Range request.

A 206 result at Step 2 means the server accepts the fingerprint and supports resume.
"""
import sys
import pathlib

from curl_cffi import requests

here = pathlib.Path(__file__).parent
landing = (here / "url.txt").read_text(encoding="utf-8").strip().splitlines()[0]

# Read all headers from headers.txt ("Name: Value", '#' = comment)
hdr = {}
for line in (here / "headers.txt").read_text(encoding="utf-8").splitlines():
    t = line.strip()
    if not t or t.startswith("#") or ":" not in t:
        continue
    name, value = t.split(":", 1)
    if name.strip() and value.strip():
        hdr[name.strip()] = value.strip()

print("== Step 1: resolving the direct link ==")
r = requests.get(landing, headers=hdr, impersonate="chrome", allow_redirects=False, timeout=30)
print("status:", r.status_code)
real = r.headers.get("hx-redirect") or r.headers.get("location")
print("redirect ->", (real[:90] + "...") if real else None)
if not real:
    # If there's no redirect, the landing itself might already be the direct file.
    if r.headers.get("Content-Disposition") or r.headers.get("Content-Type", "").startswith("application/"):
        real = landing
    else:
        print("No direct link. Body (first 300 chars):", r.text[:300])
        sys.exit(1)

print("\n== Step 2: Range request on the file (first 64 KB) ==")
h2 = dict(hdr)
h2["Range"] = "bytes=0-65535"
got = 0
resp = requests.get(real, headers=h2, impersonate="chrome", stream=True, timeout=60)
print("status:", resp.status_code, "(206 = resume supported)")
for k in ("Content-Range", "Content-Length", "Accept-Ranges", "Content-Type", "Content-Disposition"):
    if k in resp.headers:
        print(f"  {k}: {resp.headers[k]}")
for chunk in resp.iter_content(chunk_size=16384):
    got += len(chunk)
resp.close()
print("bytes received:", got)
print("\nRESULT:", "OK - fingerprint accepted and resume available" if got > 1000 else "NOT downloading data")
