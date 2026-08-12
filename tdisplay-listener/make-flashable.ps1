# Merge the PlatformIO build into a single merged.bin flashable at offset 0
# (esptool or ESP Web Tools, chipFamily "ESP32-S3"). Run after `pio run`.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$build = ".pio\build\tdisplay-s3"
if (-not (Test-Path "$build\firmware.bin")) {
    Write-Error "No build found - run 'pio run' first."
}

# boot_app0.bin ships with the Arduino framework package
$bootApp0 = Get-ChildItem "$env:USERPROFILE\.platformio\packages\framework-arduinoespressif32*\tools\partitions\boot_app0.bin" | Select-Object -First 1
if (-not $bootApp0) { Write-Error "boot_app0.bin not found in PlatformIO packages." }

# esptool via PlatformIO's bundled python
$pioPython = "$env:USERPROFILE\.platformio\penv\Scripts\python.exe"

& $pioPython -m esptool --chip esp32s3 merge_bin -o merged.bin `
    --flash_mode qio --flash_freq 80m --flash_size 16MB `
    0x0 "$build\bootloader.bin" `
    0x8000 "$build\partitions.bin" `
    0xe000 $bootApp0.FullName `
    0x10000 "$build\firmware.bin"

Write-Host "merged.bin created. Flash with:"
Write-Host "  python -m esptool --chip esp32s3 write_flash 0x0 merged.bin"
