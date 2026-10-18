"""Now-playing page using MPRIS over the session bus (background poller thread)."""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from PIL import Image

from ..render import draw_for, ellipsize, hbar, load_font, new_canvas, text, text_width
from . import Page, register

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
COMMANDS = {"MENU4": "PlayPause", "MENU5": "Next"}


@dataclass
class Track:
    player: str = ""
    status: str = ""  # Playing / Paused / Stopped
    title: str = ""
    artist: str = ""
    position: float = 0.0  # seconds at `stamp`
    length: float = 0.0  # seconds, 0 if unknown
    stamp: float = 0.0  # time.monotonic() when position sampled
    live: bool = False  # infinite-duration stream (length sentinel)

    def now_position(self, mono: float) -> float:
        pos = self.position
        if self.status == "Playing":
            pos += max(0.0, mono - self.stamp)
        if self.length:
            pos = min(pos, self.length)
        return pos


INT64_MAX = 2**63 - 1
MAX_LENGTH_S = 7 * 86400


def _length_seconds(raw) -> tuple[float, bool]:
    """Normalize mpris:length (microseconds) -> (seconds, live). Unknown -> (0.0, False)."""
    try:
        us = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0, False
    if us <= 0:
        return 0.0, False
    if us >= INT64_MAX // 2:
        return 0.0, True
    sec = us / 1e6
    if sec > MAX_LENGTH_S:
        return 0.0, False
    return sec, False


# Hand-drawn 3x5 glyphs for the inverted "LIVE" badge (no font is legible at this size).
_LIVE_GLYPHS = (
    ("X..", "X..", "X..", "X..", "XXX"),  # L
    ("XXX", ".X.", ".X.", ".X.", "XXX"),  # I
    ("X.X", "X.X", "X.X", "X.X", ".X."),  # V
    ("XXX", "X..", "XX.", "X..", "XXX"),  # E
)


class WatchedClock:
    """Local elapsed-playing counter for live streams (their MPRIS Position resets)."""

    MAX_DELTA = 5.0

    def __init__(self) -> None:
        self._key = None
        self._last = 0.0
        self._acc = 0.0

    def update(self, key, status: str, now: float) -> float:
        if key != self._key:
            self._key, self._acc = key, 0.0
        elif status == "Playing":
            self._acc += min(max(0.0, now - self._last), self.MAX_DELTA)
        self._last = now
        return self._acc


class MprisSource:
    """Polls MPRIS in its own thread; commands are queued to that thread."""

    def __init__(self, poll: float = 1.0) -> None:
        self.poll = poll
        self.commands: queue.Queue[str] = queue.Queue()
        self.track: Track | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_player: str | None = None
        self._mono = time.monotonic  # injectable clock
        self._watched = WatchedClock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mpris", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.commands.put("")  # wake up
        if self._thread:
            self._thread.join(timeout=2)

    def send(self, cmd: str) -> None:
        self.commands.put(cmd)

    # --- thread body ---
    def _run(self) -> None:
        try:
            import dbus
            bus = dbus.SessionBus()
        except Exception as e:
            log.info("media: no session bus (%s)", e)
            return
        while not self._stop.is_set():
            try:
                self.track = self._poll(bus)
            except Exception:
                log.debug("media: poll failed", exc_info=True)
                self.track = None
            try:
                cmd = self.commands.get(timeout=self.poll)
                if cmd and self._last_player:
                    self._exec(bus, self._last_player, cmd)
                    time.sleep(0.15)
            except queue.Empty:
                pass
            except Exception:
                log.debug("media: command failed", exc_info=True)

    def _exec(self, bus, name: str, cmd: str) -> None:
        import dbus
        obj = bus.get_object(name, "/org/mpris/MediaPlayer2", introspect=False)
        getattr(dbus.Interface(obj, PLAYER_IFACE), cmd)()

    def _poll(self, bus) -> Track | None:
        import dbus
        names = [n for n in bus.list_names() if str(n).startswith(MPRIS_PREFIX)]
        best = None
        for n in names:
            try:
                obj = bus.get_object(n, "/org/mpris/MediaPlayer2", introspect=False)
                p = dbus.Interface(obj, PROPS_IFACE)
                status = str(p.Get(PLAYER_IFACE, "PlaybackStatus"))
                if best and not (status == "Playing" and best[1] != "Playing"):
                    continue
                best = (n, status, p)
            except dbus.DBusException:
                continue
        if not best:
            self._last_player = None
            return None
        n, status, p = best
        # Prefer Playing; otherwise stick with the last seen player if still present.
        if status != "Playing" and self._last_player in names and self._last_player != n:
            n = self._last_player
            obj = bus.get_object(n, "/org/mpris/MediaPlayer2", introspect=False)
            p = dbus.Interface(obj, PROPS_IFACE)
            status = str(p.Get(PLAYER_IFACE, "PlaybackStatus"))
        self._last_player = str(n)
        md = p.Get(PLAYER_IFACE, "Metadata")
        try:
            pos = int(p.Get(PLAYER_IFACE, "Position")) / 1e6
        except dbus.DBusException:
            pos = 0.0
        artists = md.get("xesam:artist", [])
        artist = ", ".join(str(x) for x in artists) if not isinstance(artists, str) else str(artists)
        ident = str(n)[len(MPRIS_PREFIX):].split(".instance")[0]
        length, live = _length_seconds(md.get("mpris:length", 0))
        title = str(md.get("xesam:title", ""))
        return self._make_track(str(n), ident, status, title, artist, pos, length, live)

    def _make_track(self, name, ident, status, title, artist, pos, length, live) -> Track:
        now = self._mono()
        if live:
            # Player Position is unreliable for live streams; count watched time locally.
            pos = self._watched.update((name, title), status, now)
        return Track(player=ident, status=status, title=title, artist=artist,
                     position=pos, length=length, live=live, stamp=now)


