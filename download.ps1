<#
  Alternative download manager based on aria2c.
  - Automatic resume (continues from the bytes already downloaded)
  - Infinite retries + restart on crash/connection reset
  - Reads the link from url.txt on every attempt (handy when the token expires)
  - Reads headers/cookie from headers.txt
  - Final hash verification

  NOTE: aria2c does not emulate a browser's TLS fingerprint. If the server requires
  a specific fingerprint (e.g. anti-bot protection), use download.py instead.

  USAGE:
    .\download.ps1 -OutFile "filename.ext" -ExpectedHash "<hash>" -Algo SHA256
#>

param(
    [string]$OutFile      = "download.bin",
    [string]$ExpectedHash = "",
    [ValidateSet("SHA256","SHA1","MD5","SHA512")]
    [string]$Algo         = "SHA256",
    [int]$Connections     = 1,      # raise only if the server does not block multi-connection
    [int]$RetryWaitSec    = 15      # wait between one restart and the next
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$urlFile    = Join-Path $here "url.txt"
$headerFile = Join-Path $here "headers.txt"

function Get-DownloadUrl {
    $u = (Get-Content $urlFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($u) -or $u -like "*PASTE*") {
        throw "Paste the real download link into url.txt"
    }
    return $u
}

# Headers go into an aria2c input file: passed on the command line,
# Windows PowerShell 5.1 breaks the ones with quotes (e.g. sec-ch-ua).
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
Write-Host "Output file : $OutFile"
Write-Host "Folder      : $here"
if ($ExpectedHash) { Write-Host "Expected hash : $Algo = $ExpectedHash" }
Write-Host ""

$attempt = 0
while ($true) {
    $attempt++
    $url = Get-DownloadUrl
    $inputFile = Join-Path $here "aria-input.txt"
    Write-AriaInput $url $inputFile

    Write-Host "----- Attempt #$attempt -----" -ForegroundColor Yellow

    $aria = @(
        "-c",                             # continue / resume
        "--max-tries=0",                  # infinite internal retries
        "--retry-wait=10",
        "--timeout=60",
        "--connect-timeout=30",
        "--max-connection-per-server=$Connections",
        "--split=$Connections",
        "--min-split-size=20M",
        "--lowest-speed-limit=1K",        # if speed collapses -> reconnect (handles silent resets)
        "--file-allocation=none",         # don't pre-allocate the whole file: start right away
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
        Write-Host "`nDownload completed by aria2c." -ForegroundColor Green
        break
    }

    Write-Host "`naria2c exited with code $code. Restarting in $RetryWaitSec seconds..." -ForegroundColor Red
    Write-Host "(If the token expired: update url.txt with the new direct link; the bytes already downloaded are NOT lost.)" -ForegroundColor DarkYellow
    Start-Sleep -Seconds $RetryWaitSec
}

# ---- Hash verification ----
if ($ExpectedHash) {
    Write-Host "`nComputing $Algo of the file (may take a few minutes on large files)..." -ForegroundColor Cyan
    $h = (Get-FileHash -Path $full -Algorithm $Algo).Hash
    Write-Host "Computed : $h"
    Write-Host "Expected : $ExpectedHash"
    if ($h -ieq $ExpectedHash.Trim()) {
        Write-Host "`nHASH OK: the file is intact." -ForegroundColor Green
    } else {
        Write-Host "`nHASH MISMATCH: the file is corrupted or incomplete!" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "`nNo hash provided: skipping verification." -ForegroundColor DarkYellow
}
