#!/usr/bin/env python3
"""Radio Shack - an old-school terminal radio for live stations worldwide.

Station data comes from the open Radio Browser directory (radio-browser.info).
Audio is played by mpv running in the background.
"""
from __future__ import annotations

import glob
import json
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

API_HOSTS = [f"https://{h}.api.radio-browser.info" for h in ("de1", "fi1", "at1", "nl1", "de2")]
USER_AGENT = "RadioShack/0.1 (terminal radio)"
CONFIG = Path.home() / ".radioshack.json"
DEFAULT_COUNTRY = "ZA"
FM_LO, FM_HI = 87.5, 108.0
TUNE_SETTLE = 0.45  # seconds the needle must rest before a station locks in

AMBER = "#ffbf47"
AMBER_DIM = "#8a6a33"
AMBER_FAINT = "#4a3a20"
NEEDLE = "#ff5f45"
LIVE = "#7ee787"
NOISE = " .:-=+*#%░▒▓"
BARS = "▁▂▃▄▅▆▇█"


def clean(s) -> str:
    return " ".join(str(s or "").split())


# --------------------------------------------------------------------------- directory

def to_station(s: dict) -> dict:
    return {
        "id": s.get("stationuuid"),
        "name": clean(s.get("name")),
        "url": s.get("url_resolved") or s.get("url"),
        "cc": (s.get("countrycode") or "").upper(),
        "country": clean(s.get("country")),
        "state": clean(s.get("state")),
        "tags": ", ".join([t for t in clean(s.get("tags")).split(",") if t.strip()][:3]),
        "codec": s.get("codec") or "",
        "bitrate": s.get("bitrate") or 0,
    }


def dedupe(rows: list[dict]) -> list[dict]:
    """Drop broken rows and repeat listings of the same station (same name, same country)."""
    out, seen = [], set()
    for s in rows:
        st = to_station(s)
        key = (st["name"].lower(), st["cc"])
        if st["id"] and st["name"] and st["url"] and key not in seen:
            seen.add(key)
            out.append(st)
    return out


class Directory:
    def __init__(self):
        self.host: str | None = None

    def get(self, path: str):
        hosts = [self.host] + [h for h in API_HOSTS if h != self.host] if self.host else API_HOSTS
        last = None
        for h in hosts:
            try:
                with urlopen(Request(h + path, headers={"User-Agent": USER_AGENT}), timeout=15) as r:
                    data = json.load(r)
                self.host = h
                return data
            except Exception as e:  # try the next mirror
                last = e
        raise RuntimeError(f"station directory unreachable ({last})")

    def countries(self) -> list[dict]:
        best: dict[str, dict] = {}
        for c in self.get("/json/countries?hidebroken=true"):
            cc = (c.get("iso_3166_1") or "").upper()
            n = c.get("stationcount") or 0
            if len(cc) == 2 and n > 0 and c.get("name"):
                if cc not in best or n > best[cc]["count"]:
                    best[cc] = {"cc": cc, "name": clean(c["name"]), "count": n}
        return sorted(best.values(), key=lambda c: c["name"].lower())

    def stations(self, cc: str) -> list[dict]:
        return dedupe(self.get(
            f"/json/stations/bycountrycodeexact/{quote(cc)}?hidebroken=true&order=clickcount&reverse=true"))

    def search(self, q: str) -> list[dict]:
        opts = "hidebroken=true&order=clickcount&reverse=true"
        enc = quote(q)
        rows: list[dict] = []
        for field, limit in (("name", 100), ("tag", 100), ("country", 300)):
            try:
                rows += self.get(f"/json/stations/search?{field}={enc}&{opts}&limit={limit}")
            except RuntimeError:
                pass
        return dedupe(rows)[:300]

    def click(self, station_id: str) -> None:
        """Count a listen, as the directory asks client apps to do."""
        try:
            self.get(f"/json/url/{quote(station_id)}")
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- audio

def find_mpv() -> str | None:
    found = shutil.which("mpv")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        rf"{local}\Microsoft\WinGet\Links\mpv.exe",
        rf"{local}\Microsoft\WinGet\Packages\shinchiro.mpv*\**\mpv.exe",
        r"C:\Program Files\MPV Player\mpv.exe",
        r"C:\Program Files\mpv\mpv.exe",
        str(Path.home() / "scoop" / "shims" / "mpv.exe"),
    ] if os.name == "nt" else ["/opt/homebrew/bin/mpv", "/usr/local/bin/mpv"]
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


