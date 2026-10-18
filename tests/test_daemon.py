import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime

from g15lcd import pages as pages_mod
from g15lcd.daemon import Overlay, PageManager
from g15lcd.device import ButtonEdges, parse_buttons
from g15lcd.ipc import IPCServer, make_handlers
from g15lcd.pages import Page


class ClockTests(unittest.TestCase):
    def test_render(self) -> None:
        img = pages_mod.create("clock").render(datetime(2026, 10, 18, 9, 5, 30))
        self.assertEqual(img.size, (160, 43))
        self.assertEqual(img.mode, "1")
        self.assertTrue(img.getbbox())


class ManagerTests(unittest.TestCase):
    def test_wraps(self) -> None:
        a, b, c = Page(), Page(), Page()
        for p, n in ((a, "a"), (b, "b"), (c, "c")):
            p.name = n
        m = PageManager([a, b, c])
        self.assertIs(m.prev(), c)
        self.assertIs(m.next(), a)
        self.assertIs(m.next(), b)
        self.assertIs(m.goto("c"), c)
        with self.assertRaises(KeyError):
            m.goto("zzz")


def report(menu: int | None = None) -> bytes:
    r = bytearray(9)
    r[0] = 2
    if menu == 1:
        r[8] = 0x80
    elif menu:
        r[2 + menu - 2] = 0x80
    return bytes(r)


class EdgeTests(unittest.TestCase):
    def test_edges(self) -> None:
        e = ButtonEdges()
        seq = [report(), report(3), report(3), report(3), report(), report(3)]
        fired = [e.update(parse_buttons(r)) for r in seq]
        self.assertEqual(fired, [[], ["MENU3"], [], [], [], ["MENU3"]])


class OverlayTests(unittest.TestCase):
    def test_expiry(self) -> None:
        o = Overlay()
        self.assertIsNone(o.active(0))
        o.show("HI", 0.8, now=10.0)
        self.assertIsNotNone(o.active(10.5))
        self.assertIsNone(o.active(10.8))
        base = pages_mod.create("clock").render(datetime.now())
        o.show("HI", 1, now=0)
        self.assertNotEqual(list(o.apply(base, now=0.5).tobytes()), list(base.tobytes()))
        self.assertEqual(o.apply(base, now=2).tobytes(), base.tobytes())


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def switch(self, target: str) -> str:
        if target == "bad":
            raise KeyError("no such page 'bad'")
        self.calls.append(target)
        return target


class IPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.sock")
            fake = FakeDaemon()
            srv = IPCServer(make_handlers(fake), path)
            await srv.start()
            try:
                r, w = await asyncio.open_unix_connection(path)

                async def ask(obj: object) -> dict:
                    w.write(json.dumps(obj).encode() + b"\n")
                    await w.drain()
                    return json.loads(await r.readline())

                self.assertEqual(await ask({"cmd": "ping"}), {"ok": True, "pong": True})
                self.assertEqual(await ask({"cmd": "page", "name": "next"}), {"ok": True, "page": "next"})
                self.assertEqual(fake.calls, ["next"])
                self.assertFalse((await ask({"cmd": "page", "name": "bad"}))["ok"])
                self.assertFalse((await ask({"cmd": "nope"}))["ok"])
                w.write(b"not json\n")
                await w.drain()
                self.assertFalse(json.loads(await r.readline())["ok"])
                w.close()
            finally:
                await srv.stop()
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
