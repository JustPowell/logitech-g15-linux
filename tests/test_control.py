import asyncio
import base64
import io
import json
import logging
import os
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime

from PIL import Image

from g15lcd import config as config_mod
from g15lcd import ctl
from g15lcd import pages as pages_mod
from g15lcd.config import Config, ConfigError
from g15lcd.daemon import Backlight, Daemon, Overlay
from g15lcd.ipc import MAX_LINE, IPCServer
from g15lcd.pages import Page
from g15lcd.render import notification_image, to_lcd_image


def write(d: str, name: str, body: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(textwrap.dedent(body))
    return p


class ConfigTests(unittest.TestCase):
    def test_defaults_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = d
            try:
                self.assertEqual(config_mod.load(), Config())
            finally:
                os.environ.pop("XDG_CONFIG_HOME") if old is None else os.environ.update(XDG_CONFIG_HOME=old)

    def test_explicit_missing_is_error(self) -> None:
        with self.assertRaises(ConfigError):
            config_mod.load("/nonexistent/c.toml")

    def test_custom_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = write(d, "c.toml", """
                pages = ["clock", "custom", "clock"]
                [notify]
                default_seconds = 7
                [backlight]
                on_level = 1
                [page.clock]
                foo = 1
            """)
            c = config_mod.load(p)
        self.assertEqual(c.pages, ["clock", "custom"])
        self.assertEqual(c.notify_seconds, 7.0)
        self.assertEqual(c.backlight_on, 1)
        self.assertEqual(c.page_options, {"clock": {"foo": 1}})

    def test_unknown_keys_warn_and_bad_values_fail(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = write(d, "c.toml", 'bogus = 1\n[notify]\nnope = 2\n')
            with self.assertLogs("g15lcd.config", logging.WARNING) as cm:
                config_mod.load(p)
            self.assertEqual(len(cm.records), 2)
            with self.assertRaises(ConfigError):
                config_mod.load(write(d, "b.toml", "pages = []\n"))
            with self.assertRaises(ConfigError):
                config_mod.load(write(d, "b2.toml", "[backlight]\non_level = 9\n"))
            with self.assertRaises(ConfigError):
                config_mod.load(write(d, "b3.toml", "pages = [\n"))


class RegistryTests(unittest.TestCase):
    def test_lazy_import_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            write(d, "fakepg.py", """
                from g15lcd.pages import Page, register
                @register
                class FakePg(Page):
                    name = "fakepg"
                    def __init__(self, color="red"):
                        self.color = color
            """)
            pages_mod.__path__.append(d)
            try:
                self.assertIn("fakepg", pages_mod.available())
                p = pages_mod.create("fakepg", {"color": "blue", "junk": 1})
                self.assertEqual(p.color, "blue")
                self.assertEqual(pages_mod.create("fakepg").color, "red")
            finally:
                pages_mod.__path__.remove(d)
                pages_mod._REGISTRY.pop("fakepg", None)
                sys.modules.pop("g15lcd.pages.fakepg", None)

    def test_unknown_page(self) -> None:
        with self.assertRaisesRegex(KeyError, "unknown page 'nope'"):
            pages_mod.create("nope")
        with self.assertRaises(KeyError):
            pages_mod.create("../evil")

    def test_clock_ignores_options(self) -> None:
        pages_mod.create("clock", {"x": 1})


class FakeDev:
    pass


def make_daemon(names, d: str) -> Daemon:
    dm = Daemon(names, device_path=os.path.join(d, "nodev"), socket_path=os.path.join(d, "s.sock"),
                config=Config(notify_seconds=3))
    bl = os.path.join(d, "bl")
    with open(bl, "w") as f:
        f.write("0")
    dm.backlight = Backlight(bl, on_level=2)
    return dm


class DaemonTests(unittest.TestCase):
    def test_bad_page_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dm = make_daemon(["clock", "does_not_exist"], d)
            self.assertEqual([p.name for p in dm.manager.pages], ["clock"])

    def test_all_bad_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dm = make_daemon(["zzz"], d)
            self.assertTrue(dm.manager.pages)

    def test_start_failure_skipped(self) -> None:
        class Boom(Page):
            name = "boom"

            async def start(self) -> None:
                raise RuntimeError("no")

        async def go() -> list[str]:
            with tempfile.TemporaryDirectory() as d:
                dm = make_daemon(["clock"], d)
                dm.manager.pages.insert(0, Boom())
                dm._stop.set()
                await dm.run()
                return [p.name for p in dm.manager.pages]
        self.assertEqual(asyncio.run(go()), ["clock"])


class QueueTests(unittest.TestCase):
    def test_fifo(self) -> None:
        o = Overlay()
        a, b = notification_image("a"), notification_image("b")
        o.queue(a, 2)
        o.queue(b, 2)
        self.assertIs(o.active(0), a)
        self.assertIs(o.active(1.9), a)
        self.assertIs(o.active(2.0), b)
        self.assertIsNone(o.active(4.1))

    def test_label_does_not_drop_notification(self) -> None:
        o = Overlay()
        a = notification_image("a")
        o.queue(a, 5)
        o.show("NEXT", 0.8, now=0)
        self.assertIs(o.active(0.1), a)
        self.assertIsNone(o.active(6))

    def test_full(self) -> None:
        o = Overlay()
        with self.assertRaises(ValueError):
            for _ in range(100):
                o.queue(notification_image("x"), 1)


class ImageTests(unittest.TestCase):
    def test_convert(self) -> None:
        src = Image.new("RGB", (400, 100), "white")
        src.paste((0, 0, 0), (0, 0, 200, 100))
        out = to_lcd_image(src)
        self.assertEqual((out.size, out.mode), ((160, 43), "1"))
        self.assertEqual(out.getpixel((10, 21)), 255)   # dark source -> ON
        self.assertEqual(out.getpixel((150, 21)), 0)    # white -> off
        self.assertEqual(out.getpixel((10, 0)), 0)      # letterbox bar -> off

    def test_transparent_is_off(self) -> None:
        out = to_lcd_image(Image.new("RGBA", (50, 50), (0, 0, 0, 0)))
        self.assertIsNone(out.getbbox())


def png_b64(size=(32, 32), color="black") -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class IPCCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dm = make_daemon(["clock", "custom"], self.tmp.name)
        await self.dm.ipc.start()
        self.addAsyncCleanup(self.dm.ipc.stop)
        self.addCleanup(lambda: self.w.close())
        self.r, self.w = await asyncio.open_unix_connection(self.dm.ipc.path, limit=MAX_LINE)

    async def ask(self, obj) -> dict:
        self.w.write((obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n")
        await self.w.drain()
        return json.loads(await self.r.readline())

    async def test_notify(self) -> None:
        r = await self.ask({"cmd": "notify", "text": "hi", "title": "T"})
        self.assertEqual(r, {"ok": True, "queued": 1})
        self.assertEqual((await self.ask({"cmd": "notify", "text": "again", "seconds": 1}))["queued"], 2)
        self.assertIsNotNone(self.dm.overlay.active())
        for bad in ({"cmd": "notify"}, {"cmd": "notify", "text": "x", "seconds": -1},
                    {"cmd": "notify", "text": "x", "seconds": True}, {"cmd": "notify", "text": 5}):
            self.assertFalse((await self.ask(bad))["ok"])

    async def test_text_and_show(self) -> None:
        self.assertTrue((await self.ask({"cmd": "text", "text": "a\nb", "align": "center", "show": True}))["ok"])
        self.assertEqual(self.dm.manager.current.name, "custom")
        self.assertTrue(self.dm.manager.current.render(datetime.now()).getbbox())
        self.assertFalse((await self.ask({"cmd": "text", "text": "x", "align": "right"}))["ok"])

    async def test_image(self) -> None:
        r = await self.ask({"cmd": "image", "data": png_b64()})
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.dm.manager.find("custom").render(datetime.now()).getbbox())
        path = os.path.join(self.tmp.name, "i.png")
        Image.new("RGB", (10, 10)).save(path)
        self.assertTrue((await self.ask({"cmd": "image", "path": path}))["ok"])

    async def test_image_validation(self) -> None:
        big = os.path.join(self.tmp.name, "big.png")
        with open(big, "wb") as f:
            f.write(b"\0" * (5 * 1024 * 1024 + 1))
        for bad in ({"cmd": "image"}, {"cmd": "image", "path": "rel.png"},
                    {"cmd": "image", "path": self.tmp.name},          # directory
                    {"cmd": "image", "path": big},
                    {"cmd": "image", "path": "/nonexistent.png"},
                    {"cmd": "image", "data": "!!!"},
                    {"cmd": "image", "data": base64.b64encode(b"notpng").decode()},
                    {"cmd": "image", "path": "/a", "data": "b"}):
            r = await self.ask(bad)
            self.assertFalse(r["ok"], bad)

    async def test_custom_missing(self) -> None:
        dm = make_daemon(["clock"], self.tmp.name)
        with self.assertRaisesRegex(ValueError, "custom"):
            dm.custom_page()
        from g15lcd.ipc import dispatch, make_handlers
        r = await dispatch(make_handlers(dm), b'{"cmd":"text","text":"x"}')
        self.assertFalse(r["ok"])
        self.assertIn("custom", r["error"])

    async def test_backlight(self) -> None:
        self.assertEqual(await self.ask({"cmd": "backlight", "level": 1}), {"ok": True, "level": 1})
        self.assertEqual((await self.ask({"cmd": "backlight", "level": "toggle"}))["level"], 0)
        self.assertEqual((await self.ask({"cmd": "backlight", "level": "toggle"}))["level"], 2)
        self.assertFalse((await self.ask({"cmd": "backlight", "level": 3}))["ok"])
        self.assertFalse((await self.ask({"cmd": "backlight", "level": True}))["ok"])
        self.dm.backlight.path = "/nonexistent/bl"
        self.assertFalse((await self.ask({"cmd": "backlight", "level": 1}))["ok"])

    async def test_pages(self) -> None:
        self.assertEqual(await self.ask({"cmd": "pages"}),
                         {"ok": True, "pages": ["clock", "custom"], "current": "clock"})
        await self.ask({"cmd": "page", "name": "next"})
        self.assertEqual((await self.ask({"cmd": "pages"}))["current"], "custom")

    async def test_malformed_and_oversize(self) -> None:
        self.assertFalse((await self.ask(b"{bad"))["ok"])
        self.assertFalse((await self.ask(b"[1]"))["ok"])
        self.assertFalse((await self.ask(b"\xff\xfe"))["ok"])
        self.w.write(b"x" * (MAX_LINE + 10) + b"\n")
        await self.w.drain()
        r = json.loads(await self.r.readline())
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])
        self.w.close()
        # server still alive for new clients
        r2, w2 = await asyncio.open_unix_connection(self.dm.ipc.path)
        w2.write(b'{"cmd":"ping"}\n')
        await w2.drain()
        self.assertTrue(json.loads(await r2.readline())["ok"])
        w2.close()


class CtlTests(unittest.TestCase):
    def req(self, *argv: str, stdin: str = "") -> dict:
        return ctl.build_request(ctl.build_parser().parse_args(argv), io.StringIO(stdin))

    def test_mapping(self) -> None:
        self.assertEqual(self.req("notify", "hi", "-t", "T", "-s", "2.5"),
                         {"cmd": "notify", "text": "hi", "title": "T", "seconds": 2.5})
        self.assertEqual(self.req("notify", "hi"), {"cmd": "notify", "text": "hi"})
        self.assertEqual(self.req("text", "a\\nb", "--show"), {"cmd": "text", "text": "a\nb", "show": True})
        self.assertEqual(self.req("text", "-", stdin="x\ny\n"), {"cmd": "text", "text": "x\ny"})
        self.assertEqual(self.req("text", "q", "--align", "center")["align"], "center")
        self.assertEqual(self.req("image", "foo.png")["path"], os.path.abspath("foo.png"))
        self.assertEqual(self.req("page", "next"), {"cmd": "page", "name": "next"})
        self.assertEqual(self.req("pages"), {"cmd": "pages"})
        self.assertEqual(self.req("backlight", "2"), {"cmd": "backlight", "level": 2})
        self.assertEqual(self.req("backlight", "toggle"), {"cmd": "backlight", "level": "toggle"})
        self.assertEqual(self.req("ping"), {"cmd": "ping"})

    def test_no_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            old, sys.stderr = sys.stderr, err
            try:
                rc = ctl.main(["--socket", os.path.join(d, "none.sock"), "ping"])
            finally:
                sys.stderr = old
        self.assertEqual(rc, 1)
        self.assertIn("is g15lcd running?", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class DefaultPagesTest(unittest.TestCase):
    def test_defaults(self) -> None:
        from g15lcd.config import DEFAULT_PAGES, Config
        self.assertEqual(DEFAULT_PAGES, ["clock", "sysstats", "gpu", "media", "custom"])
        self.assertEqual(Config().pages, DEFAULT_PAGES)

    def test_example_config_matches(self) -> None:
        import os
        from g15lcd.config import DEFAULT_PAGES, load
        path = os.path.join(os.path.dirname(__file__), "..", "packaging", "config.example.toml")
        self.assertEqual(load(path).pages, DEFAULT_PAGES)
