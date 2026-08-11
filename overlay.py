"""GameOverlay - in-game performance overlay for Windows.

Shows FPS, ping, packet loss, CPU/GPU usage and temperatures in a
click-through always-on-top overlay. Fully customisable: per-metric
toggles and colours, screen position, font, opacity.

FPS capture uses Intel PresentMon (bundled), hardware sensors use
LibreHardwareMonitor via a small bundled helper (bin/HardwareMonitor.exe).
"""

import atexit
import ctypes
import ipaddress
import json
import math
import mmap
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, colorchooser, font as tkfont

try:
    import psutil
except ImportError:
    psutil = None

APP_NAME = "GameOverlay"
CREATE_NO_WINDOW = 0x08000000
TRANSPARENT_COLOR = "#010203"  # magic colour rendered fully transparent

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# ---------------------------------------------------------------- paths ----

def resource_path(rel):
    """Path to a bundled resource (works from source and from PyInstaller exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# --------------------------------------------------------------- config ----

METRIC_ORDER = ["fps", "fps_low", "fps_low01", "ping", "loss",
                "cpu", "cpu_temp", "gpu", "gpu_temp"]

DEFAULT_CONFIG = {
    "position": "Top Right",
    "margin": 24,
    "font_family": "Consolas",
    "font_size": 14,
    "opacity": 92,
    "show_background": True,
    "bg_color": "#101014",
    "ping_mode": "auto",
    "ping_host": "1.1.1.1",
    "metrics": {
        "fps":       {"enabled": True,  "label": "FPS",     "color": "#00ff88"},
        "fps_low":   {"enabled": True,  "label": "1% LOW",  "color": "#00e0a0"},
        "fps_low01": {"enabled": False, "label": "0.1% LOW", "color": "#7fffd4"},
        "ping":      {"enabled": True,  "label": "PING",    "color": "#00ccff"},
        "loss":      {"enabled": True,  "label": "LOSS",    "color": "#ff5555"},
        "cpu":       {"enabled": True,  "label": "CPU",     "color": "#ffcc00"},
        "cpu_temp":  {"enabled": True,  "label": "CPU°",    "color": "#ff9933"},
        "gpu":       {"enabled": True,  "label": "GPU",     "color": "#cc66ff"},
        "gpu_temp":  {"enabled": True,  "label": "GPU°",    "color": "#ff6699"},
    },
}

METRIC_NAMES = {
    "fps": "FPS",
    "fps_low": "1% low FPS",
    "fps_low01": "0.1% low FPS",
    "ping": "Ping",
    "loss": "Packet loss",
    "cpu": "CPU usage",
    "cpu_temp": "CPU temperature",
    "gpu": "GPU usage",
    "gpu_temp": "GPU temperature",
}

POSITIONS = [
    "Top Left", "Top Centre", "Top Right",
    "Centre Left", "Centre", "Centre Right",
    "Bottom Left", "Bottom Centre", "Bottom Right",
]

FONT_CHOICES = ["Consolas", "Courier New", "Segoe UI", "Arial", "Verdana", "Impact"]


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for key, val in saved.items():
            if key == "metrics":
                for m, mv in val.items():
                    if m in cfg["metrics"]:
                        cfg["metrics"][m].update(mv)
            else:
                cfg[key] = val
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass

# ---------------------------------------------------------------- stats ----

class Stats:
    """Thread-safe container for the latest metric values."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}
        self.ping_status = "starting"
        self.hw_status = "starting"
        self.fps_status = "starting"
        self.game_pid = None  # pid of the game the FPS worker is attached to

    def set(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)

    def get(self):
        with self._lock:
            return dict(self._data)


STATS = Stats()
RUNNING = threading.Event()
RUNNING.set()

# ----------------------------------------------------------- ping worker ---

