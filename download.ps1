<#
  Download manager alternativo basato su aria2c.
  - Resume automatico (riprende dai byte gia' scaricati)
  - Retry infiniti + riavvio in caso di crash/reset connessione
  - Legge il link da url.txt ad ogni tentativo (comodo se il token scade)
  - Legge header/cookie da headers.txt
  - Verifica hash finale

  NOTA: aria2c non emula l'impronta TLS di un browser. Se il server richiede una
  fingerprint specifica (es. protezione anti-bot), usa invece download.py.

  USO:
    .\download.ps1 -OutFile "nomefile.ext" -ExpectedHash "<hash>" -Algo SHA256
#>

param(
    [string]$OutFile      = "download.bin",
    [string]$ExpectedHash = "",
    [ValidateSet("SHA256","SHA1","MD5","SHA512")]
    [string]$Algo         = "SHA256",
    [int]$Connections     = 1,      # alza solo se il server non blocca il multi-connessione
    [int]$RetryWaitSec    = 15      # attesa tra un riavvio e l'altro
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$urlFile    = Join-Path $here "url.txt"
$headerFile = Join-Path $here "headers.txt"

function Get-DownloadUrl {
    $u = (Get-Content $urlFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($u) -or $u -like "*PASTE*") {
        throw "Inserisci il link di download reale dentro url.txt"
    }
    return $u
}

# Gli header vanno in un file di input aria2c: passati da riga di comando,
# Windows PowerShell 5.1 spezza quelli con virgolette (es. sec-ch-ua).
function Write-AriaInput([string]$url, [string]$path) {
    $lines = @($url)
    if (Test-Path $headerFile) {
        foreach ($line in Get-Content $headerFile) {
            $t = $line.Trim()
            if ($t -eq "" -or $t.StartsWith("#")) { continue }
            $lines += "  header=$t"
        }
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($path, $lines, $utf8NoBom)
}

Write-Host "=== Download manager (aria2c) ===" -ForegroundColor Cyan
Write-Host "File di output : $OutFile"
Write-Host "Cartella       : $here"
if ($ExpectedHash) { Write-Host "Hash atteso    : $Algo = $ExpectedHash" }
Write-Host ""

$attempt = 0
while ($true) {
    $attempt++
    $url = Get-DownloadUrl
    $inputFile = Join-Path $here "aria-input.txt"
    Write-AriaInput $url $inputFile

    Write-Host "----- Tentativo #$attempt -----" -ForegroundColor Yellow

    $aria = @(
        "-c",                             # continua / resume
        "--max-tries=0",                  # retry infiniti interni
        "--retry-wait=10",
        "--timeout=60",
        "--connect-timeout=30",
        "--max-connection-per-server=$Connections",
        "--split=$Connections",
        "--min-split-size=20M",
        "--lowest-speed-limit=1K",        # se la velocita' crolla -> riconnette (gestisce i reset silenziosi)
        "--file-allocation=none",         # non pre-alloca tutto il file: parte subito
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--summary-interval=5",
        "--console-log-level=warn",
        "--dir=$here",
        "--out=$OutFile",
        "--input-file=$inputFile"
    )

    & aria2c @aria
    $code = $LASTEXITCODE

    $full = Join-Path $here $OutFile
    if ($code -eq 0 -and (Test-Path $full)) {
        Write-Host "`nDownload completato da aria2c." -ForegroundColor Green
        break
    }

    Write-Host "`naria2c e' uscito con codice $code. Riavvio tra $RetryWaitSec secondi..." -ForegroundColor Red
    Write-Host "(Se il token e' scaduto: aggiorna url.txt col nuovo link diretto, i byte gia' scaricati NON si perdono.)" -ForegroundColor DarkYellow
    Start-Sleep -Seconds $RetryWaitSec
}

# ---- Verifica hash ----
if ($ExpectedHash) {
    Write-Host "`nCalcolo $Algo del file (puo' richiedere qualche minuto su file grandi)..." -ForegroundColor Cyan
    $h = (Get-FileHash -Path $full -Algorithm $Algo).Hash
    Write-Host "Calcolato : $h"
    Write-Host "Atteso    : $ExpectedHash"
    if ($h -ieq $ExpectedHash.Trim()) {
        Write-Host "`nHASH OK: il file e' integro." -ForegroundColor Green
    } else {
        Write-Host "`nHASH DIVERSO: il file e' corrotto o incompleto!" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "`nNessun hash fornito: salto la verifica." -ForegroundColor DarkYellow
}
