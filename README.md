# buzzheavier-parallel-downloader

Downloader **parallelo e resiliente per Buzzheavier** e i suoi mirror
(`bzzhr.to`, `bzzhr.co`, `buzzheavier.com`, `fuckingfast.net`, ...): download
multi-connessione con **resume**, auto-retry e aggiramento del **throttling per-IP**.

Pensato per i file host dietro Cloudflare che usano **token a scadenza**,
**controllo dell'impronta TLS** e **limite di velocità per-IP**. A differenza dei
downloader classici, il problema qui non è "scaricare un file" ma *riuscirci*:
superare il controllo anti-bot, riprendere dopo i reset, e battere il limite che il
server impone a ogni singolo IP.

## Caratteristiche

- **Impronta TLS di un browser** (via [`curl_cffi`](https://github.com/lexiforest/curl_cffi)): supera i controlli anti-bot che bloccano i client "non-browser".
- **Resume persistente**: scrive su `<file>.part` e, se interrotto, riparte dal byte esatto.
- **Auto-retry**: sui cali di rete riprende da solo; quando il link scade, si mette in pausa e riparte appena ne fornisci uno fresco (senza perdere i byte già scaricati).
- **Multi-connessione** (`-n`): scarica più segmenti del file in parallelo.
- **Multi-IP opzionale**: facendo uscire ogni connessione da un IP diverso (lista di proxy in `proxies.txt`) si può aggirare il throttling che molti host applicano *per-IP*.
- **Verifica integrità**: controllo opzionale dello SHA-256 a fine download.

## Requisiti

- Python 3.9+
- `pip install curl_cffi`
- (opzionale) [`aria2c`](https://aria2.github.io/) se vuoi usare la variante `download.ps1`

## Configurazione

Il programma legge tre file (template inclusi come `*.example.txt`):

```bash
copy url.example.txt url.txt
copy headers.example.txt headers.txt
copy proxies.example.txt proxies.txt   # solo per la modalità multi-IP
```

### Come ottenere il link e gli header (cURL)

1. Apri il sito nel browser e vai fino alla pagina di download del file.
2. Apri gli strumenti per sviluppatori (**F12**) → scheda **Network**.
3. Avvia il download (o clicca il pulsante di download).
4. Nella lista delle richieste trovane quella del download, **tasto destro → Copy → Copy as cURL**.

Dal comando `curl` copiato, riempi i file così:

- **`url.txt`** → l'URL del comando (quello dopo `--url '...'` o subito dopo `curl '...'`), sulla **prima riga**.
- **`headers.txt`** → ogni header `-H 'Nome: Valore'` diventa una riga `Nome: Valore`.
- Il **cookie** (`-b '...'` oppure `-H 'cookie: ...'`) va messo come riga `Cookie: ...` dentro `headers.txt`.

> **I token scadono in fretta.** Il link di download contiene un token temporaneo e il
> cookie di sessione (es. `cf_clearance`) è legato a IP + User-Agent. Se vedi un errore
> `TOKEN SCADUTO` o `403`, ricattura il cURL dal browser e reincolla URL + Cookie.

## Uso

Verifica rapida che impronta e header siano accettati (scarica i primi 64 KB):

```bash
python probe.py
```

Download a connessione singola:

```bash
python download.py -o "NomeFile.ext"
```

Con verifica dell'hash (se il sito lo fornisce):

```bash
python download.py -o "NomeFile.ext" --sha256 <hash>
```

Il download riprende sempre da dove si era interrotto: puoi chiudere e rilanciare lo
stesso comando quando vuoi. Se durante un'assenza scade il link, il programma resta in
attesa; appena aggiorni `url.txt` con un link fresco, riprende da solo.

### Modalità multi-connessione / multi-IP

```bash
python download.py -o "NomeFile.ext" -n 4
```

Con `-n` il file viene diviso in segmenti scaricati in parallelo. Da solo, questo **non**
aiuta se il server limita la velocità *per-IP* (le connessioni condividono lo stesso IP).
Per superare quel limite serve far uscire ogni connessione da un IP diverso, elencando
dei proxy in `proxies.txt` (una riga per connessione):

```
direct
socks5h://proxy-a.example.net:1080
socks5h://proxy-b.example.net:1080
```

Prima di impostare il multi-IP, conviene verificare se l'host accetta il download da un
IP diverso (alcuni legano il link all'IP che lo ha generato):

```bash
python proxytest.py socks5h://proxy-a.example.net:1080
```

Se il verdetto è "FUNZIONA da IP diverso", il multi-IP è utile.

### Nota sui servizi VPN/proxy

Questo progetto è stato sviluppato e testato usando **Mullvad** e i suoi proxy SOCKS5
multihop (ogni relay = un IP di uscita diverso). Il codice però accetta **qualsiasi**
proxy SOCKS5 o HTTP: basta inserirlo in `proxies.txt`.

Se usi un altro servizio VPN/proxy e ti serve un'integrazione specifica (o qualunque
altra funzione), apri una *issue*: sono disponibile ad aggiornare il progetto. E
ovviamente **ogni contributo è benvenuto** — vedi la sezione [Contribuire](#contribuire).

## Risoluzione problemi

| Messaggio | Significato | Cosa fare |
|---|---|---|
| `TOKEN SCADUTO` / `Risposta HTML` | Il link/token è scaduto | Ricattura il cURL, aggiorna `url.txt` e il `Cookie` in `headers.txt` |
| `403 Forbidden` | Impronta o cookie rifiutati | Aggiorna il Cookie; il programma ruota anche automaticamente l'impronta TLS |
| `norange ... usa -n 1` | Il server rifiuta le richieste parziali multiple | Usa `-n 1` (il `.part` resta valido) |
| Velocità ferma anche con più connessioni | Throttling per-IP o limite della tua linea | Usa `proxies.txt` con IP diversi, o accetta il limite della linea |

## Struttura dei file

- `download.py` — il downloader principale (single + multi-connessione, resume, retry).
- `probe.py` — diagnostica rapida dell'accesso al file.
- `proxytest.py` — verifica se il download funziona da un IP diverso.
- `download.ps1` — variante basata su `aria2c` (NB: non emula l'impronta TLS).
- `*.example.txt` — template di configurazione da copiare.

## Nota sullo sviluppo

Parte di questo progetto (codice e documentazione) è stata realizzata con l'assistenza
di strumenti di intelligenza artificiale, con revisione e test manuali.

## Contribuire

Chiunque può proporre modifiche o segnalazioni, anche senza permessi speciali:

- **Issue** — per segnalare un bug, chiedere una funzione o fare una domanda. È un
  messaggio pubblico sulla pagina del progetto; non modifica il codice.
- **Pull Request (PR)** — per proporre una modifica concreta al codice. Chi contribuisce
  fa una propria copia del progetto (*fork*), applica le modifiche e apre una PR: io la
  rivedo e, se va bene, la unisco (*merge*). Nulla viene cambiato finché non approvo.
- **Fork** — una copia personale e indipendente del progetto, che ognuno può modificare
  liberamente per conto proprio.

Se vuoi una funzione o un adattamento (altro servizio proxy, altro host, ecc.), apri pure
una issue descrivendo cosa ti serve.

## Avvertenze

Strumento fornito "così com'è", per uso personale e lecito. Scaricare contenuti protetti
da copyright senza diritto può violare la legge; aggirare limiti o protezioni può violare
i termini di servizio del sito e del tuo provider VPN/proxy. L'uso è responsabilità di chi
lo utilizza.