class PingWorker(threading.Thread):
    """Measures latency and packet loss, once a second.

    In "auto" mode it looks up the remote addresses the game process is
    actually connected to (UDP first - that's where game traffic lives)
    and pings the game server itself. Servers that never answer ICMP get
    rotated out for the game's next remote address. Falls back to the
    configured custom host when no game connection is found.
    """

    DETECT_EVERY = 10  # rescan the game's connections every N seconds
    MIN_LOSS_SAMPLES = 10  # don't report loss % from a tiny sample
    SILENT_AFTER = 5   # probes with no reply before calling a server ICMP-blocked

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.history = deque(maxlen=50)  # (success, ms) over last ~50s
        self.cur_host = None
        self.candidates = []
        self.cand_idx = 0
        self.last_detect = 0.0
        self._cand_lock = threading.Lock()
        self._detecting = False
        self._empty_scans = 0
        self._hops = {}        # server ip -> last responding hop ip (or None)
        self._tracing = set()  # servers a traceroute is running for

    def run(self):
        while RUNNING.is_set():
            start = time.time()
            host, source = self._choose_host()

            if host is None:
                # Auto mode, no active game connection - show -- like FPS
                # does, instead of quietly pinging some unrelated host.
                if self.cur_host is not None:
                    self.cur_host = None
                    self.history.clear()
                STATS.set(ping=None, loss=None)
                pid = STATS.game_pid
                if pid and psutil and psutil.pid_exists(pid):
                    # Game is up but sending no gameplay traffic - almost
                    # always a menu/lobby between matches.
                    STATS.ping_status = "game open, no match traffic yet"
                else:
                    STATS.ping_status = "waiting for a game"
                time.sleep(max(0.0, 1.0 - (time.time() - start)))
                continue

            if host != self.cur_host:
                self.cur_host = host
                self.history.clear()
            success, ms = self._ping_once(host)
            self.history.append((success, ms))

            answered = any(ok for ok, _ in self.history)
            silent = not answered and len(self.history) >= self.SILENT_AFTER

            if silent and source == "auto":
                # We can see the game exchanging packets with this server, so
                # the connection is plainly fine - the server is just refusing
                # ICMP (very common; COD and many others drop ping and TCP
                # entirely). Never report that as 100% loss.
                #
                # Routers *on the way* to the server usually do answer, and the
                # last one that does is normally in the server's own
                # datacentre - so its round-trip is a good approximation of
                # real ping. Shown with a ~ so it is never mistaken for exact.
                hop = self._hops.get(host, "pending")
                if hop == "pending":
                    self._start_trace(host)
                    STATS.set(ping=None, loss=None)
                    STATS.ping_status = "%s - blocks ping, tracing route..." % host
                elif hop:
                    ok, ms = self._ping_once(hop)
                    if ok:
                        STATS.set(ping=ms, ping_approx=True, loss=None)
                        STATS.ping_status = "~%s via %s (server blocks ping)" % (host, hop)
                    else:
                        STATS.set(ping=None, loss=None)
                        STATS.ping_status = "%s - blocks ping (hop unreachable)" % host
                else:
                    STATS.set(ping=None, loss=None)
                    STATS.ping_status = "%s - server blocks ping" % host
                time.sleep(max(0.0, 1.0 - (time.time() - start)))
                continue
            STATS.set(ping_approx=False)

            # Loss is only meaningful against a host that does answer pings;
            # otherwise we cannot tell loss apart from an ICMP-blocking host.
            loss = None
            if answered and len(self.history) >= self.MIN_LOSS_SAMPLES:
                fails = sum(1 for ok, _ in self.history if not ok)
                loss = 100.0 * fails / len(self.history)
            elif not answered and len(self.history) >= self.MIN_LOSS_SAMPLES:
                # Custom host the user picked themselves: a host that never
                # replies really is 100% unreachable, and they asked for that.
                loss = 100.0
            last_ok = next((m for ok, m in reversed(self.history) if ok), None)

            if answered:
                STATS.ping_status = "%s (%s)" % (host, source)
            else:
                STATS.ping_status = "%s (%s) not answering" % (host, source)
            STATS.set(ping=last_ok, loss=loss)
            time.sleep(max(0.0, 1.0 - (time.time() - start)))

    def _choose_host(self):
        if self.cfg.get("ping_mode", "auto") != "auto":
            manual = (self.cfg.get("ping_host") or "1.1.1.1").strip() or "1.1.1.1"
            return manual, "custom"
        now = time.time()
        if now - self.last_detect >= self.DETECT_EVERY and not self._detecting:
            self.last_detect = now
            self._detecting = True
            threading.Thread(target=self._detect_bg, daemon=True).start()
        with self._cand_lock:
            cands = list(self.candidates)
        if cands:
            return cands[self.cand_idx % len(cands)], "auto"
        return None, "none"  # no game connection detected - show --, don't probe anything

    STALE_AFTER_SCANS = 2  # ~2 quiet detection cycles (~20s) -> drop last known server

    def _detect_bg(self):
        """Detection samples live traffic for ~2s, so it runs off-thread."""
        try:
            found = self._detect_game_server()
            pid = STATS.game_pid
            game_alive = bool(pid and psutil and psutil.pid_exists(pid))
            with self._cand_lock:
                if found:
                    self._empty_scans = 0
                    if found != self.candidates:
                        self.candidates = found
                        self.cand_idx = 0
                elif not game_alive:
                    self.candidates = []  # game closed - stop pinging its server
                    self._empty_scans = 0
                elif self.candidates:
                    # Game is alive but sent no traffic this scan (menu/lobby
                    # lull) - keep the last known server for a couple of
                    # scans, but don't ping a stale/disconnected address
                    # forever; fall back once the quiet spell persists.
                    self._empty_scans += 1
                    if self._empty_scans >= self.STALE_AFTER_SCANS:
                        self.candidates = []
                        self._empty_scans = 0
        finally:
            self._detecting = False

    @classmethod
    def _detect_game_server(cls):
        """Find the game server's public IPv4, busiest traffic first.

        Windows never exposes remote addresses for UDP sockets, and live
        gameplay traffic is essentially always UDP - so we take the game's
        local UDP port numbers and sample real packets with a raw socket
        (needs admin): whichever public IP exchanges the most packets on
        those ports is the server.

        Deliberately UDP-only. A game's TCP connections go to CDN, auth and
        matchmaking hosts (Akamai/Cloudflare/Google), not the match server -
        pinging those produces a confident but meaningless number. Showing
        -- until real gameplay traffic appears is the honest answer.
        """
        pid = STATS.game_pid
        if not pid or not psutil or not psutil.pid_exists(pid):
            return []
        return cls._sniff_udp_peers(cls._game_udp_ports(pid))

    @staticmethod
    def _game_udp_ports(pid):
        """Local UDP port numbers used by the game and its child processes."""
        ports = set()
        try:
            procs = [psutil.Process(pid)]
            procs += procs[0].children(recursive=True)
        except psutil.Error:
            return ports
        for p in procs:
            try:
                get_conns = getattr(p, "net_connections", None) or p.connections
                for c in get_conns(kind="inet4"):
                    if c.type == socket.SOCK_DGRAM and c.laddr:
                        ports.add(c.laddr.port)
            except psutil.Error:
                continue
        return ports

    @staticmethod
    def _local_ipv4s():
        """Every usable local IPv4, most-likely-default first.

        A raw socket only captures traffic on the interface it is bound to,
        so with a VPN (NordVPN/Wireguard etc.) or multiple NICs we must watch
        all of them - the game's traffic may not be on the default route.
        """
        ips = []
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 53))
            ips.append(probe.getsockname()[0])  # default-route IP goes first
        except OSError:
            pass
        finally:
            probe.close()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                ip = info[4][0]
                if ip in ips:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if addr.is_loopback or addr.is_link_local:
                    continue  # 127.x and unconfigured 169.254.x adapters
                ips.append(ip)
        except OSError:
            pass
        return ips

    @classmethod
    def _sniff_udp_peers(cls, ports, duration=2.0):
        """Sample UDP traffic on the game's ports; public peers, busiest first.

        Captures on every local interface simultaneously so a VPN tunnel or
        secondary NIC carrying the game traffic is not missed.
        """
        if not ports:
            return []
        local_ips = cls._local_ipv4s()
        if not local_ips:
            return []
        counts = Counter()
        counts_lock = threading.Lock()

        def capture(local_ip):
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                  socket.IPPROTO_IP)
                s.bind((local_ip, 0))
                s.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
                s.settimeout(0.25)
                deadline = time.time() + duration
                local = Counter()
                while time.time() < deadline and RUNNING.is_set():
                    try:
                        data = s.recv(65535)
                    except socket.timeout:
                        continue
                    if len(data) < 28 or data[9] != 17:  # not UDP
                        continue
                    ihl = (data[0] & 0x0F) * 4
                    if len(data) < ihl + 8:
                        continue
                    sport = int.from_bytes(data[ihl:ihl + 2], "big")
                    dport = int.from_bytes(data[ihl + 2:ihl + 4], "big")
                    if dport in ports:        # inbound packet -> peer is source
                        peer = data[12:16]
                    elif sport in ports:      # outbound packet -> peer is dest
                        peer = data[16:20]
                    else:
                        continue
                    local[socket.inet_ntoa(peer)] += 1
                with counts_lock:
                    counts.update(local)
            except OSError:
                pass  # interface refuses raw capture (common on tunnels)
            finally:
                if s is not None:
                    try:
                        s.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
                    except OSError:
                        pass
                    s.close()

        threads = [threading.Thread(target=capture, args=(ip,), daemon=True)
                   for ip in local_ips]
        for t in threads:
            t.start()
        for t in threads:
            t.join(duration + 2.0)
        result = []
        for ip, n in counts.most_common():
            if n < 8:  # sustained flow only, not a stray DNS lookup
                break
            try:
                if ipaddress.ip_address(ip).is_global:
                    result.append(ip)
            except ValueError:
                continue
        return result

    def _start_trace(self, server):
        """Trace toward an unpingable server to find the closest hop that answers."""
        if server in self._tracing:
            return
        self._tracing.add(server)

        def worker():
            hop = None
            try:
                out = subprocess.run(
                    ["tracert", "-d", "-h", "24", "-w", "600", server],
                    capture_output=True, text=True, timeout=120,
                    creationflags=CREATE_NO_WINDOW,
                )
                for line in (out.stdout or "").splitlines():
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s*$", line.strip())
                    if not m:
                        continue
                    ip = m.group(1)
                    if ip == server:
                        continue  # the server itself answered; not our case
                    try:
                        if ipaddress.ip_address(ip).is_global:
                            hop = ip  # keep the furthest public responder
                    except ValueError:
                        continue
            except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
                pass
            finally:
                self._hops[server] = hop
                self._tracing.discard(server)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _ping_once(host):
        try:
            out = subprocess.run(
                ["ping", "-n", "1", "-w", "1000", host],
                capture_output=True, text=True, timeout=4,
                creationflags=CREATE_NO_WINDOW,
            )
            text = out.stdout or ""
            if "TTL=" in text.upper():
                m = re.search(r"[=<]\s*(\d+)\s*ms", text)
                return True, (int(m.group(1)) if m else 0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return False, None

# --------------------------------------------------- windows gpu sensors ---

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _LUID), ("Flags", wintypes.UINT)]


