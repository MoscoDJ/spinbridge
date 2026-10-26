# Compila SpinBridge: engine (PyInstaller) + app (Electron) -> dist\SpinBridge Setup <versión>.exe
# Requisitos en la máquina de compilación: Python 3.13 con engine\requirements.txt + pyinstaller, Node.js 20+.
# 'Continue': PyInstaller y npm escriben su progreso en stderr; los errores se detectan con $LASTEXITCODE
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
Set-Location $root

Write-Host '== Engine (PyInstaller)' -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean --onedir --name spinbridge-engine `
  --distpath engine-dist --workpath build\pyi --specpath build `
  --paths engine --collect-all SpoutGL --collect-submodules OpenGL --hidden-import psutil `
  --icon "$root\app\assets\icon.ico" engine\server.py
if ($LASTEXITCODE -ne 0) { throw 'Falló PyInstaller' }

Write-Host '== App (electron-builder)' -ForegroundColor Cyan
Set-Location "$root\app"
if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
npx electron-builder --win nsis
if ($LASTEXITCODE -ne 0) { throw 'Falló electron-builder' }

Set-Location $root
Get-ChildItem dist\*.exe | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB)}}
