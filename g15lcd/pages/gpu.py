"""GPU page: load, VRAM, temperatures, power and fan of an amdgpu, read from sysfs.

Sampled once a second in a background task; ``render()`` only reads the cache.
The two busy metrics (core and memory-controller bandwidth) are instantaneous and
bursty, so they are read at ~10 Hz and shown as a moving average.
Every sysfs file is optional (integrated GPUs expose fewer), so each value is
read defensively and shown as "--" when missing.

Only the amdgpu kernel driver is supported. NVIDIA (nvidia-smi / NVML) is out of
scope for now; without an amdgpu the page shows "No supported GPU".
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from PIL import Image

from ..render import (draw_for, ellipsize, hbar, load_font, new_canvas, sparkline, text,
                      text_pct)
from . import Page, register

log = logging.getLogger(__name__)

HISTORY = 64
DRM_ROOT = "/sys/class/drm"


@dataclass
class GpuStats:
    present: bool = True
    name: str = ""
    busy: float | None = None
    busy_hist: list[float] = field(default_factory=list)
    mem_busy: float | None = None
    vram_used: int | None = None
    vram_total: int | None = None
    gtt_used: int | None = None
    gtt_total: int | None = None
    temp_edge: float | None = None
    temp_junction: float | None = None
    temp_mem: float | None = None
    power: float | None = None      # W
    power_cap: float | None = None  # W
    fan_rpm: int | None = None
    fan_pct: float | None = None
    voltage: float | None = None    # V (vddgfx)
    sclk: float | None = None       # MHz
    mclk: float | None = None       # MHz
    pcie: str | None = None         # e.g. "16GT/s x16"


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _num(path: str) -> float | None:
    v = _read(path)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _scaled(path: str, div: float) -> float | None:
    v = _num(path)
    return None if v is None else v / div


def _starred(path: str) -> str | None:
    """The currently selected line ('*' marker) of a pp_dpm_* file."""
    v = _read(path)
    for line in (v or "").splitlines():
        if line.rstrip().endswith("*"):
            return line
    return None


def _dpm_mhz(path: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*mhz", _starred(path) or "", re.I)
    return float(m.group(1)) if m else None


def _pcie(path: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)GT/s,\s*x(\d+)", _starred(path) or "")
    return f"{float(m.group(1)):g}GT/s x{m.group(2)}" if m else None


def shorten_name(s: str) -> str:
    """'Radeon RX 9070 XT 16GB' -> 'RX 9070 XT'; 'Granite Ridge [Radeon 610M]' -> '610M'."""
    if m := re.search(r"\[([^\]]+)\]", s):
        s = m.group(1)
    s = re.sub(r"^(AMD|ATI|Radeon|\s)+", "", s)
    s = re.sub(r"\s+\d+\s*GB$", "", s, flags=re.I)
    return s.strip()


def _lspci_name(slot: str) -> str | None:
    """Short name from `lspci -s <slot> -mm` (prefers the board/subsystem name)."""
    try:
        out = subprocess.run(["lspci", "-s", slot, "-mm"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = re.findall(r'"([^"]*)"', out)  # class, vendor, device, [subvendor, subdevice]
    for cand in (fields[4:5] + fields[2:3]):
        if "radeon" in cand.lower() or "[" in cand:
            if (n := shorten_name(cand)):
                return n
    return None


def _is_amdgpu(dev: str) -> bool:
    if any(os.path.exists(f"{dev}/{f}") for f in ("gpu_busy_percent", "mem_info_vram_total")):
        return True
    return any(_read(h) == "amdgpu" for h in glob.glob(f"{dev}/hwmon/hwmon*/name"))


def find_card(card: str | int | None = None, root: str = DRM_ROOT) -> str | None:
    """sysfs ``device`` dir of the chosen amdgpu, or None."""
    if card is not None:
        name = str(card)
        name = name if name.startswith("card") else f"card{name}"
        dev = f"{root}/{name}/device"
        return dev if os.path.isdir(dev) and _is_amdgpu(dev) else None
    best, best_vram = None, -1
    for dev in sorted(glob.glob(f"{root}/card[0-9]*/device")):
        if not _is_amdgpu(dev):
            continue
        vram = int(_num(f"{dev}/mem_info_vram_total") or 0)
        if vram > best_vram:
            best, best_vram = dev, vram
    return best


class Sampler:
    """Reads one amdgpu's sysfs files into GpuStats."""

    def __init__(self, card: str | int | None = None, root: str = DRM_ROOT) -> None:
        self.dev = find_card(card, root)
        self.card = os.path.basename(os.path.dirname(self.dev)) if self.dev else ""
        self.hist: deque[float] = deque(maxlen=HISTORY)
        self.hwmon = ""
        self.name = ""
        if self.dev:
            hw = sorted(glob.glob(f"{self.dev}/hwmon/hwmon*"))
            self.hwmon = hw[0] if hw else ""
            self.name = self._fallback_name()

    def _fallback_name(self) -> str:
        n = self.card[4:] if self.card.startswith("card") else self.card
        return f"GPU {n}"

    def resolve_name(self) -> None:
        """Blocking (may run lspci); call from a thread, once."""
        if not self.dev:
            return
        if (pn := _read(f"{self.dev}/product_name")):
            self.name = shorten_name(pn) or self.name
            return
        slot = None
        for line in (_read(f"{self.dev}/uevent") or "").splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                slot = line.split("=", 1)[1]
        if slot and (n := _lspci_name(slot)):
            self.name = n

    def _temp(self, label: str, index: int) -> float | None:
        for p in glob.glob(f"{self.hwmon}/temp*_label"):
            if (_read(p) or "").lower() == label:
                return _scaled(p.replace("_label", "_input"), 1000)
        return _scaled(f"{self.hwmon}/temp{index}_input", 1000)

    def read_busy(self) -> tuple[float | None, float | None]:
        """Just the two cheap busy-percent files: (gpu, memory bandwidth)."""
        if not self.dev:
            return None, None
        return (_num(f"{self.dev}/gpu_busy_percent"), _num(f"{self.dev}/mem_busy_percent"))

    def sample(self) -> GpuStats:
        if not self.dev:
            return GpuStats(present=False)
        d, h = self.dev, self.hwmon
        s = GpuStats(name=self.name)
        s.busy = _num(f"{d}/gpu_busy_percent")
        if s.busy is not None:
            self.hist.append(s.busy)
        s.busy_hist = list(self.hist)
        s.mem_busy = _num(f"{d}/mem_busy_percent")
        for attr, f in (("vram_used", "mem_info_vram_used"), ("vram_total", "mem_info_vram_total"),
                        ("gtt_used", "mem_info_gtt_used"), ("gtt_total", "mem_info_gtt_total")):
            v = _num(f"{d}/{f}")
            setattr(s, attr, None if v is None else int(v))
        if h:
            s.temp_edge = self._temp("edge", 1)
            s.temp_junction = self._temp("junction", 2)
            s.temp_mem = self._temp("mem", 3)
            s.power = _scaled(f"{h}/power1_average", 1e6)
            if s.power is None:
                s.power = _scaled(f"{h}/power1_input", 1e6)
            s.power_cap = _scaled(f"{h}/power1_cap", 1e6)
            rpm = _num(f"{h}/fan1_input")
            s.fan_rpm = None if rpm is None else int(rpm)
            pwm, fmax = _num(f"{h}/pwm1"), _num(f"{h}/fan1_max")
            if pwm is not None:
                s.fan_pct = pwm * 100 / 255
            elif rpm is not None and fmax:
                s.fan_pct = rpm * 100 / fmax
            s.voltage = _scaled(f"{h}/in0_input", 1000)
            s.sclk = _scaled(f"{h}/freq1_input", 1e6)
            s.mclk = _scaled(f"{h}/freq2_input", 1e6)
        if s.sclk is None:
            s.sclk = _dpm_mhz(f"{d}/pp_dpm_sclk")
        if s.mclk is None:
            s.mclk = _dpm_mhz(f"{d}/pp_dpm_mclk")
        s.pcie = _pcie(f"{d}/pp_dpm_pcie")
        return s


