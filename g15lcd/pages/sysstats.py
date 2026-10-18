"""System stats page: CPU/RAM/GPU/network, sampled in a background task."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image

from ..render import draw_for, hbar, load_font, new_canvas, sparkline, text
from . import Page, register

log = logging.getLogger(__name__)

HISTORY = 40
_CPU_TEMP_PREF = (("k10temp", "Tctl"), ("zenpower", "Tdie"), ("coretemp", "Package"))


@dataclass
class Stats:
    cpu: float = 0.0
    cpu_hist: list[float] = field(default_factory=list)
    per_core: list[float] = field(default_factory=list)
    ram_used: int = 0
    ram_total: int = 0
    cpu_temp: float | None = None
    gpu_busy: float | None = None
    gpu_temp: float | None = None
    rx: float = 0.0  # bytes/s
    tx: float = 0.0


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_gpu() -> tuple[str | None, str | None]:
    """(busy_percent path, temp1_input path) of the biggest amdgpu, if any."""
    best, best_vram = (None, None), -1
    for dev in glob.glob("/sys/class/drm/card[0-9]*/device"):
        temps = glob.glob(dev + "/hwmon/hwmon*/temp1_input")
        busy = dev + "/gpu_busy_percent"
        if not os.path.exists(busy):
            busy = None
        if not temps and not busy:
            continue
        vram = int(_read(dev + "/mem_info_vram_total") or 0)
        if vram > best_vram:
            best, best_vram = (busy, temps[0] if temps else None), vram
    return best


class Sampler:
    """Collects Stats via psutil/sysfs. Cheap, but called outside render()."""

    def __init__(self) -> None:
        import psutil
        self.ps = psutil
        self.hist: deque[float] = deque(maxlen=HISTORY)
        self.gpu_busy_path, self.gpu_temp_path = find_gpu()
        self.last_net: tuple[float, int, int] | None = None
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)

    def _cpu_temp(self) -> float | None:
        try:
            temps = self.ps.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for chip, label in _CPU_TEMP_PREF:
            for t in temps.get(chip, []):
                if t.label == label:
                    return t.current
        return None

    def sample(self) -> Stats:
        ps = self.ps
        cpu = ps.cpu_percent(None)
        self.hist.append(cpu)
        vm = ps.virtual_memory()
        s = Stats(cpu=cpu, cpu_hist=list(self.hist), per_core=ps.cpu_percent(None, percpu=True),
                  ram_used=vm.total - vm.available, ram_total=vm.total, cpu_temp=self._cpu_temp())
        if self.gpu_busy_path and (v := _read(self.gpu_busy_path)):
            s.gpu_busy = float(v)
        if self.gpu_temp_path and (v := _read(self.gpu_temp_path)):
            s.gpu_temp = int(v) / 1000
        now = time.monotonic()
        rx = tx = 0
        for nic, c in ps.net_io_counters(pernic=True).items():
            if nic != "lo":
                rx += c.bytes_recv
                tx += c.bytes_sent
        if self.last_net:
            t0, rx0, tx0 = self.last_net
            dt = max(now - t0, 1e-3)
            s.rx, s.tx = max(0, rx - rx0) / dt, max(0, tx - tx0) / dt
        self.last_net = (now, rx, tx)
        return s


def fmt_rate(bps: float) -> str:
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if bps >= div:
            v = bps / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{bps:.0f}B"


def fmt_gib(n: int) -> str:
    return f"{n / (1 << 30):.1f}"


def draw_pct(d, x: int, y: int) -> None:
    """Hand-drawn 6x8 percent sign: no font renders '%' cleanly at 10px in 1-bit."""
    d.rectangle((x, y + 1, x + 1, y + 2), fill=1)        # upper-left ring
    d.rectangle((x + 4, y + 7, x + 5, y + 8), fill=1)    # lower-right ring
    d.line((x + 5, y + 1, x, y + 8), fill=1)             # slash


def text_pct(d, xy, s: str, f) -> None:
    """Draw ``s`` with each '%' replaced by draw_pct (6px advance, like the mono font)."""
    x, y = xy
    for i, part in enumerate(s.split("%")):
        if i:
            draw_pct(d, x, y)
            x += 6
        text(d, (x, y), part, f)
        x += round(d.textlength(part, font=f))


@register
class SysStatsPage(Page):
    name = "sysstats"
    interval = 1.0

    def __init__(self, sampler=None) -> None:
        self.sampler = sampler  # any object with .sample() -> Stats
        self.stats = Stats()
        self.view = 0  # 0 = summary, 1 = per-core bars
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.sampler is None:
            try:
                self.sampler = Sampler()
            except Exception:
                log.exception("sysstats: sampler unavailable")
                return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                self.stats = self.sampler.sample()
            except Exception:
                log.exception("sysstats: sample failed")
            await asyncio.sleep(1.0)

    def on_button(self, name: str) -> bool:
        if name in ("MENU4", "MENU5"):
            self.view = (self.view + (1 if name == "MENU5" else -1)) % 2
            return True
        return False

    def render(self, now: datetime) -> Image.Image:
        img = new_canvas()
        d = draw_for(img)
        s = self.stats
        # Regular weight: bold smudges N/E/T at 10px. '%' is hand-drawn (text_pct).
        f = fb = load_font(10)
        if self.view == 1:
            self._render_cores(d, s, f)
            return img
        # row 1: CPU
        t = f"{s.cpu_temp:.0f}°C" if s.cpu_temp is not None else ""
        text(d, (0, 0), "CPU", fb)
        text_pct(d, (24, 0), f"{s.cpu:3.0f}% {t}", f)
        sparkline(d, (96, 1, 159, 9), s.cpu_hist, 0, 100)
        d.line((96, 10, 159, 10), fill=1)
        # row 2: RAM
        text(d, (0, 11), "RAM", fb)
        text(d, (24, 11), f"{fmt_gib(s.ram_used)}/{fmt_gib(s.ram_total)}G", f)
        frac = s.ram_used / s.ram_total if s.ram_total else 0
        hbar(d, (96, 12, 159, 20), frac)
        # row 3: GPU
        text(d, (0, 22), "GPU", fb)
        gb = f"{s.gpu_busy:3.0f}%" if s.gpu_busy is not None else " n/a"
        gt = f" {s.gpu_temp:.0f}°C" if s.gpu_temp is not None else ""
        text_pct(d, (24, 22), gb + gt, f)
        hbar(d, (96, 23, 159, 31), (s.gpu_busy or 0) / 100)
        # row 4: net
        text(d, (0, 33), "NET", fb)
        d.polygon([(24, 34), (28, 34), (26, 40)], fill=1)  # down arrow head
        text(d, (30, 33), fmt_rate(s.rx), f)
        d.polygon([(82, 40), (86, 40), (84, 34)], fill=1)  # up arrow head
        text(d, (88, 33), fmt_rate(s.tx), f)
        return img

    def _render_cores(self, d, s: Stats, f) -> None:
        cores = s.per_core or [0.0]
        n = len(cores)
        cols = min(n, 16)
        rows = -(-n // cols)
        bw = 160 // cols
        top, h = 0, 33
        rh = h // rows
        for i, v in enumerate(cores):
            r, c = divmod(i, cols)
            x0 = c * bw + 1
            y1 = top + r * rh + rh - 2
            hh = round((rh - 3) * v / 100)
            d.rectangle((x0, top + r * rh, x0 + bw - 3, y1), outline=1)
            if hh > 0:
                d.rectangle((x0, y1 - hh, x0 + bw - 3, y1), fill=1)
        text_pct(d, (0, 33), f"CPU {s.cpu:.0f}%  {n} threads", f)
