# GameOverlay

A lightweight, fully customisable in-game performance overlay for Windows.

Shows, in an always-on-top click-through overlay:

- **FPS** (via Intel PresentMon, bundled)
- **Ping** and **packet loss** (to any host you choose — your game server IP or 1.1.1.1)
- **CPU usage** and **CPU temperature**
- **GPU usage** and **GPU temperature** (AMD, NVIDIA and Intel GPUs, via LibreHardwareMonitor)

Every metric has its own **on/off toggle** and **colour picker**. You can also change the
**screen position** (top left / top centre / top right / centre / bottom corners, etc.),
font, font size, opacity, and toggle the background box.

## Download

Grab **GameOverlay.exe** from the
[latest release](../../releases/latest) — no install needed, just run it.

> **First run notes**
> - Windows SmartScreen may warn about an unknown publisher: click **More info → Run anyway**.
> - The app asks for **administrator access** (UAC prompt). This is required to read CPU
>   temperature sensors and to capture FPS — decline it and those metrics show `--`.
> - Some antivirus tools flag PyInstaller-packed exes as suspicious. It's a false positive;
>   you can build the exe yourself from this source (see below) if you prefer.

## Usage

1. Run `GameOverlay.exe`. A settings window opens and the overlay appears.
2. Tick/untick metrics, click the colour squares to recolour them, pick a position.
   All changes apply instantly and are saved to `%APPDATA%\GameOverlay\config.json`.
3. Start your game. FPS locks onto whatever game window is in the foreground.
4. Minimise the settings window while playing. **Hide overlay** temporarily hides it;
   **Quit** (or closing the settings window) exits.

### Important: fullscreen mode

Like most external overlays, it **cannot draw over exclusive-fullscreen games**.
Set your game's display mode to **Borderless / Windowed Fullscreen** (looks identical,
and is the default in most modern games). FPS capture works in any mode.

### Ping host

By default it pings `1.1.1.1` (Cloudflare). For a ping that matches your in-game ping,
enter your game server's IP in **Settings → Network**. Packet loss is measured over the
last ~50 pings.

## Running from source

```
pip install psutil
python overlay.py
```

Requires Python 3.10+ on Windows. The `bin\` folder (PresentMon, sensor helper and DLLs)
must sit next to `overlay.py` — it's included in the repo.

## Building the exe

```
build.ps1
```

This creates a venv, installs PyInstaller, and produces `dist\GameOverlay.exe`.

## How it works

- `overlay.py` — tkinter overlay (frameless, transparent, click-through via
  `WS_EX_TRANSPARENT`) plus the settings window and worker threads.
- `hwmon\HardwareMonitor.cs` — tiny C# helper compiled against
  [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor);
  prints CPU/GPU sensor JSON once a second. Compiled with the `csc.exe` that ships with
  Windows, so no .NET SDK is needed.
- FPS comes from [Intel PresentMon](https://github.com/GameTechDev/PresentMon): every CSV
  row it emits is one presented frame, so FPS = rows per second.

## Licence

MIT. Bundles LibreHardwareMonitorLib (MPL 2.0) and Intel PresentMon (MIT).