def fmt_gib(n: int | None) -> str:
    return "--" if n is None else f"{n / (1 << 30):.1f}"


def _n(v: float | None, spec: str = ".0f", suffix: str = "") -> str:
    return "--" if v is None else f"{v:{spec}}{suffix}"


class MovingAverage:
    """Time-windowed mean; None samples are ignored."""

    def __init__(self, window: float) -> None:
        self.window = window
        self._q: deque[tuple[float, float]] = deque()

    def add(self, t: float, v: float | None) -> None:
        if v is not None:
            self._q.append((t, v))
        self._trim(t)

    def _trim(self, t: float) -> None:
        while self._q and self._q[0][0] < t - self.window:
            self._q.popleft()

    def mean(self, t: float) -> float | None:
        self._trim(t)
        return sum(v for _, v in self._q) / len(self._q) if self._q else None


def _valid(name: str, value, default: float, lo: float, hi: float = 1e6) -> float:
    ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
          and value == value and lo <= value <= hi)
    if not ok:
        log.warning("gpu: invalid %s=%r, using %s", name, value, default)
        return default
    return float(value)


@register
class GpuPage(Page):
    name = "gpu"
    interval = 1.0
    DEFAULT_RATE = 10.0    # Hz, busy metrics
    DEFAULT_WINDOW = 2.5   # s, busy averaging

    def __init__(self, card: str | int | None = None, sampler=None, interval: float = 1.0,
                 busy_rate: float = DEFAULT_RATE, busy_window: float = DEFAULT_WINDOW) -> None:
        self.card = card
        self.sampler = sampler  # .sample() -> GpuStats; optional .read_busy() -> (busy, mem)
        self.interval = float(interval)
        self.busy_rate = _valid("busy_rate", busy_rate, self.DEFAULT_RATE, 1.0, 50.0)
        self.busy_window = _valid("busy_window", busy_window, self.DEFAULT_WINDOW, 1e-3)
        self.stats = GpuStats()
        self.view = 0  # 0 = summary, 1 = detail
        self._task: asyncio.Task | None = None
        self._mono = time.monotonic  # injectable clock
        self._busy = MovingAverage(self.busy_window)
        self._mem = MovingAverage(self.busy_window)
        self._hist: deque[float] = deque(maxlen=HISTORY)
        self._last_full: float | None = None

    async def start(self) -> None:
        if self.sampler is None:
            try:
                self.sampler = Sampler(self.card)
                await asyncio.to_thread(self.sampler.resolve_name)  # lspci subprocess
            except Exception:
                log.exception("gpu: sampler unavailable")
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

    def _tick(self) -> None:
        """One fast step: busy files every call, the full sample about once a second."""
        t = self._mono()
        full = self._last_full is None or t - self._last_full >= 1.0 - 0.5 / self.busy_rate
        if full:
            self._last_full = t
            self.stats = st = self.sampler.sample()
            self._busy.add(t, st.busy)
            self._mem.add(t, st.mem_busy)
            if st.present and (avg := self._busy.mean(t)) is not None:
                self._hist.append(avg)  # sparkline: smoothed, once per second
        elif (rb := getattr(self.sampler, "read_busy", None)):
            b, m = rb()
            self._busy.add(t, b)
            self._mem.add(t, m)

    async def _loop(self) -> None:
        period = 1.0 / self.busy_rate
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("gpu: sample failed")
            await asyncio.sleep(period)

    def on_button(self, name: str) -> bool:
        if name in ("MENU4", "MENU5"):
            self.view = (self.view + (1 if name == "MENU5" else -1)) % 2
            return True
        return False

    def render(self, now: datetime) -> Image.Image:
        img = new_canvas()
        d = draw_for(img)
        f = load_font(10)
        now_t = self._mono()
        s = replace(self.stats, busy=self._busy.mean(now_t), mem_busy=self._mem.mean(now_t),
                    busy_hist=list(self._hist))
        if not s.present:
            text(d, (80, 21), "No supported GPU", f, anchor="mm")
        elif self.view == 1:
            self._render_detail(d, s, f)
        else:
            self._render_summary(d, s, f)
        return img

    def _render_summary(self, d, s: GpuStats, f) -> None:
        # row 1: load, core clock, history
        text(d, (0, 0), "GPU", f)
        text_pct(d, (24, 0), f"{_n(s.busy):>3}% {_n(s.sclk, '.0f', 'MHz')}", f)
        sparkline(d, (97, 1, 159, 9), s.busy_hist, 0, 100)
        d.line((97, 10, 159, 10), fill=1)
        # row 2: VRAM
        text(d, (0, 11), "VRAM", f)
        text(d, (30, 11), f"{fmt_gib(s.vram_used)}/{fmt_gib(s.vram_total)}G", f)
        frac = s.vram_used / s.vram_total if s.vram_used is not None and s.vram_total else 0
        hbar(d, (97, 12, 159, 20), frac)
        # row 3: temperatures (edge / junction / memory)
        text(d, (0, 22), "TMP", f)
        temps = " ".join(f"{_n(v)}°{tag}".rstrip() for v, tag in
                         ((s.temp_edge, ""), (s.temp_junction, "J"), (s.temp_mem, "M"))
                         if v is not None) or "--"
        text(d, (30, 22), temps, f)
        # row 4: power, fan
        text(d, (0, 33), "PWR", f)
        cap = f"/{s.power_cap:.0f}" if s.power_cap is not None else ""
        pw = _n(s.power, ".1f" if s.power is not None and s.power < 10 else ".0f")
        text(d, (30, 33), f"{pw}{cap}W", f)
        text(d, (97, 33), f"FAN {_n(s.fan_rpm)}", f)

    def _render_detail(self, d, s: GpuStats, f) -> None:
        text(d, (0, 0), ellipsize(d, s.name or "GPU", f, 78), f)
        text(d, (159, 0), f"PCIe {s.pcie or '--'}"[-18:], f, anchor="ra")
        text(d, (0, 11), "MCLK", f)
        text(d, (30, 11), _n(s.mclk, ".0f", "MHz"), f)
        text(d, (159, 11), f"VDD {_n(s.voltage, '.2f', 'V')}", f, anchor="ra")
        text(d, (0, 22), "GTT", f)
        text(d, (30, 22), f"{fmt_gib(s.gtt_used)}/{fmt_gib(s.gtt_total)}G", f)
        text_pct(d, (97, 22), f"FAN {_n(s.fan_pct)}%", f)
        text(d, (0, 33), "MEM BW", f)
        text_pct(d, (44, 33), f"{_n(s.mem_busy):>3}%", f)
        hbar(d, (97, 34, 159, 42), (s.mem_busy or 0) / 100)
