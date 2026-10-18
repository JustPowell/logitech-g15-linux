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


class OverlayDismissTests(unittest.TestCase):
    def img(self):
        from g15lcd.render import notification_image
        return notification_image("x")

    def test_upsert_coalesces_queued_and_showing(self) -> None:
        o = Overlay()
        a, b = self.img(), self.img()
        self.assertFalse(o.upsert(a, 5, ("app", "s1"), now=0))   # shows immediately
        self.assertFalse(o.upsert(a, 5, ("app", "s2"), now=0))   # queued
        self.assertTrue(o.upsert(b, 5, ("app", "s2"), now=1))    # replaces queued in place
        self.assertEqual(len(o._queue), 1)
        self.assertIs(o._queue[0].img, b)
        self.assertTrue(o.upsert(b, 5, ("app", "s1"), now=4))    # replaces showing, restarts timer
        self.assertIsNotNone(o.active(8))                        # would have expired at 5
        self.assertIs(o.active(8), b)
        self.assertFalse(o.upsert(a, 5, None, 9, now=8))         # rid 9 is new
        self.assertTrue(o.upsert(b, 5, ("other", "z"), 9, now=8))  # same rid coalesces
        self.assertEqual(len(o._queue), 2)

    def test_upsert_full(self) -> None:
        o = Overlay()
        for i in range(21):
            o.upsert(self.img(), 5, ("a", str(i)), now=0)
        with self.assertRaises(ValueError):
            o.upsert(self.img(), 5, ("a", "new"), now=0)

    def test_dismiss(self) -> None:
        o = Overlay()
        o.show("LABEL", 5, now=0)
        self.assertFalse(o.notifying(now=0))
        self.assertEqual(o.dismiss(now=0), 0)
        self.assertIsNotNone(o.active(0))                        # label untouched
        o.queue(self.img(), 5)
        o.queue(self.img(), 5)
        o.queue(self.img(), 5)
        self.assertTrue(o.notifying(now=0))
        self.assertEqual(o.dismiss(now=0), 1)
        self.assertTrue(o.notifying(now=0))                      # next one shows
        self.assertEqual(o.dismiss(all=True, now=0), 2)
        self.assertFalse(o.notifying(now=0))
        self.assertIsNotNone(o.active(0))                        # label still there


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dismiss(self, all: bool = False) -> int:
        self.calls.append(f"dismiss:{all}")
        return 2 if all else 1

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
                self.assertEqual(await ask({"cmd": "dismiss"}), {"ok": True, "dismissed": 1})
                self.assertEqual(await ask({"cmd": "dismiss", "all": True}), {"ok": True, "dismissed": 2})
                self.assertFalse((await ask({"cmd": "dismiss", "all": "yes"}))["ok"])
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
