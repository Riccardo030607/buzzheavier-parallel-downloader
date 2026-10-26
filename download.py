#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download manager robusto basato su curl_cffi.

Caratteristiche:
  1) BARRA DI AVANZAMENTO  -> percentuale, byte scaricati/totali, velocita' ed ETA.
  2) PERSISTENZA           -> scrive su <OutFile>.part; se rilanci lo script
                              riprende dal punto esatto leggendo i byte gia' su disco.
  3) AUTO-RETRY + RESUME   -> se la connessione cade riparte da solo e continua
                              dal byte dove si era interrotto (Range: bytes=pos-).
  4) MULTI-CONNESSIONE     -> scarica segmenti in parallelo (-n), con una lista di
                              proxy opzionale (proxies.txt) per far uscire ogni
                              connessione da un IP diverso.
  5) FALLBACK              -> ri-risolve il link dalla configurazione, ruota piu'
                              impronte TLS se il server rifiuta, gestisce 200/206/416/403.

Configurazione (file nella stessa cartella):
  - url.txt      : il link di download (endpoint /download?... oppure link diretto).
  - headers.txt  : gli header catturati dal browser ("Nome: Valore"), incluso Cookie.
  - proxies.txt  : (opzionale) un proxy per riga per la modalita' multi-connessione.

USO:
    python download.py -o MioFile.ext
    python download.py -o MioFile.ext --sha256 <hash>
    python download.py -o MioFile.ext -n 4
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

# Scala di impronte TLS per il fallback: si prova la prima; se il server
# rifiuta il TLS/403, si passa alla successiva.
IMPERSONATE_CHAIN = ["chrome", "chrome124", "chrome120", "edge101"]

CHUNK = 1024 * 1024  # 1 MiB


# --------------------------------------------------------------------------- #
# Lettura configurazione
# --------------------------------------------------------------------------- #
def read_landing() -> str:
    txt = URL_FILE.read_text(encoding="utf-8").strip()
    if not txt or "PASTE" in txt.upper():
        raise SystemExit("ERRORE: inserisci il link di download nella prima riga di url.txt")
    return txt.splitlines()[0].strip()


def read_headers() -> dict:
    """Legge headers.txt come dizionario. Una riga per header ('Nome: Valore').
    Le righe vuote o che iniziano con '#' sono ignorate."""
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
    """Legge proxies.txt: una riga per connessione del download multiplo.
    'direct' = nessun proxy (rete/tunnel attuale). Altrimenti host:porta
    (SOCKS5) oppure un URL completo. Ritorna una lista di (None | 'socks5h://...')."""
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
    """Se il link/token e' scaduto, l'auto-retry da solo non basta. Aspetta che
    venga salvato un link fresco in url.txt e poi riprende. I byte gia' scaricati
    nel .part restano intatti."""
    try:
        base_mtime = URL_FILE.stat().st_mtime
    except OSError:
        base_mtime = 0
    print("\n" + "=" * 70)
    print(">>> LINK/TOKEN SCADUTO - serve un intervento manuale.")
    print(">>> 1) nel browser ricattura la richiesta di download (Copy as cURL)")
    print(">>> 2) incolla l'URL nella 1a riga di url.txt (e aggiorna il Cookie in headers.txt)")
    print(">>> 3) salva: il download riprende dal punto esatto, i byte del .part sono salvi.")
    print("=" * 70)
    waited = 0
    while True:
        time.sleep(poll)
        waited += poll
        try:
            if URL_FILE.stat().st_mtime != base_mtime:
                print(">>> url.txt aggiornato: riprendo il download.\n")
                return
        except OSError:
            pass
        if waited % remind_every == 0:
            print(f"    ...in attesa di un link fresco in url.txt ({waited}s)")