def fmt_time(sec: float) -> str:
    try:
        sec = int(max(0, sec))
    except (OverflowError, ValueError):
        return "--:--"
    if sec >= 100 * 3600:
        return "--:--"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@register
class MediaPage(Page):
    name = "media"
    interval = 1.0
    GAP = 24
    TEXT_W = 156
    DEFAULT_SPEED = 8.0  # px/s; slow, the STN LCD ghosts at higher speeds
    DEFAULT_PAUSE = 3.0  # s held at offset 0 before every pass

    def __init__(self, source=None, scroll_speed: float = DEFAULT_SPEED,
                 scroll_pause: float = DEFAULT_PAUSE) -> None:
        self.speed = self._valid("scroll_speed", scroll_speed, self.DEFAULT_SPEED, 1e-3)
        self.pause = self._valid("scroll_pause", scroll_pause, self.DEFAULT_PAUSE, 0.0)
        self._pass_start: float | None = None
        self.source = source  # needs .track, .send(cmd), optional start()/stop()
        self._own = source is None
        self.offset = 0
        self._title_key = ""
        self._mono = time.monotonic  # injectable clock

    @staticmethod
    def _valid(name: str, value, default: float, minimum: float) -> float:
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(value) and value >= minimum and (minimum > 0 or value >= 0)
              and value <= 1e6)
        if not ok:
            log.warning("media: invalid %s=%r, using %s", name, value, default)
            return default
        return float(value)

    def _advance(self, period: int) -> tuple[int, float]:
        """Marquee offset and next render interval, from time since this pass began."""
        now = self._mono()
        if self._pass_start is None:
            self._pass_start = now
        elapsed = now - self._pass_start
        step = max(0.05, 1.0 / self.speed)
        if elapsed < self.pause:
            return 0, min(1.0, max(0.05, self.pause - elapsed))
        dist = (elapsed - self.pause) * self.speed
        if dist >= period:  # second copy is at 0: snap back, pause again
            self._pass_start = now
            return 0, min(1.0, max(0.05, self.pause)) if self.pause else step
        return int(dist), step

    async def start(self) -> None:
        if self.source is None:
            self.source = MprisSource()
        if hasattr(self.source, "start"):
            self.source.start()

    async def stop(self) -> None:
        if self.source is not None and hasattr(self.source, "stop"):
            self.source.stop()

    def on_button(self, name: str) -> bool:
        cmd = COMMANDS.get(name)
        if not cmd or self.source is None:
            return False
        self.source.send(cmd)
        return True

    def _scrolling(self, t: Track | None, d) -> bool:
        return bool(t and t.title and text_width(d, t.title, load_font(12, bold=True)) > self.TEXT_W)

    def render(self, now: datetime) -> Image.Image:
        img = new_canvas()
        d = draw_for(img)
        t = self.source.track if self.source is not None else None
        if t is None:
            text(d, (80, 21), "No player", load_font(12), anchor="mm")
            self.interval = 1.0
            return img
        ft = load_font(12, bold=True)
        fs = load_font(10)
        if t.title != self._title_key:
            self._title_key, self.offset, self._pass_start = t.title, 0, None
        title = t.title or ("Stopped" if t.status != "Playing" else "(untitled)")
        tw = text_width(d, title, ft)
        scrolling = tw > self.TEXT_W
        self.interval = 1.0
        if scrolling:
            period = tw + self.GAP
            if t.status == "Playing":
                self.offset, self.interval = self._advance(period)
            else:  # not playing: snap to the start, fresh pause on resume
                self.offset, self._pass_start = 0, None
            text(d, (-self.offset, 0), title, ft)
            text(d, (-self.offset + period, 0), title, ft)
        else:
            self.offset, self._pass_start = 0, None
            text(d, (0, 0), title, ft)
        text(d, (0, 14), ellipsize(d, t.artist, fs, self.TEXT_W) if t.artist else "", fs)
        # glyph + times
        playing = t.status == "Playing"
        if playing:
            d.polygon([(1, 28), (1, 36), (6, 32)], fill=1)
        else:
            d.rectangle((1, 28, 2, 36), fill=1)
            d.rectangle((5, 28, 6, 36), fill=1)
        pos = t.now_position(self._mono())
        fp = load_font(9)
        pl = t.player[:12]
        pw = text_width(d, pl, fp) if pl else 0
        label = fmt_time(pos)
        if t.length:
            label = f"{label} / {fmt_time(t.length)}"
        text(d, (11, 27), label, fs)
        x = 11 + text_width(d, label, fs) + 4
        if t.live:
            bw = len(_LIVE_GLYPHS) * 4 + 3
            d.rectangle((x, 28, x + bw - 1, 36), fill=1)
            for i, g in enumerate(_LIVE_GLYPHS):
                for r, row in enumerate(g):
                    for c, ch in enumerate(row):
                        if ch == "X":
                            d.point((x + 2 + i * 4 + c, 30 + r), fill=0)
            x += bw + 3
        if pl:
            room = 159 - x
            if pw > room:
                pl = ellipsize(d, pl, fp, max(0, room))
            if pl:
                text(d, (159, 27), pl, fp, anchor="ra")
        if t.live:
            d.rectangle((0, 39, 159, 42), outline=1)
            d.line((0, 41, 159, 41), fill=1)
        else:
            hbar(d, (0, 39, 159, 42), pos / t.length if t.length else 0)
        return img
