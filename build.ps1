# Builds two artifacts in dist\:
#   GameOverlay.exe            - single-file (convenient, but more AV false positives)
#   GameOverlay-folder.zip     - onedir build (does NOT self-extract; far fewer AV flags)
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

# Args shared by both builds. --version-file stamps Windows file metadata,
# which improves reputation for an unsigned exe.
$common = @(
    "--noconfirm", "--noconsole", "--name", "GameOverlay",
    "--icon", "icon.ico",
    "--version-file", "assets\version_info.txt",
    "--add-binary", "bin\PresentMon.exe;bin",
    "--add-binary", "bin\HardwareMonitor.exe;bin",
    "--add-data",   "bin\HardwareMonitor.exe.config;bin",
    "--add-binary", "bin\LibreHardwareMonitorLib.dll;bin",
    "--add-binary", "bin\HidSharp.dll;bin",
    "--add-binary", "bin\System.Memory.dll;bin",
    "--add-binary", "bin\System.Buffers.dll;bin",
    "--add-binary", "bin\System.Runtime.CompilerServices.Unsafe.dll;bin",
    "--add-binary", "bin\System.Numerics.Vectors.dll;bin",
    "--add-binary", "bin\System.Threading.Tasks.Extensions.dll;bin"
)

# 1) single-file build -> dist\GameOverlay.exe
& ".venv\Scripts\pyinstaller.exe" @common --onefile --distpath "dist" --workpath "build\onefile" overlay.py
Write-Output "Built: $PSScriptRoot\dist\GameOverlay.exe"

# 2) onedir build -> dist\onedir\GameOverlay\  ->  zipped for release
& ".venv\Scripts\pyinstaller.exe" @common --onedir --distpath "dist\onedir" --workpath "build\onedir" overlay.py
$zip = "dist\GameOverlay-folder.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\onedir\GameOverlay\*" -DestinationPath $zip
Write-Output "Built: $PSScriptRoot\$zip  (antivirus-friendly onedir build)"