# --------------------------------------------------------------------------- #
# STEP 1 - risoluzione del link diretto (gestisce hx-redirect / 3xx Location)
# --------------------------------------------------------------------------- #
def resolve_direct_url(landing: str, headers: dict, impersonate: str) -> str:
    hdr = dict(headers)
    hdr.setdefault("Accept", "*/*")
    # stream=True: non scarica il body (se per sbaglio e' gia' il file, non va in RAM)
    r = requests.get(landing, headers=hdr, impersonate=impersonate,
                     allow_redirects=False, stream=True, timeout=30)
    real = r.headers.get("hx-redirect") or r.headers.get("location")

    landing_host = urlparse(landing).hostname or ""

    if real:
        r.close()
        real = urljoin(landing, real)          # normalizza eventuali relativi
        pr = urlparse(real)
        # Un link valido al file sta su un host diverso dalla pagina, o ha /d/ nel path
        looks_like_file = ("/d/" in pr.path) or (pr.hostname and pr.hostname != landing_host)
        if not looks_like_file:
            raise RequestException(
                "TOKEN SCADUTO: il server rimbalza alla pagina "
                f"({real}) invece di dare il link al file. Aggiorna url.txt "
                "con un link fresco dal browser (e il Cookie in headers.txt)."
            )
        return real

    # Nessun redirect: accetta il landing SOLO se e' davvero un file (non HTML).
    ctype = r.headers.get("Content-Type", "").lower()
    if r.headers.get("Content-Disposition") or ctype.startswith("application/"):
        r.close()
        return landing  # url.txt contiene gia' il link diretto al file
    body = r.text[:200]
    r.close()
    raise RequestException(
        f"Step1: nessun link diretto (status {r.status_code}, "
        f"Content-Type {ctype!r}). Link scaduto? Aggiorna url.txt. "
        f"Anteprima: {body!r}"
    )


# --------------------------------------------------------------------------- #
# Barra di avanzamento
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:6.2f} {unit}"
        n /= 1024
    return f"{n:6.2f} PB"


def draw_bar(done: int, total: int, session_bytes: int, session_start: float) -> None:
    elapsed = max(time.time() - session_start, 1e-6)
    speed = session_bytes / elapsed  # byte/s della sessione corrente
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
        msg = f"\r{human(done)} scaricati  {human(speed)}/s   "
    sys.stdout.write(msg)
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# STEP 2 - un singolo tentativo di download con resume (connessione singola)
# Ritorna True se il file e' completo.
# --------------------------------------------------------------------------- #
def attempt_download(real_url: str, headers: dict, impersonate: str,
                     part_path: pathlib.Path) -> bool:
    pos = part_path.stat().st_size if part_path.exists() else 0

    h2 = dict(headers)
    h2["Accept-Encoding"] = "identity"   # niente gzip: Content-Length = size reale
    if pos > 0:
        h2["Range"] = f"bytes={pos}-"

    resp = requests.get(real_url, headers=h2, impersonate=impersonate,
                        stream=True, timeout=120)
    status = resp.status_code

    # File gia' completo (il server dice: range non soddisfacibile)
    if status == 416:
        resp.close()
        print(f"\n[info] Il server risponde 416: il .part ({human(pos)}) risulta gia' completo.")
        return True

    if status == 403:
        resp.close()
        raise RequestException("403 Forbidden: impronta/cookie rifiutati (provo fallback).")

    if status not in (200, 206):
        body = resp.text[:200]
        resp.close()
        raise RequestException(f"status inatteso {status}. Body: {body!r}")

    # Se arriva una pagina HTML non e' il file -> non salvare contenuto errato.
    ctype = resp.headers.get("Content-Type", "").lower()
    if "text/html" in ctype or "text/plain" in ctype:
        body = resp.text[:200]
        resp.close()
        raise RequestException(
            f"Risposta HTML/testo (Content-Type {ctype!r}), NON il file. "
            f"Link scaduto? Aggiorna url.txt. Anteprima: {body!r}"
        )

    # Determina la dimensione totale
    total = 0
    cr = resp.headers.get("Content-Range")  # es: "bytes 1000-69999/70000"
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            total = 0
    elif resp.headers.get("Content-Length"):
        total = int(resp.headers["Content-Length"]) + (pos if status == 206 else 0)

    # Il server ha IGNORATO il Range (200 invece di 206): riparti da zero.
    if pos > 0 and status == 200:
        print(f"\n[warn] Il server ha ignorato il resume (200): riparto da 0.")
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
        raise RequestException(f"stream interrotto: {human(done)}/{human(total)}")
    return True


