<#
.SYNOPSIS
    Compila AudioAula (Extractor y Editor de Audio) en un ejecutable único (.exe) para Windows.
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "Verificando PyInstaller..." -ForegroundColor Cyan
$pyinstallerOk = $true
try {
    python -m PyInstaller --version | Out-Null
} catch {
    $pyinstallerOk = $false
}

if (-not $pyinstallerOk) {
    Write-Host "Instalando PyInstaller (herramienta libre y gratuita)..." -ForegroundColor Yellow
    pip install pyinstaller
}

Write-Host "Compilando AudioAula.exe..." -ForegroundColor Cyan
$pyiArgs = @("--noconfirm", "--clean", "--onefile", "--windowed", "--name", "AudioAula")
if (Test-Path "assets\AudioAula.ico") {
    $pyiArgs += @("--icon", "assets\AudioAula.ico")
}
if (Test-Path "assets") {
    $pyiArgs += @("--add-data", "assets;assets")
}
$pyiArgs += "app.py"
python -m PyInstaller @pyiArgs

$distExe = Join-Path $PSScriptRoot "dist\AudioAula.exe"
$rootExe = Join-Path $PSScriptRoot "AudioAula.exe"

if (Test-Path $distExe) {
    Copy-Item -Path $distExe -Destination $rootExe -Force
    Write-Host "Compilación exitosa. Ejecutable generado en: $rootExe" -ForegroundColor Green
} else {
    throw "No se encontró el archivo generado en $distExe."
}
