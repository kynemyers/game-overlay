# Builds dist\GameOverlay.exe (single-file, windowed).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".venv\Scripts\pip.exe" install --quiet psutil pyinstaller

# Recompile the sensor helper if the source is newer than the exe.
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$src = "hwmon\HardwareMonitor.cs"
$out = "bin\HardwareMonitor.exe"
if (-not (Test-Path $out) -or (Get-Item $src).LastWriteTime -gt (Get-Item $out).LastWriteTime) {
    & $csc /nologo /optimize /target:exe /platform:x64 /out:$out /reference:"bin\LibreHardwareMonitorLib.dll" $src
    Copy-Item "hwmon\HardwareMonitor.exe.config" "bin\" -Force
}

& ".venv\Scripts\pyinstaller.exe" --noconfirm --onefile --noconsole --name GameOverlay `
    --add-binary "bin\PresentMon.exe;bin" `
    --add-binary "bin\HardwareMonitor.exe;bin" `
    --add-data   "bin\HardwareMonitor.exe.config;bin" `
    --add-binary "bin\LibreHardwareMonitorLib.dll;bin" `
    --add-binary "bin\HidSharp.dll;bin" `
    --add-binary "bin\System.Memory.dll;bin" `
    --add-binary "bin\System.Buffers.dll;bin" `
    --add-binary "bin\System.Runtime.CompilerServices.Unsafe.dll;bin" `
    --add-binary "bin\System.Numerics.Vectors.dll;bin" `
    --add-binary "bin\System.Threading.Tasks.Extensions.dll;bin" `
    overlay.py

Write-Output "Built: $PSScriptRoot\dist\GameOverlay.exe"