# --------------------------------------------------------------------------- #
# MODALITA' MULTI-CONNESSIONE (segmenti paralleli con range)
# --------------------------------------------------------------------------- #
def get_total_size(real: str, headers: dict, impersonate: str) -> int:
    """Chiede 1 byte (Range 0-0) per leggere la dimensione totale dal Content-Range."""
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
        raise RequestException("403/HTML sul sizing: link o impronta non validi.")
    if not total:
        raise RequestException(f"impossibile leggere la dimensione totale (status {st}).")
    return total


def _progress_path(part_path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(str(part_path) + ".progress")


def save_segments(progress_path: pathlib.Path, total: int, segments: list) -> None:
    tmp = pathlib.Path(str(progress_path) + ".tmp")
    tmp.write_text(json.dumps({"total": total, "segments": segments}), encoding="utf-8")
    tmp.replace(progress_path)


def load_or_init_segments(part_path: pathlib.Path, total: int, n: int,
                          progress_path: pathlib.Path) -> list:
    # Riprende lo stato salvato se coerente con la dimensione totale.
    if progress_path.exists():
        try:
            data = json.loads(progress_path.read_text(encoding="utf-8"))
            if int(data.get("total", 0)) == total and data.get("segments"):
                return data["segments"]
        except Exception:
            pass
    # Altrimenti crea i segmenti. Un eventuale .part contiguo gia' esistente
    # (dalla modalita' a 1 connessione) diventa il 1o segmento gia' COMPLETO.
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
    """Se i segmenti INCOMPIUTI sono meno di 'desired', spezza i tronconi con piu'
    byte residui finche' non se ne hanno 'desired' (o non sono piu' divisibili).
    Preserva il progresso: la parte gia' scaricata di ogni segmento resta valida."""
    MIN_SPLIT = 8 * CHUNK  # non spezzare pezzi piccoli (overhead inutile)
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
    """Byte scaricati in modo CONTIGUO dall'inizio (senza buchi). Serve per
    convertire in sicurezza una sessione multi in una a connessione singola."""
    c = 0
    for s in sorted(segments, key=lambda x: x["start"]):
        if s["start"] != c:
            break               # c'e' un buco: mi fermo al prefisso contiguo
        c = s["cur"]
        if s["cur"] <= s["end"]:
            break               # segmento non finito: il contiguo arriva fin qui
    return c


def progress_done_bytes(progress_path: pathlib.Path) -> int:
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        return sum(s["cur"] - s["start"] for s in data["segments"])
    except Exception:
        return 0


def normalize_from_multi(part_path: pathlib.Path, progress_path: pathlib.Path) -> int:
    """Se esiste una sessione multi (.progress), riduce il .part pre-allocato ai
    byte contigui reali, cosi' la modalita' a 1 connessione riprende corretta."""
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
        # Disegna la barra aggregata al massimo ~4 volte al secondo, da qualunque
        # worker: essendo chiamata dopo la scrittura di un blocco, viene eseguita
        # di sicuro (il thread tiene il GIL in quel momento).
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
                result["msg"] = "il server ignora Range (usa -n 1)"; return
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

    print(f"=== Modalita' multi-connessione: {n} connessioni parallele ===\n")

    while True:
        headers = read_headers()
        impersonate = IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]

        # --- risoluzione link + dimensione totale ---
        try:
            if real is None:
                landing = read_landing()
                real = resolve_direct_url(landing, headers, impersonate)
                print(f"link diretto risolto: {real[:80]}...")
            if total is None:
                total = get_total_size(real, headers, impersonate)
                print(f"dimensione totale: {human(total)}")
        except KeyboardInterrupt:
            print("\nInterrotto. .part e .progress salvi: rilancia per riprendere.")
            return
        except RequestException as e:
            msg = str(e)
            print(f"\n[errore] {msg}")
            if ("TOKEN SCADUTO" in msg or "HTML" in msg or "nessun link diretto" in msg):
                wait_for_fresh_link(); real = None; continue
            real = None; imp_idx += 1
            time.sleep(min(retry_wait, 30)); continue
        except Exception as e:
            print(f"\n[errore rete] {type(e).__name__}: {e}")
            time.sleep(min(retry_wait, 30)); continue

        # --- init segmenti + preallocazione del file (una volta sola) ---
        if segments is None:
            segments = load_or_init_segments(part_path, total, n, progress_path)
            # ri-suddivide se servono piu' connessioni dei segmenti incompiuti
            desired = max(n, len(read_proxies()))
            before = len([s for s in segments if s["cur"] <= s["end"]])
            segments = resplit_segments(segments, desired)
            after = len([s for s in segments if s["cur"] <= s["end"]])
            if after != before:
                print(f"ri-suddivisione: da {before} a {after} segmenti incompiuti")
                with prog_lock:
                    save_segments(progress_path, total, segments)
            print(f"segmenti: {after} da scaricare "
                  f"su {len(segments)} totali; gia' su disco {human(done_bytes(segments))}")
        if not prealloc:
            if not part_path.exists():
                part_path.touch()
            with open(part_path, "r+b") as _f:
                _f.truncate(total)   # estende preservando i dati gia' presenti
            prealloc = True
        if segments_done(segments):
            break

        # --- scarico i segmenti incompleti in parallelo ---
        fh = open(part_path, "r+b")
        stop_event = threading.Event()
        draw_lock = threading.Lock()
        start_time = time.time()
        start_done = done_bytes(segments)
        draw_state = {"last": 0.0, "start_time": start_time, "start_done": start_done}

        active = [s for s in segments if s["cur"] <= s["end"]]
        proxies_list = read_proxies()   # una riga per connessione (vedi proxies.txt)
        print(f"avvio {len(active)} connessioni parallele"
              + (f" su {len(proxies_list)} proxy" if proxies_list else " (connessione diretta)") + "...")

        threads, results = [], []
        for i, seg in enumerate(active):
            proxy_url = proxies_list[i % len(proxies_list)] if proxies_list else None
            print(f"  segmento @{human(seg['start'])}  ->  "
                  f"{'connessione diretta' if not proxy_url else proxy_url}")
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

        draw_bar(start_done, total, 0, start_time)  # barra iniziale subito

        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            stop_event.set()
            with prog_lock:
                save_segments(progress_path, total, segments)
            fh.close()
            print("\nInterrotto: progresso salvato, rilancia per riprendere.")
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
            print(f"-> link/impronta non piu' validi ({bad[0]['status']}: "
                  f"{bad[0]['msg']}). Ri-risolvo.")
            real = None
            imp_idx += 1
        else:
            wait = min(retry_wait, 30)
            print(f"-> alcuni segmenti interrotti (rete). Riprendo tra {wait}s.")
            time.sleep(wait)

    # --- finalizzazione ---
    with open(part_path, "r+b") as f:
        f.truncate(total)
    try:
        progress_path.unlink()
    except OSError:
        pass
    part_path.replace(out_path)
    print(f"\nDownload COMPLETATO: {out_path.name} ({human(out_path.stat().st_size)})")


