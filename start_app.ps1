$envFile = "C:\Users\david\AppData\Local\Temp\opencode\kpi-push\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}
$env:MYSQL_DB = 'proyecto_multiservicios_richard'
$env:MYSQL_DB_ALMACEN = 'proyecto_gestion_almacen'
$env:PORT = '5000'

Start-Process python -ArgumentList 'app.py' -WorkingDirectory 'C:\Users\david\AppData\Local\Temp\opencode\kpi-push' -RedirectStandardOutput run.out -RedirectStandardError run.err

Start-Sleep 5
if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host 'UP'
} else {
    Write-Host 'DOWN'
    Get-Content run.err -ErrorAction SilentlyContinue | Select-Object -Last 20
}