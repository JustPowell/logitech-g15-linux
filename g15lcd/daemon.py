"""g15lcd daemon: page manager, overlay layer, device lifecycle, CLI."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import time
from collections import deque
from datetime import datetime

from PIL import Image

from . import config as config_mod
from . import pages as pages_mod
from .config import DEFAULT_PAGES, Config, ConfigError
from .device import HEIGHT, WIDTH, ButtonEdges, DeviceGone, G15LCD, parse_buttons
from .ipc import IPCServer, make_handlers
from .pages import Page
from .render import label_image, new_canvas, notification_image

log = logging.getLogger("g15lcd")

MAX_QUEUED = 20
BACKLIGHT = "/sys/class/leds/g15::lcd_backlight/brightness"
RETRY_SECONDS = 2.0
LABEL_SECONDS = 0.8


class PageManager:
    def __init__(self, pages: list[Page]) -> None:
        if not pages:
            raise ValueError("need at least one page")
        self.pages = pages
        self.index = 0

    @property
    def current(self) -> Page:
        return self.pages[self.index]

    def next(self) -> Page:
        self.index = (self.index + 1) % len(self.pages)
        return self.current

    def prev(self) -> Page:
        self.index = (self.index - 1) % len(self.pages)
        return self.current

    def remove(self, page: Page) -> None:
        i = self.pages.index(page)
        if len(self.pages) == 1:
            raise RuntimeError("no usable pages left")
        del self.pages[i]
        if i < self.index or self.index >= len(self.pages):
            self.index = max(0, self.index - 1)

    def find(self, name: str) -> Page | None:
        return next((p for p in self.pages if p.name == name), None)

    def goto(self, name: str) -> Page:
        for i, p in enumerate(self.pages):
            if p.name == name:
                self.index = i
                return p
        raise KeyError(f"no such page {name!r}")


class Overlay:
    """Temporary images drawn centred over the page.

    ``show`` is a transient label (replaces the previous label); ``queue``
    adds notifications that are shown one after another (FIFO) and take
    priority over the label.
    """

    def __init__(self) -> None:
        self._img: Image.Image | None = None
        self._until = 0.0
        self._queue: deque[tuple[Image.Image, float]] = deque()
        self._note: Image.Image | None = None
        self._note_until = 0.0

    def show(self, img: Image.Image | str, seconds: float, now: float | None = None) -> None:
        if isinstance(img, str):
            img = label_image(img)
        self._img = img
        self._until = (time.monotonic() if now is None else now) + seconds

    def queue(self, img: Image.Image, seconds: float) -> int:
        """Queue a notification; returns how many are waiting or showing."""
        if len(self._queue) >= MAX_QUEUED:
            raise ValueError("notification queue full")
        self._queue.append((img, seconds))
        return len(self._queue) + (self._note is not None)

    def _advance(self, now: float) -> None:
        if self._note is not None and now >= self._note_until:
            self._note = None
        if self._note is None and self._queue:
            self._note, secs = self._queue.popleft()
            self._note_until = now + secs
        if self._img is not None and now >= self._until:
            self._img = None

    def active(self, now: float | None = None) -> Image.Image | None:
        self._advance(time.monotonic() if now is None else now)
        return self._note if self._note is not None else self._img

    def remaining(self, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        ov = self.active(now)
        if ov is None:
            return None
        return (self._note_until if ov is self._note else self._until) - now

    def apply(self, frame: Image.Image, now: float | None = None) -> Image.Image:
        ov = self.active(now)
        if ov is None:
            return frame
        frame = frame.copy()
        frame.paste(ov, ((WIDTH - ov.width) // 2, (HEIGHT - ov.height) // 2))
        return frame


class Backlight:
    MAX_LEVEL = 2

    def __init__(self, path: str = BACKLIGHT, on_level: int = 2) -> None:
        self.path = path
        self.on_level = on_level
        self._warned = False

    def get(self) -> int:
        with open(self.path) as f:
            return int(f.read().strip())

    def set(self, level: int | str) -> int:
        """Set 0..2 or "toggle"; returns the new level. Raises ValueError on failure."""
        if not os.access(self.path, os.W_OK):
            raise ValueError(f"backlight {self.path} not writable (install the udev rule)")
        try:
            if level == "toggle":
                level = 0 if self.get() else self.on_level
            if level not in range(self.MAX_LEVEL + 1):
                raise ValueError(f"backlight level must be 0..{self.MAX_LEVEL}")
            with open(self.path, "w") as f:
                f.write(str(level))
        except (OSError, ValueError) as e:
            raise ValueError(f"backlight: {e}") from None
        return int(level)

    def toggle(self) -> None:
        try:
            self.set("toggle")
        except ValueError as e:
            if not self._warned:
                log.warning("%s; ignoring", e)
                self._warned = True


def sd_notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError as e:
        log.warning("sd_notify failed: %s", e)


class Daemon:
    def __init__(self, page_names: list[str] | None = None, device_path: str | None = None,
                 socket_path: str | None = None, config: Config | None = None) -> None:
        self.config = config or Config()
        self.manager = PageManager(self._build_pages(page_names or self.config.pages))
        self.overlay = Overlay()
        self.backlight = Backlight(on_level=self.config.backlight_on)
        self.lcd = G15LCD(device_path)
        self.edges = ButtonEdges()
        self.ipc = IPCServer(make_handlers(self), socket_path)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._last_render = float("-inf")
        self._next_retry = 0.0
        self._overlay_was_on = False

    def _build_pages(self, names: list[str]) -> list[Page]:
        built: list[Page] = []
        for n in names:
            try:
                built.append(pages_mod.create(n, self.config.page_options.get(n)))
            except Exception as e:
                log.error("page %r skipped: %s", n, e)
        if not built:
            log.error("no usable pages configured; falling back to %s", DEFAULT_PAGES)
            built = [pages_mod.create(n) for n in DEFAULT_PAGES]
        return built

    # -- control ---------------------------------------------------------
    def wake(self) -> None:
        self._last_render = float("-inf")
        self._wake.set()

    def show_overlay(self, img: Image.Image | str, seconds: float) -> None:
        self.overlay.show(img, seconds)
        self.wake()

    def notify(self, text: str, title: str | None = None, seconds: float | None = None) -> int:
        """Queue a notification overlay; returns the number waiting or showing."""
        n = self.overlay.queue(notification_image(text, title),
                               float(seconds or self.config.notify_seconds))
        self.wake()
        return n

    def custom_page(self):
        page = self.manager.find("custom")
        if page is None:
            raise ValueError("the 'custom' page is not enabled; add \"custom\" to pages in the config")
        return page

    def set_backlight(self, level: int | str) -> int:
        return self.backlight.set(level)

    def page_info(self) -> tuple[list[str], str]:
        return [p.name for p in self.manager.pages], self.manager.current.name

    def switch(self, target: str) -> str:
        """Change page: 'next', 'prev' or a page name. Returns the new page name."""
        if target == "next":
            page = self.manager.next()
        elif target == "prev":
            page = self.manager.prev()
        else:
            page = self.manager.goto(target)
        self.show_overlay(page.name.upper(), LABEL_SECONDS)
        return page.name

    def handle_button(self, name: str) -> None:
        log.debug("button %s", name)
        if name == "MENU1":
            self.backlight.toggle()
        elif name == "MENU2":
            self.switch("prev")
        elif name == "MENU3":
            self.switch("next")
        elif self.manager.current.on_button(name):
            self.wake()

    def _on_readable(self) -> None:
        try:
            for report in self.lcd.read_reports():
                for name in self.edges.update(parse_buttons(report)):
                    self.handle_button(name)
        except DeviceGone as e:
            self._lost(e)

    # -- device lifecycle ------------------------------------------------
    def _lost(self, why: object) -> None:
        log.warning("device lost: %s", why)
        self._detach()

    def _detach(self) -> None:
        try:
            asyncio.get_running_loop().remove_reader(self.lcd.fileno())
        except (DeviceGone, ValueError):
            pass
        self.lcd.close()
        self.edges.reset()

    def _try_open(self, now: float) -> None:
        if now < self._next_retry:
            return
        self._next_retry = now + RETRY_SECONDS
        try:
            self.lcd.open()
        except DeviceGone as e:
            log.debug("device not available: %s", e)
            return
        asyncio.get_running_loop().add_reader(self.lcd.fileno(), self._on_readable)
        log.info("opened %s", self.lcd.path)
        self._last_render = float("-inf")

    def _render_due(self, now: float) -> float:
        """Render if due; return seconds until the next needed wakeup."""
        page = self.manager.current
        if self.lcd._fd is None:
            self._try_open(now)
            return max(0.05, self._next_retry - now)
        # While an overlay is up (and once more as it expires) refresh quickly.
        overlay_on = self.overlay.active() is not None
        interval = min(page.interval, 0.1) if overlay_on or self._overlay_was_on else page.interval
        self._overlay_was_on = overlay_on
        if now - self._last_render >= interval:
            self._last_render = now
            try:
                frame = self.overlay.apply(page.render(datetime.now()))
                self.lcd.write_image(frame)
            except DeviceGone as e:
                self._lost(e)
                return 0.05
            except Exception:
                log.exception("page %s render failed", page.name)
        return max(0.01, interval - (now - self._last_render))

    # -- main loop -------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        for p in list(self.manager.pages):
            try:
                await p.start()
            except Exception:
                log.exception("page %s failed to start; skipping it", p.name)
                self.manager.remove(p)
        await self.ipc.start()
        sd_notify("READY=1")
        try:
            while not self._stop.is_set():
                wait = self._render_due(time.monotonic())
                self._wake.clear()
                waiters = {asyncio.ensure_future(self._wake.wait()),
                           asyncio.ensure_future(self._stop.wait())}
                _, pending = await asyncio.wait(waiters, timeout=wait,
                                                return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        log.info("shutting down")
        sd_notify("STOPPING=1")
        await self.ipc.stop()
        for p in self.manager.pages:
            try:
                await p.stop()
            except Exception:
                log.exception("page %s stop failed", p.name)
        if self.lcd._fd is not None:
            try:
                self.lcd.write_image(new_canvas())
            except DeviceGone:
                pass
            self._detach()


def render_preview(name: str, out: str, options: dict | None = None) -> None:
    img = pages_mod.create(name, options).render(datetime.now())
    big = img.convert("L").point(lambda p: 0 if p else 255)  # ON pixels dark
    big.resize((WIDTH * 4, HEIGHT * 4), Image.NEAREST).save(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g15lcd", description="Logitech G15 LCD daemon")
    ap.add_argument("--render-page", nargs=2, metavar=("NAME", "OUT.png"),
                    help="render one page to a 4x PNG and exit (no device needed)")
    ap.add_argument("--device", metavar="PATH", help="hidraw node (default: autodetect)")
    ap.add_argument("--socket", metavar="PATH", help="IPC socket path")
    ap.add_argument("--config", metavar="PATH",
                    help="config file (default: $XDG_CONFIG_HOME/g15lcd/config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if args.render_page:
        name, out = args.render_page
        render_preview(name, out, cfg.page_options.get(name))
        return 0
    asyncio.run(Daemon(None, args.device, args.socket, cfg).run())
    return 0
