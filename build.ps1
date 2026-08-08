# Builds two artifacts in dist\:
#   GameOverlay.exe            - single-file (convenient, but more AV false positives)
#   GameOverlay-folder.zip     - onedir build (does NOT self-extract; far fewer AV flags)
#
# Usage: .\build.ps1 [-Version 1.0.16]
param([string]$Version = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Version stamped into the exe. Defaults to the newest git tag so the
# metadata cannot silently go stale against what is actually shipped.
if (-not $Version) {
    $tag = (git describe --tags --abbrev=0 2>$null)
    if ($tag) { $Version = $tag -replace '^v', '' } else { $Version = "0.0.0" }
}
$parts = ($Version -split '\.') + @("0", "0", "0", "0")
$vtuple = "$($parts[0]), $($parts[1]), $($parts[2]), 0"
@"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($vtuple), prodvers=($vtuple),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable(u'040904B0', [
      StringStruct(u'CompanyName', u'kynemyers'),
      StringStruct(u'FileDescription', u'GameOverlay - in-game performance overlay'),
      StringStruct(u'FileVersion', u'$Version.0'),
      StringStruct(u'InternalName', u'GameOverlay'),
      StringStruct(u'LegalCopyright', u'MIT License. Open source: github.com/kynemyers/game-overlay'),
      StringStruct(u'OriginalFilename', u'GameOverlay.exe'),
      StringStruct(u'ProductName', u'GameOverlay'),
      StringStruct(u'ProductVersion', u'$Version.0')])]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@ | Set-Content "assets\version_info.txt" -Encoding utf8
Write-Output "Stamping version $Version"

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
if ($LASTEXITCODE -ne 0) { throw "onefile build FAILED (exit $LASTEXITCODE)" }
Write-Output "Built: $PSScriptRoot\dist\GameOverlay.exe"

# 2) onedir build -> dist\onedir\GameOverlay\  ->  zipped for release.
# Clear the old output first: OneDrive/AV can hold locks that make
# PyInstaller's own cleanup fail, which would silently ship a stale zip.
$onedir = "dist\onedir"
if (Test-Path $onedir) {
    Remove-Item $onedir -Recurse -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    if (Test-Path $onedir) { throw "could not clear $onedir (file lock - close the app / pause OneDrive sync)" }
}
& ".venv\Scripts\pyinstaller.exe" @common --onedir --distpath $onedir --workpath "build\onedir" overlay.py
if ($LASTEXITCODE -ne 0) { throw "onedir build FAILED (exit $LASTEXITCODE)" }

$zip = "dist\GameOverlay-folder.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$onedir\GameOverlay\*" -DestinationPath $zip
# Sanity check: the zipped exe must match the freshly built one.
$builtExe = Get-Item "$onedir\GameOverlay\GameOverlay.exe" -ErrorAction Stop
if ($builtExe.LastWriteTime -lt (Get-Date).AddMinutes(-30)) {
    throw "onedir exe looks stale ($($builtExe.LastWriteTime)) - aborting rather than shipping it"
}
Write-Output "Built: $PSScriptRoot\$zip  (antivirus-friendly onedir build)"
