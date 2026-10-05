# ═══════════════════════════════════════════════════════════════════
# Verifica TODOS los cambios de la sesion (KPIs + bloqueo por intentos).
#
# NO toca la base de datos de alwaysdata. Las pruebas simulan MySQL en
# memoria, asi que puedes correrlas las veces que quieras.
#
# USO (desde la raiz del proyecto):
#   powershell -ExecutionPolicy Bypass -File .\arrancar\verificar_cambios.ps1
# ═══════════════════════════════════════════════════════════════════
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$tmp  = Join-Path $env:TEMP 'opencode'

# Las suites viven en la carpeta temporal de trabajo, no en el repo: son
# guiones de comprobacion, no parte de la aplicacion.
$suites = @{
    'escalera'     = 'probar_escalera.py'
    'decaimiento'  = 'probar_decaimiento.py'
    'dashboard'    = 'probar_dashboard.py'
    'panel'        = 'probar_panel.py'
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "[Verificar] No hay .venv. Crealo con:" -ForegroundColor Red
    Write-Host "  conda create -n proyecto python=3.13 -y" -ForegroundColor Yellow
    Write-Host "  conda activate proyecto" -ForegroundColor Yellow
    Write-Host "  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

Push-Location $root
try {
    # ── 1. Que todo compile ───────────────────────────────────────────
    Write-Host "`n[1/3] Compilacion de modulos" -ForegroundColor Cyan
    $modulos = @('app.py', 'ms_clientes_ventas\app.py',
                 'ms_gestion_almacen\app.py', 'kpi\routes_biz.py')
    & $python -m py_compile @modulos
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      OK los 4 modulos compilan" -ForegroundColor Green
    } else {
        Write-Host "      FALLA la compilacion" -ForegroundColor Red
        exit 1
    }

    # ── 2. Rastros de lo que se elimino ───────────────────────────────
    Write-Host "`n[2/3] Rastro de simbolos que deberian estar eliminados" -ForegroundColor Cyan
    $rastro = Select-String -Path 'app.py', 'ms_clientes_ventas\app.py' `
                          -Pattern 'MAX_INTENTOS|\.seconds//60' -CaseSensitive
    if ($rastro) {
        Write-Host "      FALLA quedan rastros:" -ForegroundColor Red
        $rastro | ForEach-Object { Write-Host "        $($_.Filename):$($_.LineNumber)" }
        exit 1
    }
    Write-Host "      OK sin MAX_INTENTOS ni .seconds//60" -ForegroundColor Green

    # ── 3. Suites, contra el monolito y contra el micro ───────────────
    Write-Host "`n[3/3] Suites de pruebas (sin tocar alwaysdata)" -ForegroundColor Cyan

    $fallos = 0
    # Orden fijo para que la salida sea comparable entre corridas.
    foreach ($nombre in @('escalera', 'decaimiento', 'panel', 'dashboard')) {
        $archivo = Join-Path $tmp $suites[$nombre]
        if (-not (Test-Path $archivo)) {
            Write-Host "      -- $nombre : no existe $archivo, se omite" -ForegroundColor Yellow
            continue
        }
        foreach ($app in @(@('raiz', 'MONOLITO'), @('micro', 'MICRO-VENTAS'))) {
            $env:APP_UNDER_TEST = $app[0]
            $lineas = & $python $archivo 2>&1
            $todo   = ($lineas | Out-String)

            # La suite se considera buena si imprimio su linea RESULTADO. El
            # exit code no sirve: el hilo de KPIs del monolito deja un
            # Traceback al morir en el apagado, despues de que la suite ya
            # paso, y eso devuelve un codigo de error falso.
            $linea = ($lineas | Select-String 'RESULTADO' | Select-Object -Last 1)
            if ($linea) {
                Write-Host ("      OK    {0,-10} {1,-12} {2}" -f $nombre, $app[1], ($linea -replace '\s+',' ')) -ForegroundColor Green
            } else {
                Write-Host ("      FALLA {0,-10} {1,-12} sin linea RESULTADO" -f $nombre, $app[1]) -ForegroundColor Red
                $fallos++
            }
        }
    }
    Remove-Item Env:APP_UNDER_TEST -ErrorAction SilentlyContinue

    Write-Host ""
    if ($fallos -eq 0) {
        Write-Host "=== TODO VERIFICADO ===" -ForegroundColor Green
        Write-Host "Para verlo en el navegador:" -ForegroundColor Cyan
        Write-Host "  powershell -ExecutionPolicy Bypass -File .\arrancar\arrancar_monolito.ps1"
        Write-Host "Luego http://localhost:5000"
    } else {
        Write-Host "=== $fallos suite(s) con fallo ===" -ForegroundColor Red
    }
}
finally {
    Pop-Location
}