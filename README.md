# GameOverlay

![GameOverlay](assets/banner.png)

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

### Ping

In **Auto** mode (the default) the overlay finds the server your game is actually
connected to: it looks up the game's local UDP ports, samples 2 seconds of real network
traffic (Windows doesn't expose UDP remote addresses any other way), and pings whichever
public address the game is exchanging the most packets with. The ping therefore changes
when you switch servers/regions. If a server ignores pings (some block ICMP), the overlay
automatically tries the game's other remote addresses.

FPS, ping and loss all show `--` whenever you're not actively in a game — same as FPS,
auto-mode ping only ever probes a server while it can see you're connected to one, so it
never pings some unrelated address in the background. If you want a ping reading even
when you're not in a game, switch to a fixed **Custom host** in Settings → Network (that
mode always pings the address you enter). Packet loss is measured over the last ~50
pings, once at least 10 have been collected. The status bar in the settings window shows
exactly which address is being pinged.

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