class Player:
    """Drives a background mpv process over its JSON IPC channel."""

    def __init__(self, volume: int):
        self.exe = find_mpv()
        self.volume = volume
        self.proc: subprocess.Popen | None = None
        self.ipc = (rf"\\.\pipe\radioshack-{os.getpid()}" if os.name == "nt"
                    else f"/tmp/radioshack-{os.getpid()}.sock")
        self._rid = 0
        self._lock = threading.Lock()

    def _send(self, *command):
        with self._lock:
            self._rid += 1
            rid = self._rid
            payload = (json.dumps({"command": list(command), "request_id": rid}) + "\n").encode()
            if os.name == "nt":
                with open(self.ipc, "r+b", buffering=0) as pipe:
                    pipe.write(payload)
                    while line := pipe.readline():
                        msg = json.loads(line)
                        if msg.get("request_id") == rid:
                            return msg
            else:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(3)
                    sock.connect(self.ipc)
                    sock.sendall(payload)
                    for line in sock.makefile("rb"):
                        msg = json.loads(line)
                        if msg.get("request_id") == rid:
                            return msg
            return {}

    def command(self, *command):
        if not self.exe:
            return {}
        self.ensure()
        for _ in range(3):
            try:
                return self._send(*command)
            except OSError:  # pipe busy or mpv still starting
                time.sleep(0.15)
        return {}

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [self.exe, "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
             f"--input-ipc-server={self.ipc}", f"--volume={self.volume}",
             "--cache=yes", "--cache-secs=8", f"--user-agent={USER_AGENT}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                self._send("get_property", "pid")
                return
            except OSError:
                time.sleep(0.1)

    def play(self, url: str):
        self.command("set_property", "pause", False)
        self.command("loadfile", url, "replace")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.command("stop")

    def set_volume(self, v: int):
        self.volume = v
        if self.proc and self.proc.poll() is None:
            self.command("set_property", "volume", v)

    def status(self) -> dict:
        if not (self.proc and self.proc.poll() is None):
            return {}
        prop = lambda name: self.command("get_property", name).get("data")
        meta = prop("metadata") or {}
        title = next((v for k, v in meta.items() if k.lower() == "icy-title"), "")
        return {
            "idle": bool(prop("idle-active")),
            "paused": bool(prop("pause")),
            "buffering": bool(prop("paused-for-cache")) or bool(prop("core-idle")),
            "title": clean(title),
        }

    def quit(self):
        if self.proc and self.proc.poll() is None:
            try:
                self._send("quit")
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


# --------------------------------------------------------------------------- pickers

PICKER_CSS = """
%s { align: center middle; background: rgba(0,0,0,0.65); }
#box { width: 80%%; max-width: 96; height: 80%%; border: round #ffbf47; background: #15110d; padding: 1 2; }
#box Input { margin-bottom: 1; border: tall #4a3a20; background: #0d0a06; }
#box Input:focus { border: tall #ffbf47; }
#hint { color: #8a6a33; height: 1; margin-bottom: 1; }
#box ListView { background: #15110d; }
#box ListItem { background: #15110d; padding: 0 1; }
#box ListItem.-highlight { background: #3a2c14; }
"""


class SearchScreen(ModalScreen):
    CSS = PICKER_CSS % "SearchScreen"
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, directory: Directory):
        super().__init__()
        self.directory = directory

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Input(placeholder="Station, city, genre or country - then Enter", id="q")
            yield Label("Searches every station in the directory", id="hint")
            yield ListView(id="results")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        q = event.value.strip()
        if len(q) >= 2:
            self.query_one("#hint", Label).update("Searching...")
            self.run_search(q)

    @work(thread=True, exclusive=True)
    def run_search(self, q: str) -> None:
        results = self.directory.search(q)
        self.app.call_from_thread(self.show, q, results)

    def show(self, q: str, results: list[dict]) -> None:
        lv = self.query_one("#results", ListView)
        lv.clear()
        for st in results:
            where = ", ".join(x for x in (st["state"], st["country"]) if x)
            line = Text(st["name"], style=f"bold {AMBER}")
            line.append(f"   {where}" + (f" · {st['tags']}" if st["tags"] else ""), style=AMBER_DIM)
            item = ListItem(Label(line))
            item.station = st
            lv.append(item)
        n = len(results)
        self.query_one("#hint", Label).update(
            f"{n} station{'s' if n != 1 else ''} for \"{q}\" - Enter to tune in" if n else f"Nothing found for \"{q}\"")
        if n:
            lv.index = 0
            lv.focus()

    @on(ListView.Selected)
    def picked(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.station)

    def action_close(self) -> None:
        self.dismiss(None)