class _D3DKMT_ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("AdapterLuid", _LUID),
                ("NumOfSources", wintypes.ULONG),
                ("bPrecisePresentRegionsPreferred", wintypes.BOOL)]


class _D3DKMT_ENUMADAPTERS2(ctypes.Structure):
    _fields_ = [("NumAdapters", wintypes.ULONG),
                ("pAdapters", ctypes.POINTER(_D3DKMT_ADAPTERINFO))]


class _D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int),
                ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", wintypes.UINT)]


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _PDH_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE)]


class WinGpu:
    """GPU temperature and utilisation read straight from Windows.

    LibreHardwareMonitor talks to vendor SDKs, which go blind whenever a card
    is newer than the bundled library or the vendor driver is mid-update - on
    an RX 9070 XT it reports no GPU sensors at all. These two Windows
    interfaces are vendor-neutral and keep working regardless:

      temperature - D3DKMTQueryAdapterInfo(ADAPTER_PERFDATA) from the graphics
                    kernel, which also carries fan RPM
      utilisation - the "GPU Engine" performance counters, summed over the
                    3D engines belonging to this adapter

    Everything is best-effort: any failure leaves the values as None and the
    LibreHardwareMonitor readings stand.
    """

    PERFDATA_TYPE = 62   # KMTQAITYPE_ADAPTERPERFDATA
    PERFDATA_SIZE = 64
    TEMP_OFFSET = 56     # ADAPTER_PERFDATA.Temperature, in 0.1 C
    FAN_OFFSET = 48
    PDH_FMT_DOUBLE = 0x00000200

    def __init__(self):
        self.name = None
        self._luid = None       # (high, low) of the chosen adapter
        self._hadapter = None
        self._pdh = None
        self._query = None
        self._counter = None
        try:
            self._pick_adapter()
            self._open_counters()
        except Exception:       # ctypes/COM boundary - never break the app
            pass

    # -- setup --
    def _pick_adapter(self):
        """Choose the discrete GPU: the adapter with the most dedicated VRAM."""
        dxgi = ctypes.WinDLL("dxgi")
        iid = _GUID(0x770aae78, 0xf26f, 0x4dba,
                    (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c,
                                         0x83, 0xd1, 0xb3, 0x87))
        fac = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(fac)) != 0:
            return
        fvt = ctypes.cast(fac, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        enum1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p))(fvt[12])
        best = None
        i = 0
        while True:
            ad = ctypes.c_void_p()
            if enum1(fac, i, ctypes.byref(ad)) != 0:
                break
            avt = ctypes.cast(ad, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            desc1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                       ctypes.POINTER(_DXGI_ADAPTER_DESC1))(avt[10])
            d = _DXGI_ADAPTER_DESC1()
            if desc1(ad, ctypes.byref(d)) == 0 and d.DedicatedVideoMemory:
                if best is None or d.DedicatedVideoMemory > best[1]:
                    best = (d.Description, d.DedicatedVideoMemory,
                            d.AdapterLuid.HighPart, d.AdapterLuid.LowPart)
            i += 1
        if best:
            self.name = best[0]
            self._luid = (best[2], best[3])
            self._hadapter = self._find_kmt_handle(*self._luid)

    @staticmethod
    def _find_kmt_handle(hi, lo):
        """Graphics-kernel handle for the adapter with this LUID."""
        gdi32 = ctypes.windll.gdi32
        e = _D3DKMT_ENUMADAPTERS2()
        e.NumAdapters = 0
        e.pAdapters = None
        if gdi32.D3DKMTEnumAdapters2(ctypes.byref(e)) != 0 or not e.NumAdapters:
            return None
        arr = (_D3DKMT_ADAPTERINFO * e.NumAdapters)()
        e.pAdapters = ctypes.cast(arr, ctypes.POINTER(_D3DKMT_ADAPTERINFO))
        if gdi32.D3DKMTEnumAdapters2(ctypes.byref(e)) != 0:
            return None
        for i in range(e.NumAdapters):
            if (arr[i].AdapterLuid.HighPart == hi
                    and arr[i].AdapterLuid.LowPart == lo):
                return arr[i].hAdapter
        return None

    def _open_counters(self):
        pdh = ctypes.WinDLL("pdh")
        q = wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
            return
        c = wintypes.HANDLE()
        if pdh.PdhAddEnglishCounterW(
                q, r"\GPU Engine(*)\Utilization Percentage",
                0, ctypes.byref(c)) != 0:
            return
        pdh.PdhCollectQueryData(q)   # first sample primes the counter
        self._pdh, self._query, self._counter = pdh, q, c

    # -- reads --
    def temperature(self):
        if self._hadapter is None:
            return None
        try:
            buf = (ctypes.c_ubyte * self.PERFDATA_SIZE)()
            q = _D3DKMT_QUERYADAPTERINFO()
            q.hAdapter = self._hadapter
            q.Type = self.PERFDATA_TYPE
            q.pPrivateDriverData = ctypes.cast(buf, ctypes.c_void_p)
            q.PrivateDriverDataSize = self.PERFDATA_SIZE
            if ctypes.windll.gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(q)) & 0xFFFFFFFF:
                return None
            deci_c = int.from_bytes(
                bytes(buf[self.TEMP_OFFSET:self.TEMP_OFFSET + 4]), "little")
            temp = deci_c / 10.0
            return temp if 0.0 < temp < 150.0 else None
        except Exception:
            return None

    def load(self):
        if not self._query:
            return None
        try:
            self._pdh.PdhCollectQueryData(self._query)
            size = wintypes.DWORD(0)
            count = wintypes.DWORD(0)
            self._pdh.PdhGetFormattedCounterArrayW(
                self._counter, self.PDH_FMT_DOUBLE,
                ctypes.byref(size), ctypes.byref(count), None)
            if not size.value:
                return None
            buf = (ctypes.c_ubyte * size.value)()
            if self._pdh.PdhGetFormattedCounterArrayW(
                    self._counter, self.PDH_FMT_DOUBLE, ctypes.byref(size),
                    ctypes.byref(count), buf) != 0:
                return None
            items = ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM_W))
            tag = ("luid_0x%08x_0x%08x" % self._luid).lower()
            # Sum each engine type across processes, then report the busiest -
            # what Task Manager shows. Summing every engine instead would
            # double-count (copy/video run alongside 3D), and taking 3D alone
            # would read zero for compute workloads.
            per_engine = {}
            for i in range(count.value):
                nm = (items[i].szName or "").lower()
                if tag not in nm:
                    continue
                m = re.search(r"engtype_(\w+)", nm)
                if not m:
                    continue
                per_engine[m.group(1)] = (per_engine.get(m.group(1), 0.0)
                                          + items[i].FmtValue.doubleValue)
            if not per_engine:
                return None
            return max(0.0, min(100.0, max(per_engine.values())))
        except Exception:
            return None


