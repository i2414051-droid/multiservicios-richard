# ═══════════════════════════════════════════════════════════════════
# Arranca el MONOLITO de la raiz (lo que Render despliega) - lee el .env
# USO: PowerShell  >  .\arrancar\arrancar_monolito.ps1
# ═══════════════════════════════════════════════════════════════════
# Por que existe: app.py NO lee el .env por si mismo. En Render funciona
# porque el panel inyecta las variables en el entorno del proceso, pero en
# local, sin esto, la app caeria en los defaults (localhost, root, sin
# password) y daria 500 en todas las paginas. Los otros dos scripts de esta
# carpeta ya hacen esta carga; aqui se hace para el monolito.
# ───────────────────────────────────────────────────────────────────
$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile = Join-Path $root '.env'

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}
else {
    Write-Host "[Monolito] No existe .env en la raiz. Copia .env.example y completalo." -ForegroundColor Yellow
}

# Respaldos: el monolito corre en 5000 por defecto, no en 5001 como el de almacen
if (-not $env:MYSQL_DB)         { $env:MYSQL_DB         = 'proyecto_multiservicios_richard' }
if (-not $env:MYSQL_DB_ALMACEN) { $env:MYSQL_DB_ALMACEN = 'proyecto_gestion_almacen' }
if (-not $env:PORT)             { $env:PORT             = '5000' }

# Aviso si la password quedo como placeholder: la conexion va a fallar y el
# error real se ve solo con DEBUG_ERRORS=1
if ($env:MYSQL_PASSWORD -like 'PEGAR_AQUI*') {
    Write-Host "[Monolito] MYSQL_PASSWORD sigue como placeholder en .env" -ForegroundColor Red
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
    Write-Host "[Monolito] No hay .venv; se usara el python del PATH" -ForegroundColor Yellow
}

Write-Host "[Monolito] Puerto: $env:PORT"
Write-Host "[Monolito] BD ventas:  $env:MYSQL_DB"
Write-Host "[Monolito] BD almacen: $env:MYSQL_DB_ALMACEN"
Write-Host "[Monolito] DEBUG_ERRORS: $env:DEBUG_ERRORS"
Write-Host "[Monolito] http://localhost:$env:PORT"

Push-Location $root
try {
    & $python app.py
}
finally {
    Pop-Location
}