class BandScreen(ModalScreen):
    CSS = PICKER_CSS % "BandScreen"
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, countries: list[dict], current: str):
        super().__init__()
        self.countries = countries
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Input(placeholder="Type a country", id="q")
            yield Label("Enter picks the first match, arrows to browse", id="hint")
            yield ListView(id="results")

    def on_mount(self) -> None:
        self.fill("")

    def fill(self, q: str) -> None:
        lv = self.query_one("#results", ListView)
        lv.clear()
        q = q.lower()
        for c in self.countries:
            if q in c["name"].lower() or q == c["cc"].lower():
                line = Text(c["name"], style=f"bold {AMBER}")
                line.append(f"   {c['count']} stations", style=AMBER_DIM)
                item = ListItem(Label(line))
                item.cc = c["cc"]
                lv.append(item)
        if lv.children:
            lv.index = 0

    @on(Input.Changed)
    def changed(self, event: Input.Changed) -> None:
        self.fill(event.value.strip())

    @on(Input.Submitted)
    def submitted(self) -> None:
        lv = self.query_one("#results", ListView)
        if lv.children:
            self.dismiss(lv.children[0].cc)

    @on(ListView.Selected)
    def picked(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.cc)

    def action_close(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- the radio

class RadioShack(App):
    TITLE = "Radio Shack"
    CSS = """
    Screen { background: #0a0b0d; align: center middle; }
    #radio {
        width: 100%; max-width: 112; height: auto;
        border: heavy #6b4a2b; border-title-color: #ffbf47; border-title-style: bold;
        border-subtitle-color: #8a6a33;
        background: #15110d; padding: 1 2;
    }
    #display { height: 6; padding: 0 1; border: round #3d2f1c; background: #0d0a06; }
    #dial { height: 3; margin-top: 1; }
    #presets { height: 1; margin-top: 1; }
    #meter { height: 1; margin-top: 1; }
    Footer { background: #15110d; }
    """
    BINDINGS = [
        Binding("left", "tune(-1)", "Tune"),
        Binding("right", "tune(1)", "Tune", show=False),
        Binding("pageup", "tune(-10)", "Skip 10", show=False),
        Binding("pagedown", "tune(10)", "Skip 10", show=False),
        Binding("up", "band(-1)", "Country"),
        Binding("down", "band(1)", "Country", show=False),
        Binding("b", "pick_band", "Countries"),
        Binding("slash", "search", "Search"),
        Binding("space", "pause", "Pause"),
        Binding("plus,equals_sign", "volume(5)", "Vol +"),
        Binding("minus", "volume(-5)", "Vol -"),
        Binding("m", "memory", "Save preset"),
        *[Binding(str(i), f"preset({i})", f"Preset {i}", show=False) for i in range(1, 7)],
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.config = self.load_config()
        self.directory = Directory()
        self.player = Player(self.config.get("volume", 70))
        self.audio = ThreadPoolExecutor(max_workers=1)  # keeps player commands in order
        self.net = ThreadPoolExecutor(max_workers=2)
        self.countries: list[dict] = []
        self.cache: dict[str, list[dict]] = {}
        self.cc = self.config.get("country", DEFAULT_COUNTRY)
        self.stations: list[dict] = []
        self.idx = 0
        self.last_idx: dict[str, int] = {}
        self.focus_id = self.config.get("station")
        self.presets: dict[str, dict] = self.config.get("presets", {})
        self.tuning = True
        self.scanning = True
        self.error = ""
        self.memory_mode = False
        self.playing_id: str | None = None
        self.play_started = 0.0
        self.status: dict = {}
        self._poll_busy = False
        self._settle_timer = None
        self.frame = 0

    # ---- config
    @staticmethod
    def load_config() -> dict:
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_config(self) -> None:
        st = self.station
        data = {"volume": self.player.volume, "country": self.cc, "presets": self.presets,
                "station": st["id"] if st else None}
        try:
            CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- layout
    def compose(self) -> ComposeResult:
        with Vertical(id="radio"):
            yield Static(id="display")
            yield Static(id="dial")
            yield Static(id="presets")
            yield Static(id="meter")
        yield Footer()

    def on_mount(self) -> None:
        radio = self.query_one("#radio")
        radio.border_title = " R A D I O   S H A C K "
        radio.border_subtitle = " world band receiver "
        self.set_interval(0.12, self.animate)
        self.set_interval(1.0, self.poll)
        if self.player.exe:
            self.audio.submit(self.player.ensure)
        self.load_countries()

    def on_unmount(self) -> None:
        self.save_config()
        self.player.quit()
        self.audio.shutdown(wait=False, cancel_futures=True)
        self.net.shutdown(wait=False, cancel_futures=True)

    # ---- state helpers
    @property
    def station(self) -> dict | None:
        return self.stations[self.idx] if self.stations else None

    @property
    def country(self) -> dict:
        return next((c for c in self.countries if c["cc"] == self.cc),
                    {"cc": self.cc, "name": self.cc or "Unknown", "count": 0})

    def freq(self, i: int) -> float:
        n = len(self.stations)
        return FM_LO + (FM_HI - FM_LO) * (i / (n - 1) if n > 1 else 0.5)

    # ---- loading
    @work(thread=True, exclusive=True, group="countries")
    def load_countries(self) -> None:
        try:
            countries = self.directory.countries()
        except RuntimeError as e:
            self.call_from_thread(self.failed, str(e))
            return
        self.call_from_thread(self.countries_loaded, countries)

    def countries_loaded(self, countries: list[dict]) -> None:
        self.countries = countries
        if not any(c["cc"] == self.cc for c in countries):
            self.cc = DEFAULT_COUNTRY
        self.switch_band(self.cc, focus_id=self.focus_id)

    def failed(self, msg: str) -> None:
        self.error = msg
        self.scanning = self.tuning = False

    def switch_band(self, cc: str, focus_id: str | None = None, fallback: dict | None = None) -> None:
        if self.stations:
            self.last_idx[self.cc] = self.idx
        self.cc = cc
        self.error = ""
        self.scanning = self.tuning = True
        self.stations = []
        self.stop_audio()
        self.load_band(cc, focus_id, fallback)

    @work(thread=True, exclusive=True, group="band")
    def load_band(self, cc: str, focus_id: str | None, fallback: dict | None) -> None:
        try:
            stations = self.cache.get(cc) or self.directory.stations(cc)
        except RuntimeError as e:
            self.call_from_thread(self.failed, str(e))
            return
        self.cache[cc] = stations
        self.call_from_thread(self.band_loaded, cc, stations, focus_id, fallback)

    def band_loaded(self, cc: str, stations: list[dict], focus_id, fallback) -> None:
        if cc != self.cc:
            return  # the dial already moved on
        stations = list(stations)
        idx = self.last_idx.get(cc, 0)
        if focus_id:
            found = next((i for i, s in enumerate(stations) if s["id"] == focus_id), None)
            if found is None and fallback:
                stations.insert(0, fallback)
                self.cache[cc] = stations
                found = 0
            idx = found if found is not None else idx
        self.stations = stations
        self.idx = max(0, min(idx, len(stations) - 1))
        self.scanning = False
        if not stations:
            self.failed(f"no working stations listed for {self.country['name']}")
            return
        self.lock_in()

    # ---- tuning
    def action_tune(self, delta: int) -> None:
        if not self.stations or self.scanning:
            return
        self.memory_mode = False
        self.idx = (self.idx + delta) % len(self.stations)
        if not self.tuning:
            self.stop_audio()
        self.tuning = True
        if self._settle_timer:
            self._settle_timer.stop()
        self._settle_timer = self.set_timer(TUNE_SETTLE, self.lock_in)

    def action_band(self, delta: int) -> None:
        if not self.countries:
            return
        i = next((i for i, c in enumerate(self.countries) if c["cc"] == self.cc), 0)
        self.switch_band(self.countries[(i + delta) % len(self.countries)]["cc"])

    def lock_in(self) -> None:
        st = self.station
        self.tuning = False
        if not st:
            return
        self.playing_id = st["id"]
        self.play_started = time.monotonic()
        self.status = {}
        self.save_config()
        if self.player.exe:
            self.audio.submit(self.player.play, st["url"])
            self.net.submit(self.directory.click, st["id"])

    def stop_audio(self) -> None:
        self.playing_id = None
        self.status = {}
        if self.player.exe:
            self.audio.submit(self.player.stop)

    # ---- actions
    def action_pause(self) -> None:
        if self.playing_id and self.player.exe:
            self.audio.submit(self.player.command, "cycle", "pause")

    def action_volume(self, delta: int) -> None:
        v = max(0, min(130, self.player.volume + delta))
        self.audio.submit(self.player.set_volume, v)
        self.player.volume = v
        self.save_config()

    def action_memory(self) -> None:
        self.memory_mode = not self.memory_mode and self.station is not None

    def action_preset(self, n: int) -> None:
        key = str(n)
        if self.memory_mode and self.station:
            self.presets[key] = self.station
            self.memory_mode = False
            self.save_config()
            self.notify(f"Saved {self.station['name']} to preset {n}", timeout=2)
        elif key in self.presets:
            self.go_to(self.presets[key])

    def action_search(self) -> None:
        self.push_screen(SearchScreen(self.directory), self.go_to)

    def action_pick_band(self) -> None:
        if self.countries:
            self.push_screen(BandScreen(self.countries, self.cc),
                             lambda cc: cc and self.switch_band(cc))

    def go_to(self, st: dict | None) -> None:
        if not st:
            return
        if st["cc"] == self.cc and self.stations:
            i = next((i for i, s in enumerate(self.stations) if s["id"] == st["id"]), None)
            if i is not None:
                self.idx = i
                self.stop_audio()
                self.lock_in()
                return
        self.switch_band(st["cc"], focus_id=st["id"], fallback=st)

    # ---- status polling
    def poll(self) -> None:
        if not self.playing_id or self._poll_busy or not self.player.exe:
            return
        self._poll_busy = True
        playing = self.playing_id

        def job():
            try:
                status = self.player.status()
            finally:
                self._poll_busy = False
            if playing == self.playing_id:
                self.call_from_thread(setattr, self, "status", status)

        self.audio.submit(job)

    # ---- drawing
    def animate(self) -> None:
        self.frame += 1
        self.query_one("#display", Static).update(self.render_display())
        width = self.query_one("#dial", Static).size.width or 80
        self.query_one("#dial", Static).update(self.render_dial(width))
        self.query_one("#presets", Static).update(self.render_presets(width))
        self.query_one("#meter", Static).update(self.render_meter(width))

    def signal_state(self) -> tuple[str, str]:
        if not self.player.exe:
            return "NO AUDIO", NEEDLE
        if self.error:
            return "NO SIGNAL", NEEDLE
        if self.scanning:
            return "SCANNING", AMBER_DIM
        if self.tuning:
            return "TUNING", AMBER_DIM
        s = self.status
        if s.get("paused"):
            return "PAUSED", AMBER_DIM
        if s and not s.get("idle") and not s.get("buffering"):
            return "● LIVE", LIVE
        if s.get("idle") and time.monotonic() - self.play_started > 5:
            return "NO SIGNAL", NEEDLE
        return "CONNECTING", AMBER_DIM

    def noise(self, n: int) -> str:
        return "".join(random.choice(NOISE) for _ in range(max(n, 0)))

    def marquee(self, text: str, width: int) -> str:
        if len(text) <= width:
            return text
        loop = text + "   ·   "
        start = (self.frame // 3) % len(loop)
        return (loop + loop)[start:start + width]

    def render_display(self):
        label, colour = self.signal_state()
        st = self.station
        width = max((self.query_one("#display", Static).size.width or 80) - 18, 10)
        n = len(self.stations)
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True)
        grid.add_column(justify="right", no_wrap=True)

        band = f"{self.country['name'].upper()}  CH {self.idx + 1:03d}/{n:03d}" if n else self.country["name"].upper()
        grid.add_row(Text(label, style=f"bold {colour}"), Text(band, style=AMBER_DIM))

        if self.tuning or self.scanning:
            name = Text(self.noise(width), style=AMBER_FAINT)
        elif st:
            name = Text(self.marquee(st["name"].upper(), width), style=f"bold {AMBER}")
        else:
            name = Text("")
        freq = Text(f"{self.freq(self.idx):5.1f} MHz", style=f"bold {AMBER}") if n else Text("")
        grid.add_row(name, freq)

        if not self.player.exe:
            now = Text("mpv not found. Install it with:  winget install shinchiro.mpv", style=NEEDLE)
        elif self.error:
            now = Text(self.error, style=NEEDLE)
        elif self.status.get("title") and not self.tuning:
            now = Text("NOW  ", style=AMBER_DIM) + Text(self.marquee(self.status["title"], width), style=AMBER)
        else:
            now = Text("")
        grid.add_row(now, "")

        if st and not self.tuning:
            bits = [", ".join(x for x in (st["state"], st["country"]) if x), st["tags"],
                    f"{st['bitrate']} kbps" if st["bitrate"] else "", st["codec"]]
            grid.add_row(Text(" · ".join(b for b in bits if b), style=AMBER_DIM), "")
        else:
            grid.add_row("", "")
        return grid

    def render_dial(self, width: int):
        w = max(width, 30)
        col = lambda f: max(0, min(w - 1, round((f - FM_LO) / (FM_HI - FM_LO) * (w - 1))))
        labels = [" "] * w
        for mhz in range(88, 109, 2):
            s = str(mhz)
            start = max(0, min(w - len(s), col(mhz) - len(s) // 2))
            labels[start:start + len(s)] = s
        ticks = [" "] * w
        for mhz in range(88, 109):
            ticks[col(mhz)] = "│" if mhz % 2 == 0 else "╷"
        marks = ["─"] * w
        for i in range(len(self.stations)):
            marks[col(self.freq(i))] = "•"
        needle = col(self.freq(self.idx)) if self.stations else None

        def row(chars, base, needle_char=None):
            t = Text()
            for i, ch in enumerate(chars):
                if i == needle and needle_char:
                    t.append(needle_char, style=f"bold {NEEDLE}")
                else:
                    t.append(ch, style=AMBER if ch == "•" else base)
            return t

        return Group(row(labels, AMBER_DIM), row(ticks, AMBER_FAINT, "┃"), row(marks, AMBER_FAINT, "▲"))

    def render_presets(self, width: int):
        slot = max((width - 5) // 6, 6)
        t = Text()
        for n in range(1, 7):
            p = self.presets.get(str(n))
            name = p["name"] if p else "- empty -"
            cell = f" {n} {name}"[:slot - 1].ljust(slot - 1)
            if self.memory_mode:
                style = f"bold #15110d on {AMBER}"
            elif p and p["id"] == self.playing_id:
                style = f"bold #15110d on {AMBER_DIM}"
            else:
                style = AMBER if p else AMBER_FAINT
            t.append(cell, style=style)
            t.append(" ")
        return t

    def render_meter(self, width: int):
        t = Text()
        if self.memory_mode:
            return Text("MEMORY  press 1-6 to store this station, M to cancel", style=f"bold {AMBER}")
        v = self.player.volume
        cells = 12
        filled = round(v / 130 * cells)
        t.append("VOL ", style=AMBER_DIM)
        t.append("█" * filled, style=AMBER)
        t.append("░" * (cells - filled), style=AMBER_FAINT)
        t.append(f" {v:3d}%", style=AMBER_DIM)
        label, _ = self.signal_state()
        live = label == "● LIVE"
        t.append("      SIGNAL ", style=AMBER_DIM)
        level = "".join(random.choice(BARS[2:]) if live else ("▁" if not self.tuning else random.choice(BARS[:3]))
                        for _ in range(16))
        t.append(level, style=LIVE if live else AMBER_FAINT)
        return t


def main() -> None:
    RadioShack().run()


if __name__ == "__main__":
    main()
