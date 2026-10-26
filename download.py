#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robust download manager based on curl_cffi.

Features:
  1) PROGRESS BAR     -> percentage, downloaded/total bytes, speed and ETA.
  2) PERSISTENCE      -> writes to <OutFile>.part; if you re-run the script it
                         resumes from the exact point by reading the bytes already
                         on disk.
  3) AUTO-RETRY + RESUME -> if the connection drops it restarts on its own and
                         continues from the byte where it stopped (Range: bytes=pos-).
  4) MULTI-CONNECTION -> downloads segments in parallel (-n), with an optional list
                         of proxies (proxies.txt) to route each connection through a
                         different IP.
  5) FALLBACK         -> re-resolves the link from the configuration, rotates several
                         TLS fingerprints if the server rejects it, handles 200/206/416/403.

Configuration (files in the same folder):
  - url.txt      : the download link (/download?... endpoint or a direct link).
  - headers.txt  : the headers captured from the browser ("Name: Value"), including Cookie.
  - proxies.txt  : (optional) one proxy per line for multi-connection mode.

USAGE:
    python download.py -o MyFile.ext
    python download.py -o MyFile.ext --sha256 <hash>
    python download.py -o MyFile.ext -n 4
"""

import argparse
import hashlib
import json
import math
import os
import pathlib
import sys
import threading
import time
from urllib.parse import urljoin, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

HERE = pathlib.Path(__file__).parent
URL_FILE = HERE / "url.txt"
HEADERS_FILE = HERE / "headers.txt"
PROXIES_FILE = HERE / "proxies.txt"

# TLS fingerprint ladder for the fallback: the first one is tried; if the server
# rejects the TLS/403, it moves on to the next.
IMPERSONATE_CHAIN = ["chrome", "chrome124", "chrome120", "edge101"]

CHUNK = 1024 * 1024  # 1 MiB


# --------------------------------------------------------------------------- #
# Configuration reading
# --------------------------------------------------------------------------- #
def read_landing() -> str:
    txt = URL_FILE.read_text(encoding="utf-8").strip()
    if not txt or "PASTE" in txt.upper():
        raise SystemExit("ERROR: put the download link on the first line of url.txt")
    return txt.splitlines()[0].strip()


def read_headers() -> dict:
    """Reads headers.txt as a dictionary. One line per header ('Name: Value').
    Empty lines or lines starting with '#' are ignored."""
    headers = {}
    if not HEADERS_FILE.exists():
        return headers
    for line in HEADERS_FILE.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or ":" not in t:
            continue
        name, value = t.split(":", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            headers[name] = value
    return headers


def read_proxies() -> list:
    """Reads proxies.txt: one line per connection of the multi download.
    'direct' = no proxy (current network/tunnel). Otherwise host:port
    (SOCKS5) or a full URL. Returns a list of (None | 'socks5h://...')."""
    if not PROXIES_FILE.exists():
        return []
    out = []
    for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t.lower() == "direct":
            out.append(None)
        elif "://" in t:
            out.append(t)
        else:
            out.append("socks5h://" + t)
    return out


def wait_for_fresh_link(poll: int = 5, remind_every: int = 30) -> None:
    """If the link/token has expired, auto-retry alone isn't enough. Wait until a
    fresh link is saved in url.txt and then resume. The bytes already downloaded
    into the .part stay intact."""
    try:
        base_mtime = URL_FILE.stat().st_mtime
    except OSError:
        base_mtime = 0
    print("\n" + "=" * 70)
    print(">>> LINK/TOKEN EXPIRED - manual action required.")
    print(">>> 1) in the browser, recapture the download request (Copy as cURL)")
    print(">>> 2) paste the URL on the 1st line of url.txt (and update the Cookie in headers.txt)")
    print(">>> 3) save: the download resumes from the exact point, the .part bytes are safe.")
    print("=" * 70)
    waited = 0
    while True:
        time.sleep(poll)
        waited += poll
        try:
            if URL_FILE.stat().st_mtime != base_mtime:
                print(">>> url.txt updated: resuming the download.\n")
                return
        except OSError:
            pass
        if waited % remind_every == 0:
            print(f"    ...waiting for a fresh link in url.txt ({waited}s)")


# --------------------------------------------------------------------------- #
# STEP 1 - resolve the direct link (handles hx-redirect / 3xx Location)
# --------------------------------------------------------------------------- #
def resolve_direct_url(landing: str, headers: dict, impersonate: str) -> str:
    hdr = dict(headers)
    hdr.setdefault("Accept", "*/*")
    # stream=True: doesn't download the body (if it's already the file by mistake, it won't hit RAM)
    r = requests.get(landing, headers=hdr, impersonate=impersonate,
                     allow_redirects=False, stream=True, timeout=30)
    real = r.headers.get("hx-redirect") or r.headers.get("location")

    landing_host = urlparse(landing).hostname or ""

    if real:
        r.close()
        real = urljoin(landing, real)          # normalize any relative URLs
        pr = urlparse(real)
        # A valid link to the file is on a different host from the page, or has /d/ in the path
        looks_like_file = ("/d/" in pr.path) or (pr.hostname and pr.hostname != landing_host)
        if not looks_like_file:
            raise RequestException(
                "TOKEN EXPIRED: the server bounces back to the page "
                f"({real}) instead of giving the file link. Update url.txt "
                "with a fresh link from the browser (and the Cookie in headers.txt)."
            )
        return real

    # No redirect: accept the landing ONLY if it really is a file (not HTML).
    ctype = r.headers.get("Content-Type", "").lower()
    if r.headers.get("Content-Disposition") or ctype.startswith("application/"):
        r.close()
        return landing  # url.txt already contains the direct link to the file
    body = r.text[:200]
    r.close()
    raise RequestException(
        f"Step1: no direct link (status {r.status_code}, "
        f"Content-Type {ctype!r}). Expired link? Update url.txt. "
        f"Preview: {body!r}"
    )


# --------------------------------------------------------------------------- #
# Progress bar
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:6.2f} {unit}"
        n /= 1024
    return f"{n:6.2f} PB"


def draw_bar(done: int, total: int, session_bytes: int, session_start: float) -> None:
    elapsed = max(time.time() - session_start, 1e-6)
    speed = session_bytes / elapsed  # bytes/s of the current session
    if total > 0:
        frac = done / total
        width = 30
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        remaining = total - done
        eta = remaining / speed if speed > 0 else 0
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta)) if eta < 360000 else ">99h"
        msg = (f"\r[{bar}] {frac*100:5.1f}%  "
               f"{human(done)}/{human(total)}  "
               f"{human(speed)}/s  ETA {eta_str}   ")
    else:
        msg = f"\r{human(done)} downloaded  {human(speed)}/s   "
    sys.stdout.write(msg)
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# STEP 2 - a single download attempt with resume (single connection)
# Returns True if the file is complete.
# --------------------------------------------------------------------------- #
def attempt_download(real_url: str, headers: dict, impersonate: str,
                     part_path: pathlib.Path) -> bool:
    pos = part_path.stat().st_size if part_path.exists() else 0

    h2 = dict(headers)
    h2["Accept-Encoding"] = "identity"   # no gzip: Content-Length = real size
    if pos > 0:
        h2["Range"] = f"bytes={pos}-"

    resp = requests.get(real_url, headers=h2, impersonate=impersonate,
                        stream=True, timeout=120)
    status = resp.status_code

    # File already complete (the server says: range not satisfiable)
    if status == 416:
        resp.close()
        print(f"\n[info] The server responds 416: the .part ({human(pos)}) is already complete.")
        return True

    if status == 403:
        resp.close()
        raise RequestException("403 Forbidden: fingerprint/cookie rejected (trying fallback).")

    if status not in (200, 206):
        body = resp.text[:200]
        resp.close()
        raise RequestException(f"unexpected status {status}. Body: {body!r}")

    # If an HTML page arrives it's not the file -> don't save wrong content.
    ctype = resp.headers.get("Content-Type", "").lower()
    if "text/html" in ctype or "text/plain" in ctype:
        body = resp.text[:200]
        resp.close()
        raise RequestException(
            f"HTML/text response (Content-Type {ctype!r}), NOT the file. "
            f"Expired link? Update url.txt. Preview: {body!r}"
        )

    # Determine the total size
    total = 0
    cr = resp.headers.get("Content-Range")  # e.g.: "bytes 1000-69999/70000"
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            total = 0
    elif resp.headers.get("Content-Length"):
        total = int(resp.headers["Content-Length"]) + (pos if status == 206 else 0)

    # The server IGNORED the Range (200 instead of 206): restart from zero.
    if pos > 0 and status == 200:
        print(f"\n[warn] The server ignored the resume (200): restarting from 0.")
        pos = 0
        mode = "wb"
    else:
        mode = "ab" if pos > 0 else "wb"

    session_start = time.time()
    session_bytes = 0
    done = pos
    last_draw = 0.0

    with open(part_path, mode) as f:
        for chunk in resp.iter_content(chunk_size=CHUNK):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            session_bytes += len(chunk)
            now = time.time()
            if now - last_draw >= 0.25:
                draw_bar(done, total, session_bytes, session_start)
                last_draw = now
    resp.close()
    draw_bar(done, total, session_bytes, session_start)
    print()

    if total and done < total:
        raise RequestException(f"stream interrupted: {human(done)}/{human(total)}")
    return True


# --------------------------------------------------------------------------- #
# MULTI-CONNECTION MODE (parallel segments with range)
# --------------------------------------------------------------------------- #
def get_total_size(real: str, headers: dict, impersonate: str) -> int:
    """Requests 1 byte (Range 0-0) to read the total size from the Content-Range."""
    h = dict(headers)
    h["Accept-Encoding"] = "identity"
    h["Range"] = "bytes=0-0"
    r = requests.get(real, headers=h, impersonate=impersonate, stream=True, timeout=30)
    st = r.status_code
    total = 0
    cr = r.headers.get("Content-Range")
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            total = 0
    elif st == 200 and r.headers.get("Content-Length"):
        total = int(r.headers["Content-Length"])
    ct = r.headers.get("Content-Type", "").lower()
    r.close()
    if st == 403 or "text/html" in ct:
        raise RequestException("403/HTML on sizing: invalid link or fingerprint.")
    if not total:
        raise RequestException(f"unable to read the total size (status {st}).")
    return total


def _progress_path(part_path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(str(part_path) + ".progress")


def save_segments(progress_path: pathlib.Path, total: int, segments: list) -> None:
    tmp = pathlib.Path(str(progress_path) + ".tmp")
    tmp.write_text(json.dumps({"total": total, "segments": segments}), encoding="utf-8")
    tmp.replace(progress_path)


def load_or_init_segments(part_path: pathlib.Path, total: int, n: int,
                          progress_path: pathlib.Path) -> list:
    # Resume the saved state if consistent with the total size.
    if progress_path.exists():
        try:
            data = json.loads(progress_path.read_text(encoding="utf-8"))
            if int(data.get("total", 0)) == total and data.get("segments"):
                return data["segments"]
        except Exception:
            pass
    # Otherwise create the segments. Any existing contiguous .part
    # (from single-connection mode) becomes the 1st segment, already COMPLETE.
    existing = part_path.stat().st_size if part_path.exists() else 0
    existing = min(existing, total)
    segs = []
    if existing > 0:
        segs.append({"start": 0, "end": existing - 1, "cur": existing})
    if total > existing:
        seg_len = max(CHUNK, math.ceil((total - existing) / n))
        p = existing
        while p < total:
            e = min(p + seg_len, total) - 1
            segs.append({"start": p, "end": e, "cur": p})
            p = e + 1
    save_segments(progress_path, total, segs)
    return segs


def resplit_segments(segments: list, desired: int) -> list:
    """If the INCOMPLETE segments are fewer than 'desired', split the chunks with the
    most bytes remaining until there are 'desired' of them (or they can't be split
    anymore). Preserves progress: the already-downloaded part of each segment stays valid."""
    MIN_SPLIT = 8 * CHUNK  # don't split small pieces (pointless overhead)
    segs = [dict(s) for s in segments]

    def incomplete():
        return [s for s in segs if s["cur"] <= s["end"]]

    while len(incomplete()) < desired:
        cand = max(incomplete(), key=lambda s: s["end"] - s["cur"] + 1, default=None)
        if cand is None:
            break
        remaining = cand["end"] - cand["cur"] + 1
        if remaining < MIN_SPLIT:
            break
        mid = cand["cur"] + remaining // 2
        segs.append({"start": mid, "end": cand["end"], "cur": mid})
        cand["end"] = mid - 1

    segs.sort(key=lambda s: s["start"])
    return segs


def segments_done(segments: list) -> bool:
    return all(s["cur"] > s["end"] for s in segments)


def done_bytes(segments: list) -> int:
    return sum(s["cur"] - s["start"] for s in segments)


def contiguous_done(segments: list) -> int:
    """Bytes downloaded CONTIGUOUSLY from the start (no gaps). Needed to safely
    convert a multi session into a single-connection one."""
    c = 0
    for s in sorted(segments, key=lambda x: x["start"]):
        if s["start"] != c:
            break               # there's a gap: stop at the contiguous prefix
        c = s["cur"]
        if s["cur"] <= s["end"]:
            break               # unfinished segment: the contiguous part ends here
    return c


def progress_done_bytes(progress_path: pathlib.Path) -> int:
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        return sum(s["cur"] - s["start"] for s in data["segments"])
    except Exception:
        return 0


def normalize_from_multi(part_path: pathlib.Path, progress_path: pathlib.Path) -> int:
    """If a multi session exists (.progress), shrink the pre-allocated .part to the
    real contiguous bytes, so single-connection mode resumes correctly."""
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        c = contiguous_done(data["segments"])
    except Exception:
        c = part_path.stat().st_size if part_path.exists() else 0
    if part_path.exists():
        with open(part_path, "r+b") as f:
            f.truncate(c)
    try:
        progress_path.unlink()
    except OSError:
        pass
    return c


def seg_worker(seg, real, headers, impersonate, fh, fh_lock, prog_lock,
               progress_path, total, segments, stop_event, result,
               draw_lock, draw_state, proxy_url=None) -> None:
    h = dict(headers)
    h["Accept-Encoding"] = "identity"
    req_kwargs = {}
    if proxy_url:
        req_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

    def maybe_draw():
        # Draw the aggregate bar at most ~4 times per second, from any worker:
        # being called after writing a block, it's guaranteed to run (the thread
        # holds the GIL at that moment).
        now = time.time()
        with draw_lock:
            if now - draw_state["last"] >= 0.25:
                draw_state["last"] = now
                done = done_bytes(segments)
                draw_bar(done, total, done - draw_state["start_done"],
                         draw_state["start_time"])

    try:
        while seg["cur"] <= seg["end"]:
            if stop_event.is_set():
                result["status"] = "aborted"
                return
            pos = seg["cur"]
            h["Range"] = f"bytes={pos}-{seg['end']}"
            resp = requests.get(real, headers=h, impersonate=impersonate,
                                stream=True, timeout=120, **req_kwargs)
            st = resp.status_code
            if st == 403:
                resp.close(); result["status"] = "forbidden"; result["msg"] = "403"; return
            if st == 200 and pos > seg["start"]:
                resp.close(); result["status"] = "norange"
                result["msg"] = "the server ignores Range (use -n 1)"; return
            if st not in (200, 206):
                body = resp.text[:120]; resp.close()
                result["status"] = "http"; result["msg"] = f"{st} {body!r}"; return
            ct = resp.headers.get("Content-Type", "").lower()
            if "text/html" in ct or "text/plain" in ct:
                resp.close(); result["status"] = "forbidden"; result["msg"] = "html"; return
            last_persist = pos
            for chunk in resp.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                if stop_event.is_set():
                    resp.close(); result["status"] = "aborted"; return
                writ = seg["cur"]
                if writ + len(chunk) - 1 > seg["end"]:
                    chunk = chunk[: seg["end"] - writ + 1]
                with fh_lock:
                    fh.seek(writ)
                    fh.write(chunk)
                seg["cur"] = writ + len(chunk)
                maybe_draw()
                if seg["cur"] - last_persist >= 16 * 1024 * 1024:
                    with prog_lock:
                        save_segments(progress_path, total, segments)
                    last_persist = seg["cur"]
                if seg["cur"] > seg["end"]:
                    break
            resp.close()
        result["status"] = "done"
    except Exception as e:
        result["status"] = "neterr"; result["msg"] = f"{type(e).__name__}: {e}"


def download_multi(part_path: pathlib.Path, out_path: pathlib.Path,
                   n: int, retry_wait: int) -> None:
    progress_path = _progress_path(part_path)
    fh_lock = threading.Lock()
    prog_lock = threading.Lock()
    imp_idx = 0
    real = None
    total = None
    segments = None
    prealloc = False

    print(f"=== Multi-connection mode: {n} parallel connections ===\n")

    while True:
        headers = read_headers()
        impersonate = IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]

        # --- resolve link + total size ---
        try:
            if real is None:
                landing = read_landing()
                real = resolve_direct_url(landing, headers, impersonate)
                print(f"direct link resolved: {real[:80]}...")
            if total is None:
                total = get_total_size(real, headers, impersonate)
                print(f"total size: {human(total)}")
        except KeyboardInterrupt:
            print("\nInterrupted. .part and .progress saved: re-run to resume.")
            return
        except RequestException as e:
            msg = str(e)
            print(f"\n[error] {msg}")
            if ("TOKEN EXPIRED" in msg or "HTML" in msg or "no direct link" in msg):
                wait_for_fresh_link(); real = None; continue
            real = None; imp_idx += 1
            time.sleep(min(retry_wait, 30)); continue
        except Exception as e:
            print(f"\n[network error] {type(e).__name__}: {e}")
            time.sleep(min(retry_wait, 30)); continue

        # --- init segments + file pre-allocation (only once) ---
        if segments is None:
            segments = load_or_init_segments(part_path, total, n, progress_path)
            # re-split if more connections than incomplete segments are needed
            desired = max(n, len(read_proxies()))
            before = len([s for s in segments if s["cur"] <= s["end"]])
            segments = resplit_segments(segments, desired)
            after = len([s for s in segments if s["cur"] <= s["end"]])
            if after != before:
                print(f"re-split: from {before} to {after} incomplete segments")
                with prog_lock:
                    save_segments(progress_path, total, segments)
            print(f"segments: {after} to download "
                  f"out of {len(segments)} total; already on disk {human(done_bytes(segments))}")
        if not prealloc:
            if not part_path.exists():
                part_path.touch()
            with open(part_path, "r+b") as _f:
                _f.truncate(total)   # extends while preserving data already present
            prealloc = True
        if segments_done(segments):
            break

        # --- download the incomplete segments in parallel ---
        fh = open(part_path, "r+b")
        stop_event = threading.Event()
        draw_lock = threading.Lock()
        start_time = time.time()
        start_done = done_bytes(segments)
        draw_state = {"last": 0.0, "start_time": start_time, "start_done": start_done}

        active = [s for s in segments if s["cur"] <= s["end"]]
        proxies_list = read_proxies()   # one line per connection (see proxies.txt)
        print(f"starting {len(active)} parallel connections"
              + (f" over {len(proxies_list)} proxies" if proxies_list else " (direct connection)") + "...")

        threads, results = [], []
        for i, seg in enumerate(active):
            proxy_url = proxies_list[i % len(proxies_list)] if proxies_list else None
            print(f"  segment @{human(seg['start'])}  ->  "
                  f"{'direct connection' if not proxy_url else proxy_url}")
            res = {"status": "?", "msg": ""}
            results.append(res)
            t = threading.Thread(
                target=seg_worker,
                args=(seg, real, headers, impersonate, fh, fh_lock, prog_lock,
                      progress_path, total, segments, stop_event, res,
                      draw_lock, draw_state, proxy_url),
                daemon=True)
            t.start()
            threads.append(t)

        draw_bar(start_done, total, 0, start_time)  # initial bar right away

        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            stop_event.set()
            with prog_lock:
                save_segments(progress_path, total, segments)
            fh.close()
            print("\nInterrupted: progress saved, re-run to resume.")
            return

        fh.close()
        with prog_lock:
            save_segments(progress_path, total, segments)
        draw_bar(done_bytes(segments), total, done_bytes(segments) - start_done, start_time)
        print()

        if segments_done(segments):
            break

        bad = [r for r in results if r["status"] in ("forbidden", "norange", "http")]
        if bad:
            print(f"-> link/fingerprint no longer valid ({bad[0]['status']}: "
                  f"{bad[0]['msg']}). Re-resolving.")
            real = None
            imp_idx += 1
        else:
            wait = min(retry_wait, 30)
            print(f"-> some segments interrupted (network). Resuming in {wait}s.")
            time.sleep(wait)

    # --- finalization ---
    with open(part_path, "r+b") as f:
        f.truncate(total)
    try:
        progress_path.unlink()
    except OSError:
        pass
    part_path.replace(out_path)
    print(f"\nDownload COMPLETE: {out_path.name} ({human(out_path.stat().st_size)})")


# --------------------------------------------------------------------------- #
# Main loop with AUTO-RETRY and FALLBACK
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="download.bin", help="output file name")
    ap.add_argument("--sha256", default="", help="expected hash for final verification")
    ap.add_argument("--max-retries", type=int, default=0,
                    help="maximum number of attempts (0 = infinite)")
    ap.add_argument("--retry-wait", type=int, default=10,
                    help="base wait between attempts (seconds)")
    ap.add_argument("-n", "--connections", type=int, default=1,
                    help="parallel connections (1 = single)")
    args = ap.parse_args()

    out_path = HERE / args.out
    part_path = HERE / (args.out + ".part")
    progress_path = _progress_path(part_path)

    if out_path.exists():
        print(f"'{args.out}' already exists. Nothing to do.")
        return 0

    # bytes actually downloaded (from .progress if multi, otherwise .part size)
    if progress_path.exists():
        resume_bytes = progress_done_bytes(progress_path)
    elif part_path.exists():
        resume_bytes = part_path.stat().st_size
    else:
        resume_bytes = 0

    print("=== Download manager (curl_cffi) ===")
    print(f"Output : {out_path}")
    print(f"Partial: {part_path.name}"
          + (f"  (already {human(resume_bytes)} downloaded, resuming)"
             if resume_bytes else ""))
    print()

    # --- MULTI-CONNECTION MODE ---
    if args.connections and args.connections > 1:
        download_multi(part_path, out_path, args.connections, args.retry_wait)
        return _verify_hash(out_path, args.sha256)

    # --- SINGLE MODE: if coming from a multi session, normalize the .part ---
    if progress_path.exists():
        c = normalize_from_multi(part_path, progress_path)
        print(f"(previous multi session detected: .part normalized to "
              f"{human(c)}, resuming with 1 connection)\n")

    attempt = 0
    imp_idx = 0
    real = None          # last resolved direct link: reused for resume
    consec_fail = 0      # consecutive failures (for backoff)

    while True:
        attempt += 1
        if args.max_retries and attempt > args.max_retries:
            print(f"\nReached the limit of {args.max_retries} attempts. Stopping.")
            return 1

        headers = read_headers()  # re-read every time: fresh cookie if you update headers.txt
        impersonate = IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]
        print(f"----- Attempt #{attempt}  (fingerprint: {impersonate}) -----")

        try:
            if real is None:
                landing = read_landing()       # re-read every time: fresh link/token
                real = resolve_direct_url(landing, headers, impersonate)
                print(f"direct link resolved: {real[:80]}...")
            if attempt_download(real, headers, impersonate, part_path):
                break
            consec_fail = 0
        except KeyboardInterrupt:
            print("\nInterrupted by user. The .part stays on disk: re-run to resume.")
            return 130
        except RequestException as e:
            msg = str(e)
            print(f"\n[error] {msg}")
            # Dead link/token: auto-retry alone is NOT enough -> wait for a fresh link.
            if ("TOKEN EXPIRED" in msg or "HTML" in msg
                    or "no direct link" in msg):
                wait_for_fresh_link()
                real = None                    # force re-resolution with the new link
                consec_fail = 0
                continue                       # no extra wait: resume right away
            # 403 or TLS: the link may have expired or the fingerprint was rejected.
            real = None                        # discard the old link, re-resolve
            if "403" in msg or "TLS" in msg or "SSL" in msg or "curl: (35)" in msg:
                imp_idx += 1
                print(f"-> rotating TLS fingerprint to: {IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]}")
            consec_fail += 1
        except Exception as e:  # network/timeout/reset: resume on the SAME link
            print(f"\n[network error] {type(e).__name__}: {e}")
            consec_fail += 1
            # 'real' stays set: the direct link usually still supports the resume.
            # If it returns 403 next round, it'll be reset and re-resolved automatically.

        wait = min(args.retry_wait * consec_fail, 120)
        print(f"Retrying in {wait}s (the bytes already downloaded are NOT lost).\n")
        time.sleep(wait)

    # ---- Finalization: rename .part -> final file ----
    part_path.replace(out_path)
    size = out_path.stat().st_size
    print(f"\nDownload COMPLETE: {out_path.name} ({human(size)})")

    return _verify_hash(out_path, args.sha256)


def _verify_hash(out_path: pathlib.Path, expected: str) -> int:
    """Optional SHA256 verification. Returns the program's exit code."""
    if not out_path.exists():
        return 0  # download not completed (interrupted or waiting): nothing to verify
    if not expected:
        return 0
    print("Computing SHA256 (may take a few minutes on large files)...")
    h = hashlib.sha256()
    with open(out_path, "rb") as f:
        for blk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(blk)
    digest = h.hexdigest()
    print(f"Computed: {digest}")
    print(f"Expected: {expected.strip()}")
    if digest.lower() == expected.strip().lower():
        print("HASH OK: file intact.")
        return 0
    print("HASH MISMATCH: file corrupted or incomplete!")
    return 1


if __name__ == "__main__":
    sys.exit(main())
