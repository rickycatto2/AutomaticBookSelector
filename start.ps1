$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.env')) { throw 'Copy .env.example to .env and set your ABS server address and token first.' }
New-Item -ItemType Directory -Force -Path 'imports' | Out-Null
$seedCsv = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.csv' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($seedCsv) { Copy-Item -LiteralPath $seedCsv.FullName -Destination (Join-Path $PSScriptRoot 'imports') }
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Docker could not start the app. Make sure Docker Desktop is running.' }
Write-Output 'Open http://localhost:5077'
