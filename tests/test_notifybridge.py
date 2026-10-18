import logging
import os
import tempfile
import unittest

from g15lcd import config as config_mod
from g15lcd.config import ConfigError, DesktopConfig
from g15lcd.notifybridge import (DesktopNote, allowed, display_seconds, format_note,
                                 parse_notify, prepare, strip_markup)


def note(app="App", summary="Sum", body="Body", urgency=1, rid=0) -> DesktopNote:
    return DesktopNote(app, rid, summary, body, urgency)


class MarkupTests(unittest.TestCase):
    def test_strip(self) -> None:
        self.assertEqual(strip_markup('<b>Hi</b> <i>there</i> <a href="http://x?a=1&amp;b">link</a><img src="x.png"/>'),
                         "Hi there link")
        self.assertEqual(strip_markup("a<br>b<br/>c<BR />d"), "a\nb\nc\nd")
        self.assertEqual(strip_markup("5 &lt; 6 &amp;&amp; &quot;ok&quot;"), '5 < 6 && "ok"')
        self.assertEqual(strip_markup("  lots   of \t space \n\n  here "), "lots of space\nhere")

    def test_truncate(self) -> None:
        self.assertEqual(len(strip_markup("x" * 10000)), 4000)


class ParseTests(unittest.TestCase):
    def test_args(self) -> None:
        args = ("Discord", 7, "icon", "Title", "<b>Body</b>", ["default", "Open"],
                {"urgency": 2, "transient": True}, -1)
        n = parse_notify(args)
        self.assertEqual(n, DesktopNote("Discord", 7, "Title", "<b>Body</b>", 2))
        self.assertEqual(parse_notify(("a", 0, "", "s", "b", [], {}, 0)).urgency, 1)
        self.assertEqual(parse_notify(("a", 0, "", "s", "b", [], {"urgency": 9}, 0)).urgency, 2)
        self.assertIsNone(parse_notify(("a",)))


class FilterTests(unittest.TestCase):
    def test_urgency(self) -> None:
        cfg = DesktopConfig()
        self.assertFalse(allowed(note(urgency=0), cfg))
        self.assertTrue(allowed(note(urgency=1), cfg))
        self.assertTrue(allowed(note(urgency=0), DesktopConfig(min_urgency="low")))
        self.assertFalse(allowed(note(urgency=1), DesktopConfig(min_urgency="critical")))

    def test_apps(self) -> None:
        inc = DesktopConfig(include_apps=["discord"])
        self.assertTrue(allowed(note(app="Discord"), inc))
        self.assertFalse(allowed(note(app="Firefox"), inc))
        exc = DesktopConfig(exclude_apps=["FIREFOX"])
        self.assertFalse(allowed(note(app="firefox"), exc))
        self.assertTrue(allowed(note(app="Discord"), exc))

    def test_seconds(self) -> None:
        self.assertEqual(display_seconds(note(), DesktopConfig(), 5), 5)
        self.assertEqual(display_seconds(note(urgency=2), DesktopConfig(), 5), 10)
        self.assertEqual(display_seconds(note(urgency=2), DesktopConfig(seconds=200), 5), 300)
        self.assertEqual(display_seconds(note(), DesktopConfig(seconds=7), 5), 7)

    def test_prepare(self) -> None:
        cfg = DesktopConfig()
        self.assertIsNone(prepare(note(summary="", body=" <b></b> "), cfg, 5))
        self.assertIsNone(prepare(note(urgency=0), cfg, 5))
        title, text, secs, key, rid = prepare(note(app="Chat", summary="Hi", body="a<br>b", rid=3), cfg, 5)
        self.assertEqual((title, text, secs, key, rid), ("Chat", "Hi\na b", 5, ("chat", "Hi"), 3))


class FormatTests(unittest.TestCase):
    def test_layouts(self) -> None:
        n = note(app="Chat", summary="Hi", body="secret")
        self.assertEqual(format_note(n, DesktopConfig()), ("Chat", "Hi\nsecret"))
        self.assertEqual(format_note(n, DesktopConfig(hide_body=True)), ("Chat", "Hi"))
        self.assertEqual(format_note(n, DesktopConfig(show_app=False)), ("Hi", "secret"))
        self.assertEqual(format_note(note(app="", summary="Hi", body=""), DesktopConfig()), ("Hi", ""))


def write(d: str, name: str, text: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


class DesktopConfigTests(unittest.TestCase):
    def test_defaults_and_values(self) -> None:
        d = config_mod.parse({}).desktop
        self.assertEqual((d.enabled, d.include_apps, d.exclude_apps, d.min_urgency, d.hide_body, d.show_app, d.seconds),
                         (False, [], [], "normal", False, True, None))
        c = config_mod.parse({"notify": {"default_seconds": 4, "desktop": {
            "enabled": True, "include_apps": ["a"], "min_urgency": "low", "hide_body": True, "seconds": 9}}})
        self.assertTrue(c.desktop.enabled)
        self.assertEqual((c.desktop.include_apps, c.desktop.min_urgency, c.desktop.seconds), (["a"], "low", 9.0))
        self.assertEqual(c.notify_seconds, 4.0)

    def test_invalid(self) -> None:
        for bad in ({"enabled": "yes"}, {"include_apps": "x"}, {"exclude_apps": [1]},
                    {"min_urgency": "high"}, {"seconds": 0}, {"seconds": 301}, {"hide_body": 1}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                config_mod.parse({"notify": {"desktop": bad}})
        with self.assertRaises(ConfigError):
            config_mod.parse({"notify": {"desktop": 3}})

    def test_unknown_key_warns(self) -> None:
        with self.assertLogs("g15lcd.config", logging.WARNING):
            config_mod.parse({"notify": {"desktop": {"bogus": 1}}})

    def test_example_config_stays_disabled(self) -> None:
        path = os.path.join(os.path.dirname(__file__), "..", "packaging", "config.example.toml")
        self.assertFalse(config_mod.load(path).desktop.enabled)


if __name__ == "__main__":
    unittest.main()