# --------------------------------------------------------------------------- #
# Loop principale con AUTO-RETRY e FALLBACK
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="download.bin", help="nome file di output")
    ap.add_argument("--sha256", default="", help="hash atteso per verifica finale")
    ap.add_argument("--max-retries", type=int, default=0,
                    help="numero massimo di tentativi (0 = infiniti)")
    ap.add_argument("--retry-wait", type=int, default=10,
                    help="attesa base tra i tentativi (secondi)")
    ap.add_argument("-n", "--connections", type=int, default=1,
                    help="connessioni parallele (1 = singola)")
    args = ap.parse_args()

    out_path = HERE / args.out
    part_path = HERE / (args.out + ".part")
    progress_path = _progress_path(part_path)

    if out_path.exists():
        print(f"'{args.out}' esiste gia'. Nulla da fare.")
        return 0

    # byte realmente scaricati (dal .progress se multi, altrimenti dimensione .part)
    if progress_path.exists():
        resume_bytes = progress_done_bytes(progress_path)
    elif part_path.exists():
        resume_bytes = part_path.stat().st_size
    else:
        resume_bytes = 0

    print("=== Download manager (curl_cffi) ===")
    print(f"Output : {out_path}")
    print(f"Parziale: {part_path.name}"
          + (f"  (gia' {human(resume_bytes)} scaricati, riprendo)"
             if resume_bytes else ""))
    print()

    # --- MODALITA' MULTI-CONNESSIONE ---
    if args.connections and args.connections > 1:
        download_multi(part_path, out_path, args.connections, args.retry_wait)
        return _verify_hash(out_path, args.sha256)

    # --- MODALITA' SINGOLA: se vengo da una sessione multi, normalizzo il .part ---
    if progress_path.exists():
        c = normalize_from_multi(part_path, progress_path)
        print(f"(sessione multi precedente rilevata: .part normalizzato a "
              f"{human(c)}, riprendo a 1 connessione)\n")

    attempt = 0
    imp_idx = 0
    real = None          # ultimo link diretto risolto: riusato per il resume
    consec_fail = 0      # fallimenti consecutivi (per il backoff)

    while True:
        attempt += 1
        if args.max_retries and attempt > args.max_retries:
            print(f"\nRaggiunto il limite di {args.max_retries} tentativi. Mi fermo.")
            return 1

        headers = read_headers()  # riletto ogni volta: cookie fresco se aggiorni headers.txt
        impersonate = IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]
        print(f"----- Tentativo #{attempt}  (impronta: {impersonate}) -----")

        try:
            if real is None:
                landing = read_landing()       # riletto ogni volta: link/token fresco
                real = resolve_direct_url(landing, headers, impersonate)
                print(f"link diretto risolto: {real[:80]}...")
            if attempt_download(real, headers, impersonate, part_path):
                break
            consec_fail = 0
        except KeyboardInterrupt:
            print("\nInterrotto dall'utente. Il .part resta su disco: rilancia per riprendere.")
            return 130
        except RequestException as e:
            msg = str(e)
            print(f"\n[errore] {msg}")
            # Link/token morto: l'auto-retry da solo NON basta -> aspetto un link fresco.
            if ("TOKEN SCADUTO" in msg or "HTML" in msg
                    or "nessun link diretto" in msg):
                wait_for_fresh_link()
                real = None                    # forza ri-risoluzione col link nuovo
                consec_fail = 0
                continue                       # niente attesa extra: riparti subito
            # 403 o TLS: il link potrebbe essere scaduto o l'impronta rifiutata.
            real = None                        # scarta il link vecchio, ri-risolvo
            if "403" in msg or "TLS" in msg or "SSL" in msg or "curl: (35)" in msg:
                imp_idx += 1
                print(f"-> ruoto impronta TLS a: {IMPERSONATE_CHAIN[imp_idx % len(IMPERSONATE_CHAIN)]}")
            consec_fail += 1
        except Exception as e:  # rete/timeout/reset: riprendo sullo STESSO link
            print(f"\n[errore rete] {type(e).__name__}: {e}")
            consec_fail += 1
            # 'real' resta impostato: il link diretto di solito regge ancora il resume.
            # Se al giro dopo dara' 403, verra' azzerato e ri-risolto automaticamente.

        wait = min(args.retry_wait * consec_fail, 120)
        print(f"Riprovo tra {wait}s (i byte gia' scaricati NON si perdono).\n")
        time.sleep(wait)

    # ---- Finalizzazione: rinomino .part -> file definitivo ----
    part_path.replace(out_path)
    size = out_path.stat().st_size
    print(f"\nDownload COMPLETATO: {out_path.name} ({human(size)})")

    return _verify_hash(out_path, args.sha256)


def _verify_hash(out_path: pathlib.Path, expected: str) -> int:
    """Verifica SHA256 opzionale. Ritorna il codice di uscita del programma."""
    if not out_path.exists():
        return 0  # download non completato (interrotto o in attesa): niente da verificare
    if not expected:
        return 0
    print("Calcolo SHA256 (puo' richiedere qualche minuto su file grandi)...")
    h = hashlib.sha256()
    with open(out_path, "rb") as f:
        for blk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(blk)
    digest = h.hexdigest()
    print(f"Calcolato: {digest}")
    print(f"Atteso   : {expected.strip()}")
    if digest.lower() == expected.strip().lower():
        print("HASH OK: file integro.")
        return 0
    print("HASH DIVERSO: file corrotto o incompleto!")
    return 1


if __name__ == "__main__":
    sys.exit(main())