# ---------------------------------------------------- core temp fallback ---

class CoreTempShm:
    """CPU temperature from Core Temp's shared memory, when it is running.

    Some machines refuse to load the kernel driver LibreHardwareMonitor uses
    for CPU temperature - Windows' vulnerable-driver blocklist covers it - so
    every core reads back empty even though the chip is fully supported. Core
    Temp (free, alcpu.com) ships its own signed driver and publishes readings
    through a documented shared memory block, so if the user runs it we can
    show a real CPU temperature instead of nothing.

    Entirely optional: no Core Temp simply means no value, no error.
    """

    TAGS = ("CoreTempMappingObjectEx", "CoreTempMappingObject")
    # CORE_TEMP_SHARED_DATA: uiLoad[256], uiTjMax[128], uiCoreCnt, uiCPUCnt,
    # fTemp[256], fVID, fCPUSpeed, fFSBSpeed, fMultiplier, sCPUName[100],
    # ucFahrenheit, ucDeltaToTjMax
    FMT = "<256I128I2I256f4f100sBB"
    SIZE = struct.calcsize(FMT)

    def __init__(self):
        self.cpu_name = None

    def cpu_temperature(self):
        """Hottest core right now, in Celsius, or None."""
        for tag in self.TAGS:
            try:
                m = mmap.mmap(-1, self.SIZE, tagname=tag, access=mmap.ACCESS_READ)
            except OSError:
                continue
            try:
                raw = m.read(self.SIZE)
            finally:
                m.close()
            if len(raw) < self.SIZE or not any(raw):
                continue        # mapping exists but Core Temp is not publishing
            try:
                vals = struct.unpack(self.FMT, raw)
            except struct.error:
                continue
            tjmax = vals[256:384]
            core_cnt, cpu_cnt = vals[384], vals[385]
            temps = vals[386:642]
            self.cpu_name = vals[646].split(b"\x00")[0].decode("latin-1", "ignore")
            fahrenheit, delta_to_tjmax = vals[647], vals[648]
            n = max(1, core_cnt) * max(1, cpu_cnt)
            readings = []
            for i in range(min(n, len(temps))):
                t = temps[i]
                if delta_to_tjmax:      # value is degrees below TjMax
                    t = tjmax[i if i < len(tjmax) else 0] - t
                if fahrenheit:
                    t = (t - 32.0) * 5.0 / 9.0
                if 0.0 < t < 150.0:
                    readings.append(t)
            if readings:
                return max(readings)    # hottest core, as most tools report
        return None


# ------------------------------------------------------- hardware worker ---

