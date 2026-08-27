# Забирает свежий ночной снимок базы «Чека» с сервера на этот компьютер.
#
# Зачем: снимки на самом сервере защищают от порчи файла, но не от потери
# сервера целиком. Эта копия лежит off-site — здесь.
#
# Запуск вручную:  powershell -ExecutionPolicy Bypass -File pull_backup.ps1
# По расписанию:   см. раздел «Бэкап базы» в CLAUDE.md

$ErrorActionPreference = "Stop"

$Server = "root@72.56.79.34"
$Remote = "/opt/chek/backups/daily"
$Local  = "$env:USERPROFILE\Documents\chek-backups"
$Keep   = 30          # сколько копий держать на компьютере

New-Item -ItemType Directory -Force -Path $Local | Out-Null

# Сначала просим сервер сделать свежий снимок — чтобы забрать актуальные данные,
# а не вчерашние. Скрипт на сервере сам ротирует свои копии.
ssh -o BatchMode=yes $Server "/opt/chek/backup_db.sh"

$latest = (ssh -o BatchMode=yes $Server "ls -1t $Remote/food_diary_*.db.gz | head -1").Trim()
if (-not $latest) { throw "На сервере нет снимков в $Remote" }

$name = Split-Path $latest -Leaf
$dest = Join-Path $Local $name
scp -o BatchMode=yes "${Server}:$latest" $dest

$size = [math]::Round((Get-Item $dest).Length / 1KB, 1)
Write-Host "Скачано: $dest ($size КБ)"

# Ротация локальных копий
Get-ChildItem (Join-Path $Local "food_diary_*.db.gz") |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force

$count = (Get-ChildItem (Join-Path $Local "food_diary_*.db.gz")).Count
Write-Host "Всего копий на компьютере: $count"
