# buzzheavier-parallel-downloader

**Parallel, resilient downloader for Buzzheavier** and its mirrors
(`bzzhr.to`, `bzzhr.co`, `buzzheavier.com`, `fuckingfast.net`, ...): multi-connection
downloads with **resume**, auto-retry and **per-IP throttling** bypass.

Built for file hosts behind Cloudflare that use **expiring tokens**,
**TLS fingerprint checks** and a **per-IP speed limit**. Unlike regular downloaders,
the hard part here isn't "downloading a file" but *managing to*: getting past the
anti-bot check, resuming after resets, and beating the limit the server enforces on
every single IP.

## Features

- **Browser TLS fingerprint** (via [`curl_cffi`](https://github.com/lexiforest/curl_cffi)): gets past anti-bot checks that block "non-browser" clients.
- **Persistent resume**: writes to `<file>.part` and, if interrupted, restarts from the exact byte.
- **Auto-retry**: recovers on its own after network drops; when the link expires it pauses and resumes as soon as you supply a fresh one (without losing the bytes already downloaded).
- **Multi-connection** (`-n`): downloads several segments of the file in parallel.
- **Optional multi-IP**: by routing each connection through a different IP (list of proxies in `proxies.txt`) you can bypass the throttling that many hosts apply *per-IP*.
- **Integrity check**: optional SHA-256 verification once the download finishes.

## Requirements

- Python 3.9+
- `pip install curl_cffi`
- (optional) [`aria2c`](https://aria2.github.io/) if you want to use the `download.ps1` variant

## Configuration

The program reads three files (templates are included as `*.example.txt`):

```bash
copy url.example.txt url.txt
copy headers.example.txt headers.txt
copy proxies.example.txt proxies.txt   # only for multi-IP mode
```

### How to get the link and headers (cURL)

1. Open the site in your browser and navigate to the file's download page.
2. Open the developer tools (**F12**) → **Network** tab.
3. Start the download (or click the download button).
4. In the request list, find the download request, **right-click → Copy → Copy as cURL**.

From the copied `curl` command, fill the files like this:

- **`url.txt`** → the command's URL (the one after `--url '...'` or right after `curl '...'`), on the **first line**.
- **`headers.txt`** → every `-H 'Name: Value'` header becomes a `Name: Value` line.
- The **cookie** (`-b '...'` or `-H 'cookie: ...'`) goes as a `Cookie: ...` line inside `headers.txt`.

> **Tokens expire fast.** The download link carries a temporary token and the session
> cookie (e.g. `cf_clearance`) is bound to IP + User-Agent. If you see a `TOKEN EXPIRED`
> or `403` error, recapture the cURL from the browser and paste the URL + Cookie again.

## Usage

Quick check that the fingerprint and headers are accepted (downloads the first 64 KB):

```bash
python probe.py
```

Single-connection download:

```bash
python download.py -o "FileName.ext"
```

With hash verification (if the site provides one):

```bash
python download.py -o "FileName.ext" --sha256 <hash>
```

The download always resumes from where it stopped: you can close and re-run the same
command whenever you want. If the link expires while you're away, the program waits; as
soon as you update `url.txt` with a fresh link, it resumes on its own.

### Multi-connection / multi-IP mode

```bash
python download.py -o "FileName.ext" -n 4
```

With `-n` the file is split into segments downloaded in parallel. On its own, this does
**not** help if the server limits speed *per-IP* (the connections share the same IP).
To beat that limit you need to route each connection through a different IP, by listing
proxies in `proxies.txt` (one line per connection):

```
direct
socks5h://proxy-a.example.net:1080
socks5h://proxy-b.example.net:1080
```

Before setting up multi-IP, it's worth checking whether the host accepts the download
from a different IP (some bind the link to the IP that generated it):

```bash
python proxytest.py socks5h://proxy-a.example.net:1080
```

If the verdict is "WORKS from a different IP", multi-IP is useful.

### A note on VPN/proxy services

This project was developed and tested using **Mullvad** and its multihop SOCKS5 proxies
(each relay = a different exit IP). The code, however, accepts **any** SOCKS5 or HTTP
proxy: just add it to `proxies.txt`.

If you use a different VPN/proxy service and need specific integration (or any other
feature), open an *issue*: I'm happy to update the project. And of course **any
contribution is welcome** — see the [Contributing](#contributing) section.

## Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `TOKEN EXPIRED` / `HTML response` | The link/token has expired | Recapture the cURL, update `url.txt` and the `Cookie` in `headers.txt` |
| `403 Forbidden` | Fingerprint or cookie rejected | Update the Cookie; the program also rotates the TLS fingerprint automatically |
| `norange ... use -n 1` | The server refuses multiple partial requests | Use `-n 1` (the `.part` stays valid) |
| Speed stuck even with more connections | Per-IP throttling or your line's limit | Use `proxies.txt` with different IPs, or accept the line limit |

## File layout

- `download.py` — the main downloader (single + multi-connection, resume, retry).
- `probe.py` — quick diagnostic of file access.
- `proxytest.py` — checks whether the download works from a different IP.
- `download.ps1` — `aria2c`-based variant (note: it does **not** emulate the TLS fingerprint).
- `*.example.txt` — configuration templates to copy.

## A note on development

Part of this project (code and documentation) was built with the help of AI tools, with
manual review and testing.

## Contributing

Anyone can propose changes or report issues, even without special permissions:

- **Issue** — to report a bug, request a feature or ask a question. It's a public message
  on the project page; it does not change the code.
- **Pull Request (PR)** — to propose a concrete change to the code. A contributor makes
  their own copy of the project (*fork*), applies the changes and opens a PR: I review it
  and, if it's good, I merge it. Nothing changes until I approve.
- **Fork** — a personal, independent copy of the project that anyone can modify freely on
  their own.

If you want a feature or an adaptation (another proxy service, another host, etc.), feel
free to open an issue describing what you need.

## Disclaimer

This tool is provided "as is", for personal and lawful use. Downloading copyrighted
content without the right to do so may break the law; bypassing limits or protections may
violate the terms of service of the site and of your VPN/proxy provider. Use is the
responsibility of whoever runs it.