class HardwareWorker(threading.Thread):
    """Runs bin/HardwareMonitor.exe and parses its JSON lines.

    Falls back to psutil for CPU usage if the helper is unavailable.
    """

    DISCRETE_RE = re.compile(r"\b(RX|RTX|GTX|ARC|TITAN)\b", re.IGNORECASE)

    def __init__(self):
        super().__init__(daemon=True)
        self.proc = None
        self.wingpu = WinGpu()
        self.coretemp = CoreTempShm()

    def run(self):
        exe = resource_path(os.path.join("bin", "HardwareMonitor.exe"))
        while RUNNING.is_set():
            if not os.path.exists(exe):
                STATS.hw_status = "helper missing"
                self._psutil_only(30)
                continue
            try:
                self.proc = subprocess.Popen(
                    [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, bufsize=1,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as e:
                STATS.hw_status = "helper failed: %s" % e
                self._psutil_only(30)
                continue
            STATS.hw_status = "ok"
            try:
                for line in self.proc.stdout:
                    if not RUNNING.is_set():
                        return
                    self._handle_line(line)
            except (OSError, ValueError):
                pass
            err = ""
            try:
                err = (self.proc.stderr.read() or "").strip().splitlines()[-1:]
                err = err[0] if err else ""
            except (OSError, ValueError, IndexError):
                pass
            STATS.hw_status = "sensor helper stopped" + (": " + err if err else "")
            self._psutil_only(15)  # wait, then retry the helper

    def _handle_line(self, line):
        try:
            data = json.loads(line)
        except ValueError:
            return
        cpu_load = data.get("cpu_load")
        if cpu_load is None and psutil:
            cpu_load = psutil.cpu_percent(interval=None)

        # A machine that blocks the sensor driver reports every core as empty.
        # Core Temp, if the user runs it, has its own driver and publishes
        # readings we can use instead of showing nothing.
        cpu_temp = data.get("cpu_temp")
        if cpu_temp is None:
            cpu_temp = self.coretemp.cpu_temperature()
            if cpu_temp is not None and "Core Temp" not in STATS.hw_status:
                STATS.hw_status = "%s (CPU via Core Temp)" % STATS.hw_status.split(" (")[0]

        gpu = self._pick_gpu(data.get("gpus") or [])
        gpu_temp = gpu.get("temp") if gpu else None
        gpu_load = gpu.get("load") if gpu else None

        # No temperature means the vendor SDK could not read this card (too
        # new for the bundled sensor library, or the driver is mid-update).
        # Windows can still tell us, so use it for both figures rather than
        # trusting a load reading from a source that is clearly blind.
        if gpu_temp is None:
            win_temp = self.wingpu.temperature()
            win_load = self.wingpu.load()
            if win_temp is not None or win_load is not None:
                gpu_temp = win_temp
                gpu_load = win_load
                STATS.hw_status = "ok (GPU via Windows)"

        STATS.set(
            cpu=cpu_load,
            cpu_temp=cpu_temp,
            gpu=gpu_load,
            gpu_temp=gpu_temp,
        )

    @classmethod
    def _pick_gpu(cls, gpus):
        if not gpus:
            return None
        discrete = [g for g in gpus if cls.DISCRETE_RE.search(g.get("name") or "")]
        candidates = discrete or gpus
        return max(candidates, key=lambda g: g.get("load") or -1.0)

    def _psutil_only(self, seconds):
        """Helper unavailable: CPU load from psutil, GPU from Windows itself."""
        for _ in range(seconds):
            if not RUNNING.is_set():
                return
            STATS.set(cpu=psutil.cpu_percent(interval=None) if psutil else None,
                      cpu_temp=None,
                      gpu=self.wingpu.load(),
                      gpu_temp=self.wingpu.temperature())
            time.sleep(1)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # helper exits on stdin EOF
            except OSError:
                pass
            try:
                self.proc.kill()
            except OSError:
                pass

# ------------------------------------------------------------ fps worker ---

IGNORED_PROCS = {
    "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "applicationframehost.exe",
    "taskmgr.exe", "lockapp.exe", "dwm.exe", "systemsettings.exe",
    # browsers/launchers: alt-tabbing to these shouldn't steal the game target
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe",
    "brave.exe", "vivaldi.exe", "discord.exe", "steam.exe", "steamwebhelper.exe",
    "epicgameslauncher.exe", "battle.net.exe", "claude.exe", "spotify.exe",
    "slack.exe", "whatsapp.exe", "whatsapp.root.exe", "telegram.exe",
    "signal.exe", "teams.exe", "ms-teams.exe", "messenger.exe",
    # consoles/terminals: running the overlay from a prompt otherwise makes
    # that prompt "the game", so FPS attaches to it and ping hunts for match
    # traffic on a shell that will never have any
    "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "openconsole.exe", "python.exe", "pythonw.exe",
    "py.exe", "code.exe", "notepad.exe", "wt.exe",
}


def foreground_pid():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


class FpsWorker(threading.Thread):
    """Attaches Intel PresentMon to the foreground game and reads frame times.

    Each CSV row PresentMon emits is one presented frame; the MsBetweenPresents
    column is that frame's time in milliseconds. Average FPS is frames-per-
    second over a short window; the 1% / 0.1% lows are derived from the worst
    (longest) frame times over a longer window - the number that reflects
    stutter, which average FPS hides.
    """

    AVG_WINDOW = 2.0    # seconds, for the responsive average FPS number
    LOW_WINDOW = 60.0   # seconds of frame times feeding the 1% / 0.1% lows
    MIN_LOW_FRAMES = 120     # need a real sample before showing a 1% low
    MIN_LOW01_FRAMES = 1000  # 0.1% low needs ~1000+ frames to mean anything

    def __init__(self):
        super().__init__(daemon=True)
        self.proc = None
        self.target_pid = None
        self.target_name = None
        self.samples = deque()  # (timestamp, frametime_ms or None)
        self._frames_lock = threading.Lock()

    @staticmethod
    def _percentile(sorted_vals, pct):
        """Nearest-rank percentile of an ascending-sorted list."""
        if not sorted_vals:
            return None
        k = int(math.ceil(pct / 100.0 * len(sorted_vals))) - 1
        k = max(0, min(len(sorted_vals) - 1, k))
        return sorted_vals[k]

    def run(self):
        exe = resource_path(os.path.join("bin", "PresentMon.exe"))
        if not os.path.exists(exe):
            STATS.fps_status = "PresentMon missing"
            return
        while RUNNING.is_set():
            self._maybe_retarget(exe)
            fps = fps_low = fps_low01 = None
            if self.proc is not None and self.proc.poll() is None:
                fps, fps_low, fps_low01 = self._compute()
            STATS.set(fps=fps, fps_low=fps_low, fps_low01=fps_low01)
            time.sleep(1.0)

    def _compute(self):
        now = time.time()
        with self._frames_lock:
            while self.samples and now - self.samples[0][0] > self.LOW_WINDOW:
                self.samples.popleft()
            recent = sum(1 for ts, _ in self.samples if now - ts <= self.AVG_WINDOW)
            frametimes = sorted(ft for _, ft in self.samples if ft is not None)
        if not recent:
            # Nothing rendered in the last couple of seconds (alt-tabbed or
            # sitting in a menu). Blank the lows too: showing a stale 1% low
            # from a minute ago next to an empty FPS reads as a broken
            # overlay, and the number no longer describes what you're doing.
            return None, None, None
        fps = recent / self.AVG_WINDOW
        fps_low = fps_low01 = None
        n = len(frametimes)
        if n >= self.MIN_LOW_FRAMES:
            p99 = self._percentile(frametimes, 99.0)  # worst-1% frame time
            if p99 and p99 > 0:
                fps_low = 1000.0 / p99
        if n >= self.MIN_LOW01_FRAMES:
            p999 = self._percentile(frametimes, 99.9)  # worst-0.1% frame time
            if p999 and p999 > 0:
                fps_low01 = 1000.0 / p999
        return fps, fps_low, fps_low01

    def _maybe_retarget(self, exe):
        pid = foreground_pid()
        name = None
        if pid and psutil:
            try:
                name = psutil.Process(pid).name().lower()
            except psutil.Error:
                pid = None
        if not pid or pid == os.getpid() or (name and name in IGNORED_PROCS):
            # Foreground is us or the shell - keep watching the current game.
            if self.proc is not None and self.proc.poll() is not None:
                self._stop_proc()
                STATS.fps_status = "waiting for a game"
            return
        if pid == self.target_pid and self.proc and self.proc.poll() is None:
            return  # already attached
        self._stop_proc()
        self.target_pid, self.target_name = pid, name
        STATS.game_pid = pid
        with self._frames_lock:
            self.samples.clear()
        try:
            self.proc = subprocess.Popen(
                [exe, "--process_id", str(pid), "--output_stdout",
                 "--stop_existing_session", "--terminate_on_proc_exit",
                 "--session_name", "GameOverlayFPS"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            STATS.fps_status = "PresentMon failed: %s" % e
            self.proc = None
            return
        STATS.fps_status = "capturing %s" % (name or pid)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._err_reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        ft_col = None      # index of the MsBetweenPresents column in this session
        fg = True          # is the game currently the foreground window?
        last_fg_check = 0.0
        try:
            for line in proc.stdout:
                if not RUNNING.is_set():
                    return
                if not line.strip():
                    continue
                if line.startswith("Application"):  # CSV header
                    cols = line.strip().split(",")
                    ft_col = cols.index("MsBetweenPresents") \
                        if "MsBetweenPresents" in cols else None
                    continue
                now = time.time()
                # When you alt-tab out, the game renders throttled background
                # frames (~30fps) that you never actually saw - counting them
                # would wreck the 1%/0.1% lows. Only keep frames rendered while
                # the game is the foreground window. Checked ~4x/sec (cheap but
                # not per-frame at hundreds of fps).
                if now - last_fg_check >= 0.25:
                    fg = (foreground_pid() == self.target_pid)
                    last_fg_check = now
                if not fg:
                    continue
                ft = None
                if ft_col is not None:
                    parts = line.split(",")
                    if len(parts) > ft_col:
                        try:
                            ft = float(parts[ft_col])
                        except ValueError:
                            ft = None  # e.g. "NA" on the first frame
                with self._frames_lock:
                    self.samples.append((now, ft))
        except (OSError, ValueError):
            pass

    def _err_reader(self, proc):
        try:
            err = (proc.stderr.read() or "").strip()
            if err and proc.poll() not in (None, 0):
                STATS.fps_status = "PresentMon: %s" % err.splitlines()[-1][:120]
        except (OSError, ValueError):
            pass

    def _stop_proc(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def stop(self):
        self._stop_proc()

# ------------------------------------------------------------- formatting --

def format_value(metric, stats):
    val = stats.get(metric)
    if val is None:
        return "--"
    if metric in ("fps", "fps_low", "fps_low01"):
        return "%d" % round(val)
    if metric == "ping":
        # "~" marks a value measured against the closest router that answers,
        # used when the game server itself refuses to be pinged.
        return "%s%d ms" % ("~" if stats.get("ping_approx") else "", round(val))
    if metric == "loss":
        return "%.1f %%" % val
    if metric in ("cpu", "gpu"):
        return "%d %%" % round(val)
    if metric in ("cpu_temp", "gpu_temp"):
        return "%d°C" % round(val)
    return str(val)

# ------------------------------------------------------------ overlay win --

class OverlayWindow:
    def __init__(self, root, cfg):
        self.cfg = cfg
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.config(bg=TRANSPARENT_COLOR)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.frame = tk.Frame(self.win, bg=TRANSPARENT_COLOR)
        self.frame.pack()
        self.labels = {}
        self.visible = True
        self.rebuild()
        self.win.update_idletasks()
        self._make_click_through()

    def _make_click_through(self):
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def rebuild(self):
        """Recreate labels from current config (toggles, colours, font)."""
        cfg = self.cfg
        bg = cfg["bg_color"] if cfg["show_background"] else TRANSPARENT_COLOR
        size = int(cfg["font_size"])
        self.frame.config(bg=bg, padx=max(6, int(size * 0.7)), pady=max(4, int(size * 0.4)))
        for lbl in self.labels.values():
            lbl.destroy()
        self.labels = {}
        fnt = (cfg["font_family"], int(cfg["font_size"]), "bold")
        enabled = [m for m in METRIC_ORDER if cfg["metrics"][m]["enabled"]]
        pad = max((len(cfg["metrics"][m]["label"]) for m in enabled), default=0)
        for m in enabled:
            lbl = tk.Label(self.frame, text="", font=fnt, bg=bg,
                           fg=cfg["metrics"][m]["color"], anchor="w", justify="left")
            lbl._label_pad = pad
            lbl.pack(anchor="w")
            self.labels[m] = lbl
        try:
            self.win.attributes("-alpha", max(0.2, int(cfg["opacity"]) / 100.0))
        except tk.TclError:
            pass
        self.refresh()

    def refresh(self):
        stats = STATS.get()
        for m, lbl in self.labels.items():
            label = self.cfg["metrics"][m]["label"]
            lbl.config(text="%-*s %s" % (lbl._label_pad + 1, label, format_value(m, stats)))
        if self.visible:
            # Reassert topmost (some games steal it) BEFORE repositioning:
            # tk's -topmost attribute snaps the window back to its last-
            # committed geometry as a side effect, so geometry must be set
            # afterwards to win. Doing it in the opposite order is what
            # caused the overlay to appear stuck at its initial position.
            self.win.attributes("-topmost", True)
        self._reposition()

    def _reposition(self):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        margin = int(self.cfg.get("margin", 24))
        pos = self.cfg.get("position", "Top Right")
        if "Left" in pos:
            x = margin
        elif "Right" in pos:
            x = sw - w - margin
        else:
            x = (sw - w) // 2
        if "Top" in pos:
            y = margin
        elif "Bottom" in pos:
            y = sh - h - margin
        else:
            y = (sh - h) // 2
        self.win.geometry("+%d+%d" % (x, y))

    def set_visible(self, visible):
        self.visible = visible
        if visible:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            self._reposition()
        else:
            self.win.withdraw()

# ----------------------------------------------------------- settings win --

class SettingsWindow:
    def __init__(self, root, cfg, overlay, workers):
        self.root = root
        self.cfg = cfg
        self.overlay = overlay
        self.workers = workers
        root.title("%s Settings" % APP_NAME)
        root.resizable(False, False)
        try:
            root.iconbitmap(default="")
        except tk.TclError:
            pass

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        # --- metrics ---
        mf = ttk.LabelFrame(main, text=" Metrics ", padding=8)
        mf.pack(fill="x")
        self.metric_vars = {}
        self.swatches = {}
        for i, m in enumerate(METRIC_ORDER):
            var = tk.BooleanVar(value=cfg["metrics"][m]["enabled"])
            self.metric_vars[m] = var
            cb = ttk.Checkbutton(mf, text=METRIC_NAMES[m], variable=var,
                                 command=lambda m=m: self._toggle(m))
            cb.grid(row=i, column=0, sticky="w", pady=1)
            sw = tk.Button(mf, width=3, relief="ridge", cursor="hand2",
                           bg=cfg["metrics"][m]["color"], activebackground=cfg["metrics"][m]["color"],
                           command=lambda m=m: self._pick_color(m))
            sw.grid(row=i, column=1, padx=(16, 0), pady=1)
            self.swatches[m] = sw
        mf.columnconfigure(0, weight=1)

        # --- appearance ---
        af = ttk.LabelFrame(main, text=" Appearance ", padding=8)
        af.pack(fill="x", pady=(10, 0))
        ttk.Label(af, text="Font").grid(row=0, column=0, sticky="w")
        self.font_var = tk.StringVar(value=cfg["font_family"])
        fc = ttk.Combobox(af, textvariable=self.font_var, values=FONT_CHOICES,
                          width=14, state="readonly")
        fc.grid(row=0, column=1, sticky="w", padx=8, pady=2)
        fc.bind("<<ComboboxSelected>>", lambda e: self._set("font_family", self.font_var.get()))

        ttk.Label(af, text="Size").grid(row=1, column=0, sticky="w")
        self.size_var = tk.IntVar(value=cfg["font_size"])
        sp = ttk.Spinbox(af, from_=6, to=72, textvariable=self.size_var, width=6,
                         command=self._set_size)
        sp.grid(row=1, column=1, sticky="w", padx=8, pady=2)
        sp.bind("<Return>", lambda e: self._set_size())
        sp.bind("<FocusOut>", lambda e: self._set_size())

        ttk.Label(af, text="Opacity").grid(row=2, column=0, sticky="w")
        self.opacity_var = tk.IntVar(value=cfg["opacity"])
        ttk.Scale(af, from_=30, to=100, variable=self.opacity_var, length=140,
                  command=lambda v: self._set("opacity", int(float(v)))
                  ).grid(row=2, column=1, sticky="w", padx=8, pady=2)

        self.bg_var = tk.BooleanVar(value=cfg["show_background"])
        ttk.Checkbutton(af, text="Background box", variable=self.bg_var,
                        command=lambda: self._set("show_background", self.bg_var.get())
                        ).grid(row=3, column=0, sticky="w", pady=2)
        self.bg_swatch = tk.Button(af, width=3, relief="ridge", cursor="hand2",
                                   bg=cfg["bg_color"], activebackground=cfg["bg_color"],
                                   command=self._pick_bg)
        self.bg_swatch.grid(row=3, column=1, sticky="w", padx=8)

        # --- position ---
        pf = ttk.LabelFrame(main, text=" Position ", padding=8)
        pf.pack(fill="x", pady=(10, 0))
        ttk.Label(pf, text="Screen position").grid(row=0, column=0, sticky="w")
        self.pos_var = tk.StringVar(value=cfg["position"])
        pc = ttk.Combobox(pf, textvariable=self.pos_var, values=POSITIONS,
                          width=14, state="readonly")
        pc.grid(row=0, column=1, sticky="w", padx=8, pady=2)
        pc.bind("<<ComboboxSelected>>", lambda e: self._set("position", self.pos_var.get()))
        ttk.Label(pf, text="Edge margin (px)").grid(row=1, column=0, sticky="w")
        self.margin_var = tk.IntVar(value=cfg["margin"])
        ttk.Spinbox(pf, from_=0, to=300, increment=4, textvariable=self.margin_var, width=6,
                    command=lambda: self._set("margin", self.margin_var.get())
                    ).grid(row=1, column=1, sticky="w", padx=8, pady=2)

        # --- network ---
        nf = ttk.LabelFrame(main, text=" Network ", padding=8)
        nf.pack(fill="x", pady=(10, 0))
        self.pingmode_var = tk.StringVar(value=cfg.get("ping_mode", "auto"))
        ttk.Radiobutton(nf, text="Auto-detect game server (recommended)", value="auto",
                        variable=self.pingmode_var,
                        command=lambda: self._set("ping_mode", "auto")
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(nf, text="Custom host:", value="manual",
                        variable=self.pingmode_var,
                        command=lambda: self._set("ping_mode", "manual")
                        ).grid(row=1, column=0, sticky="w")
        self.host_var = tk.StringVar(value=cfg["ping_host"])
        he = ttk.Entry(nf, textvariable=self.host_var, width=18)
        he.grid(row=1, column=1, sticky="w", padx=8)
        he.bind("<Return>", lambda e: self._set("ping_host", self.host_var.get().strip()))
        he.bind("<FocusOut>", lambda e: self._set("ping_host", self.host_var.get().strip()))
        ttk.Label(nf, text="Auto pings the server your game is connected to, and\n"
                           "shows -- when you're not in a game. The status bar\n"
                           "below shows which address is used.",
                  foreground="#888888").grid(row=2, column=0, columnspan=2, sticky="w")

        # --- buttons + status ---
        bf = ttk.Frame(main)
        bf.pack(fill="x", pady=(12, 0))
        self.toggle_btn = ttk.Button(bf, text="Hide overlay", command=self._toggle_overlay)
        self.toggle_btn.pack(side="left")
        ttk.Button(bf, text="Quit", command=self.root.destroy).pack(side="right")

        self.status = ttk.Label(main, text="", foreground="#888888", wraplength=320,
                                justify="left")
        self.status.pack(fill="x", pady=(10, 0))
        self._update_status()

    # -- callbacks --
    def _toggle(self, metric):
        self.cfg["metrics"][metric]["enabled"] = self.metric_vars[metric].get()
        self._apply()

    def _pick_color(self, metric):
        _, hexcol = colorchooser.askcolor(self.cfg["metrics"][metric]["color"],
                                          title="Colour for %s" % METRIC_NAMES[metric])
        if hexcol:
            self.cfg["metrics"][metric]["color"] = hexcol
            self.swatches[metric].config(bg=hexcol, activebackground=hexcol)
            self._apply()

    def _pick_bg(self):
        _, hexcol = colorchooser.askcolor(self.cfg["bg_color"], title="Background colour")
        if hexcol:
            self.cfg["bg_color"] = hexcol
            self.bg_swatch.config(bg=hexcol, activebackground=hexcol)
            self._apply()

    def _set(self, key, value):
        self.cfg[key] = value
        self._apply()

    def _set_size(self):
        try:
            size = max(6, min(72, int(self.size_var.get())))
        except tk.TclError:
            return  # ignore non-numeric input while typing
        self._set("font_size", size)

    def _apply(self):
        save_config(self.cfg)
        self.overlay.rebuild()

    def _toggle_overlay(self):
        show = not self.overlay.visible
        self.overlay.set_visible(show)
        self.toggle_btn.config(text="Hide overlay" if show else "Show overlay")

    def _update_status(self):
        admin = bool(shell32.IsUserAnAdmin())
        parts = [
            "Admin: %s" % ("yes" if admin else "NO - temps/FPS may be unavailable"),
            "Sensors: %s" % STATS.hw_status,
            "FPS: %s" % STATS.fps_status,
            "Ping: %s" % STATS.ping_status,
        ]
        self.status.config(text="  |  ".join(parts))
        self.root.after(2000, self._update_status)

# ------------------------------------------------------------- elevation ---

def ensure_admin():
    """Relaunch elevated (UAC prompt) unless already admin. Returns admin state."""
    try:
        is_admin = bool(shell32.IsUserAnAdmin())
    except OSError:
        is_admin = False
    argv = sys.argv[1:]
    if is_admin or "--elevated" in argv or "--no-elevate" in argv or "--selftest" in argv:
        return is_admin
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, "--elevated"
    else:
        exe = sys.executable
        params = '"%s" --elevated' % os.path.abspath(__file__)
    ret = shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if ret > 32:  # elevated copy launched successfully
        sys.exit(0)
    return False  # user declined UAC - run degraded

# ---------------------------------------------------------------- main -----

def start_workers(cfg):
    workers = [PingWorker(cfg), HardwareWorker(), FpsWorker()]
    for w in workers:
        w.start()
    return workers


def stop_workers(workers):
    RUNNING.clear()
    for w in workers:
        if hasattr(w, "stop"):
            w.stop()


def selftest():
    """Print a full diagnostic. Run with --selftest and send the output when
    something is not reading on a machine you cannot poke at directly."""
    if sys.stdout is None:  # windowed exe has no console - log to a file
        os.makedirs(CONFIG_DIR, exist_ok=True)
        sys.stdout = open(os.path.join(CONFIG_DIR, "selftest.txt"), "w", encoding="utf-8")

    try:
        admin = bool(shell32.IsUserAnAdmin())
    except OSError:
        admin = False
    print("=== environment ===")
    print("admin rights   :", "YES" if admin else "NO  <-- CPU temp, FPS and ping need this")
    print("python         :", sys.version.split()[0], "(frozen exe)" if
          getattr(sys, "frozen", False) else "(from source)")
    for rel in ("bin/HardwareMonitor.exe", "bin/PresentMon.exe"):
        p = resource_path(rel.replace("/", os.sep))
        print("%-15s: %s" % (rel.split("/")[-1], "found" if os.path.exists(p) else "MISSING"))
    try:
        cpu_name = ""
        if psutil:
            import platform
            cpu_name = platform.processor()
        print("cpu            :", cpu_name or "unknown")
    except Exception:
        pass
    wg = WinGpu()
    print("gpu (windows)  :", wg.name or "not detected",
          "temp=%s load=%s" % (wg.temperature(), wg.load()))

    cfg = load_config()
    workers = start_workers(cfg)
    print("\ncollecting for 8 seconds...")
    time.sleep(8)
    stats = STATS.get()
    print("stats:", json.dumps({k: (round(v, 1) if isinstance(v, float) else v)
                                for k, v in stats.items()}, indent=2))
    print("ping_status:", STATS.ping_status)
    print("hw_status:  ", STATS.hw_status)
    print("fps_status: ", STATS.fps_status)
    stop_workers(workers)
    print("\n(if a value is null: CPU temp needs admin; ping/loss only read"
          " during an actual match)")


def main():
    if "--selftest" in sys.argv[1:]:
        selftest()
        return

    ensure_admin()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass

    cfg = load_config()
    workers = start_workers(cfg)
    atexit.register(stop_workers, workers)

    root = tk.Tk()
    overlay = OverlayWindow(root, cfg)
    SettingsWindow(root, cfg, overlay, workers)

    def tick():
        overlay.refresh()
        root.after(1000, tick)

    tick()
    try:
        root.mainloop()
    finally:
        stop_workers(workers)


if __name__ == "__main__":
    main()
