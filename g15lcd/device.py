"""Logitech G15 (gen 1) 160x43 monochrome LCD access via hidraw."""
from __future__ import annotations

import errno
import glob
import os
from types import TracebackType

from PIL import Image

WIDTH, HEIGHT = 160, 43
REPORT_LEN = 992
HEADER_LEN = 32
HID_ID = "HID_ID=0003:0000046D:0000C222"


class DeviceGone(Exception):
    """The keyboard was unplugged or the hidraw node went away."""


def find_hidraw() -> str | None:
    """Return /dev/hidrawN for the G15 LCD interface (046d:c222), or None."""
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        if HID_ID in (ln.upper() for ln in lines):
            return "/dev/" + uevent.split("/")[4]
    return None


def pack(image: Image.Image) -> bytes:
    """Pack a 160x43 PIL image into the 992-byte LCD output report.

    Convention: after conversion to mode '1', a pixel value of 1 (white,
    e.g. ``draw.point(xy, fill=1)``) means LCD pixel ON (dark). Value 0 is off.
    Layout: buf[0]=0x03, buf[1..31]=0, then 6 bands of 160 bytes; byte
    32 + (y // 8) * 160 + x, bit (y % 8), LSB = top row.
    """
    if image.size != (WIDTH, HEIGHT):
        raise ValueError(f"image must be {WIDTH}x{HEIGHT}, got {image.size[0]}x{image.size[1]}")
    img = image.convert("1")
    px = img.load()
    buf = bytearray(REPORT_LEN)
    buf[0] = 0x03
    for y in range(HEIGHT):
        bit = 1 << (y % 8)
        base = HEADER_LEN + (y // 8) * WIDTH
        for x in range(WIDTH):
            if px[x, y]:
                buf[base + x] |= bit
    return bytes(buf)


def parse_buttons(report: bytes) -> frozenset[str]:
    """Return pressed LCD menu buttons (MENU1..MENU5) from an input report 0x02."""
    if len(report) < 9 or report[0] != 0x02:
        return frozenset()
    pressed = set()
    if report[8] & 0x80:
        pressed.add("MENU1")
    for i in range(4):
        if report[2 + i] & 0x80:
            pressed.add(f"MENU{i + 2}")
    return frozenset(pressed)


class ButtonEdges:
    """Turn level-style button states into press events (rising edges)."""

    def __init__(self) -> None:
        self._down: frozenset[str] = frozenset()

    def update(self, pressed: frozenset[str]) -> list[str]:
        new = sorted(pressed - self._down)
        self._down = pressed
        return new

    def reset(self) -> None:
        self._down = frozenset()


class G15LCD:
    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._fd: int | None = None
        self._last: bytes | None = None
        self.disconnected = False

    def open(self) -> None:
        path = self.path or find_hidraw()
        if path is None:
            raise DeviceGone("G15 hidraw device not found")
        try:
            # One non-blocking fd: frames go out via write(), button input
            # reports come back via read() (see read_reports / add_reader).
            self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EIO, errno.ENOENT):
                raise DeviceGone(str(e)) from e
            raise
        self.path = path
        self.disconnected = False
        self._last = None

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def fileno(self) -> int:
        """fd for ``loop.add_reader``; raises DeviceGone if not open."""
        if self._fd is None:
            raise DeviceGone("device not open")
        return self._fd

    def read_reports(self) -> list[bytes]:
        """Drain all pending input reports without blocking."""
        fd = self.fileno()
        reports: list[bytes] = []
        while True:
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                return reports
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO):
                    self.disconnected = True
                    raise DeviceGone(str(e)) from e
                raise
            if not data:
                return reports
            reports.append(data)

    def write_image(self, img: Image.Image) -> None:
        self.write_raw(pack(img))

    def write_raw(self, data: bytes) -> None:
        if self._fd is None:
            raise DeviceGone("device not open")
        if data == self._last:
            return
        try:
            os.write(self._fd, data)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EIO):
                self.disconnected = True
                raise DeviceGone(str(e)) from e
            raise
        self._last = data

    def __enter__(self) -> "G15LCD":
        self.open()
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()
