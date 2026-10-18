"""Unix-socket JSON-lines control server (skeleton).

Each request line is a JSON object with a "cmd" key; the reply is one JSON
line, ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.
To add a command, add an entry to the handlers dict (see ``make_handlers``).
"""
from __future__ import annotations

import asyncio
import contextlib
import base64
import binascii
import io
import inspect
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger("g15lcd.ipc")

Handler = Callable[[dict[str, Any]], "dict[str, Any] | Awaitable[dict[str, Any]]"]


MAX_LINE = 8 * 1024 * 1024      # one request line
MAX_IMAGE = 5 * 1024 * 1024     # decoded image file size
MAX_PIXELS = 20_000_000
MAX_TEXT = 4000


def default_socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/g15lcd-{os.getuid()}"
    return os.path.join(base, "g15lcd.sock")


def _text_arg(req: dict[str, Any], key: str, required: bool = True) -> str | None:
    v = req.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str):
        raise ValueError(f"'{key}' (string) required")
    if len(v) > MAX_TEXT:
        raise ValueError(f"'{key}' too long (max {MAX_TEXT} chars)")
    return v


def load_image(req: dict[str, Any]) -> "Image.Image":
    """Decode the image named by req['path'] or req['data'] (base64), with limits."""
    from PIL import Image, UnidentifiedImageError
    path, data = req.get("path"), req.get("data")
    if (path is None) == (data is None):
        raise ValueError("give exactly one of 'path' or 'data'")
    if path is not None:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("'path' must be an absolute path string")
        try:
            st = os.stat(path)
        except OSError as e:
            raise ValueError(f"cannot read {path}: {e.strerror}") from None
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if st.st_size > MAX_IMAGE:
            raise ValueError(f"image too large (max {MAX_IMAGE // 1024 // 1024} MB)")
        src: Any = path
    else:
        if not isinstance(data, str):
            raise ValueError("'data' must be a base64 string")
        if len(data) > MAX_IMAGE * 4 // 3 + 16:
            raise ValueError(f"image too large (max {MAX_IMAGE // 1024 // 1024} MB)")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("'data' is not valid base64") from None
        src = io.BytesIO(raw)
    try:
        with Image.open(src) as im:
            if im.width * im.height > MAX_PIXELS:
                raise ValueError("image dimensions too large")
            im.load()
            return im.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ValueError(f"cannot decode image: {e}") from None


def make_handlers(daemon: Any) -> dict[str, Handler]:
    """Command table. ``daemon`` supplies switch/notify/custom_page/set_backlight/page_info."""
    def ping(req: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def page(req: dict[str, Any]) -> dict[str, Any]:
        name = req.get("name")
        if not isinstance(name, str):
            raise ValueError("'name' (string) required")
        return {"page": daemon.switch(name)}

    def pages(req: dict[str, Any]) -> dict[str, Any]:
        names, current = daemon.page_info()
        return {"pages": names, "current": current}

    def notify(req: dict[str, Any]) -> dict[str, Any]:
        body = _text_arg(req, "text")
        title = _text_arg(req, "title", required=False)
        seconds = req.get("seconds")
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                    or not 0 < seconds <= 300):
            raise ValueError("'seconds' must be a number in (0, 300]")
        return {"queued": daemon.notify(body, title, seconds)}

    def dismiss(req: dict[str, Any]) -> dict[str, Any]:
        every = req.get("all", False)
        if not isinstance(every, bool):
            raise ValueError("'all' must be a boolean")
        return {"dismissed": daemon.dismiss(every)}

    def text(req: dict[str, Any]) -> dict[str, Any]:
        body = _text_arg(req, "text")
        align = req.get("align", "left")
        if align not in ("left", "center"):
            raise ValueError("'align' must be 'left' or 'center'")
        custom = daemon.custom_page()
        custom.set_text(body, align)
        daemon.wake()
        if req.get("show"):
            daemon.switch("custom")
        return {}

    def image(req: dict[str, Any]) -> dict[str, Any]:
        custom = daemon.custom_page()
        custom.set_image(load_image(req))
        daemon.wake()
        if req.get("show"):
            daemon.switch("custom")
        return {}

    def backlight(req: dict[str, Any]) -> dict[str, Any]:
        level = req.get("level")
        if level != "toggle" and (isinstance(level, bool) or level not in (0, 1, 2)):
            raise ValueError("'level' must be 0, 1, 2 or \"toggle\"")
        return {"level": daemon.set_backlight(level)}

    return {"ping": ping, "page": page, "pages": pages, "notify": notify, "dismiss": dismiss, "text": text,
            "image": image, "backlight": backlight}


async def dispatch(handlers: dict[str, Handler], line: bytes) -> dict[str, Any]:
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        handler = handlers.get(str(req.get("cmd")))
        if handler is None:
            raise ValueError(f"unknown cmd {req.get('cmd')!r}")
        result = handler(req)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, **result}
    except (ValueError, KeyError) as e:  # includes JSONDecodeError
        return {"ok": False, "error": str(e)}
    except Exception as e:  # a bad handler must not kill the server
        log.exception("ipc handler failed")
        return {"ok": False, "error": f"internal error: {e}"}


class IPCServer:
    def __init__(self, handlers: dict[str, Handler], path: str | None = None) -> None:
        self.handlers = handlers
        self.path = path or default_socket_path()
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        try:
            os.unlink(self.path)  # stale socket from a previous run
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._client, path=self.path, limit=MAX_LINE)
        os.chmod(self.path, 0o600)
        log.info("ipc listening on %s", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for w in list(self._writers):  # wait_closed() blocks on open clients
                w.close()
            await self._server.wait_closed()
            self._server = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            async for line in reader:
                if not line.strip():
                    continue
                reply = await dispatch(self.handlers, line)
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except ValueError:  # request line longer than MAX_LINE
            writer.write(json.dumps({"ok": False, "error": "request too large"}).encode() + b"\n")
            with contextlib.suppress(ConnectionError):
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
