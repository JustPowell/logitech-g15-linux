import asyncio
import unittest
from datetime import datetime

from g15lcd.device import HEIGHT, WIDTH
from g15lcd.pages.media import MediaPage, MprisSource, Track, WatchedClock, _length_seconds, fmt_time
from g15lcd.pages.sysstats import Stats, SysStatsPage, fmt_rate

NOW = datetime(2026, 1, 1, 12, 0, 0)


class FakeSampler:
    def __init__(self):
        self.calls = 0

    def sample(self):
        self.calls += 1
        return Stats(cpu=40, cpu_hist=[10, 50, 30, 40], per_core=[10.0] * 8, ram_used=8 << 30,
                     ram_total=32 << 30, cpu_temp=55, gpu_busy=12, gpu_temp=48, rx=1.2e6, tx=3e4)


class FakeSource:
    def __init__(self, track=None):
        self.track = track
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)


class SysStatsTests(unittest.TestCase):
    def test_render_size_mode_and_content(self):
        p = SysStatsPage(FakeSampler())
        asyncio.run(self._run(p))
        img = p.render(NOW)
        self.assertEqual(img.size, (WIDTH, HEIGHT))
        self.assertEqual(img.mode, "1")
        self.assertTrue(img.getbbox())
        p.on_button("MENU5")
        self.assertEqual(p.view, 1)
        self.assertTrue(p.render(NOW).getbbox())

    async def _run(self, p):
        await p.start()
        await asyncio.sleep(0.05)
        await p.stop()
        self.assertEqual(p.stats.cpu, 40)

    def test_fmt_rate(self):
        self.assertEqual(fmt_rate(0), "0B")
        self.assertEqual(fmt_rate(34 * 1024), "34K")
        self.assertEqual(fmt_rate(1.2 * (1 << 20)), "1.2M")


class WatchedClockTests(unittest.TestCase):
    def test_accumulates_only_while_playing(self):
        w = WatchedClock()
        k = ("p", "t")
        self.assertEqual(w.update(k, "Playing", 100.0), 0.0)
        self.assertEqual(w.update(k, "Playing", 101.0), 1.0)
        self.assertEqual(w.update(k, "Paused", 102.0), 1.0)
        self.assertEqual(w.update(k, "Paused", 103.0), 1.0)
        self.assertEqual(w.update(k, "Playing", 104.0), 2.0)

    def test_resets_on_new_key(self):
        w = WatchedClock()
        w.update(("p", "a"), "Playing", 0.0)
        self.assertEqual(w.update(("p", "a"), "Playing", 3.0), 3.0)
        self.assertEqual(w.update(("p", "b"), "Playing", 4.0), 0.0)
        self.assertEqual(w.update(("q", "b"), "Playing", 5.0), 0.0)

    def test_caps_large_gap(self):
        w = WatchedClock()
        w.update(("p", "a"), "Playing", 0.0)
        self.assertEqual(w.update(("p", "a"), "Playing", 500.0), WatchedClock.MAX_DELTA)

    def test_live_track_monotonic_when_raw_position_resets(self):
        src = MprisSource()
        clock = [1000.0]
        src._mono = lambda: clock[0]
        raws = [8.4, 10.4, 12.5, 14.5, 1.5, 3.5, 5.5]
        out = []
        for raw in raws:
            t = src._make_track("org.mpris.MediaPlayer2.chromium", "chromium", "Playing",
                                "Stream", "", raw, 0.0, True)
            out.append(t.now_position(clock[0]))
            clock[0] += 2.0
        self.assertEqual(out, sorted(out))
        self.assertEqual(out[-1], 12.0)

    def test_non_live_uses_real_position(self):
        src = MprisSource()
        t = src._make_track("n", "p", "Playing", "T", "", 42.0, 100.0, False)
        self.assertEqual(t.position, 42.0)


class MediaTests(unittest.TestCase):
    def test_empty_state(self):
        p = MediaPage(FakeSource(None))
        img = p.render(NOW)
        self.assertEqual((img.size, img.mode), ((WIDTH, HEIGHT), "1"))
        self.assertTrue(img.getbbox())

    def _marquee(self, **kw):
        t = Track(player="mpv", status="Playing", title="A very long song title that cannot fit",
                  artist="Some Artist", position=83, length=225, stamp=0)
        p = MediaPage(FakeSource(t), **kw)
        clock = [100.0]
        p._mono = lambda: clock[0]
        return p, t, clock

    def test_marquee_pause_then_scroll_then_wrap(self):
        p, t, clock = self._marquee(scroll_speed=8, scroll_pause=3)
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        self.assertEqual(p.interval, 1.0)
        clock[0] += 2.5
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        self.assertAlmostEqual(p.interval, 0.5)
        clock[0] += 0.5  # pause over
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        self.assertAlmostEqual(p.interval, 0.125)
        clock[0] += 2.0
        p.render(NOW)
        self.assertEqual(p.offset, 16)
        # full period: width + GAP px at 8 px/s
        from g15lcd.render import draw_for, load_font, new_canvas, text_width
        period = text_width(draw_for(new_canvas()), t.title, load_font(12, bold=True)) + p.GAP
        clock[0] += period / 8 - 2.0 - 0.2
        p.render(NOW)
        self.assertGreater(p.offset, period - 3)
        clock[0] += 0.2
        p.render(NOW)
        self.assertEqual(p.offset, 0)  # wrapped, paused again
        clock[0] += 2.9
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        clock[0] += 0.5
        p.render(NOW)
        self.assertGreater(p.offset, 0)

    def test_marquee_frozen_when_paused_and_fresh_pause_on_resume(self):
        p, t, clock = self._marquee(scroll_speed=8, scroll_pause=1)
        p.render(NOW)
        clock[0] += 3
        p.render(NOW)
        self.assertGreater(p.offset, 0)
        t.status = "Paused"
        clock[0] += 5
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        self.assertEqual(p.interval, 1.0)
        t.status = "Playing"
        p.render(NOW)
        clock[0] += 0.9
        p.render(NOW)
        self.assertEqual(p.offset, 0)

    def test_marquee_resets_on_title_change(self):
        p, t, clock = self._marquee(scroll_speed=8, scroll_pause=1)
        p.render(NOW)
        clock[0] += 3
        p.render(NOW)
        self.assertGreater(p.offset, 0)
        t.title = "Another quite long title that will not fit either"
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        clock[0] += 0.9
        p.render(NOW)
        self.assertEqual(p.offset, 0)

    def test_marquee_option_validation(self):
        with self.assertLogs("g15lcd.pages.media", "WARNING"):
            p = MediaPage(FakeSource(None), scroll_speed=0, scroll_pause=-1)
        self.assertEqual((p.speed, p.pause), (MediaPage.DEFAULT_SPEED, MediaPage.DEFAULT_PAUSE))
        with self.assertLogs("g15lcd.pages.media", "WARNING"):
            p = MediaPage(FakeSource(None), scroll_speed="fast", scroll_pause=float("nan"))
        self.assertEqual((p.speed, p.pause), (MediaPage.DEFAULT_SPEED, MediaPage.DEFAULT_PAUSE))
        p = MediaPage(FakeSource(None), scroll_speed=12, scroll_pause=0)
        self.assertEqual((p.speed, p.pause), (12.0, 0.0))

    def test_short_title_no_scroll(self):
        t = Track(player="mpv", status="Playing", title="Hi", position=1, length=10, stamp=0)
        p = MediaPage(FakeSource(t))
        p.render(NOW)
        self.assertEqual(p.offset, 0)
        self.assertEqual(p.interval, 1.0)

    def test_interpolation(self):
        t = Track(status="Playing", position=10, length=100, stamp=5.0)
        self.assertEqual(t.now_position(8.0), 13.0)
        t.status = "Paused"
        self.assertEqual(t.now_position(8.0), 10.0)

    def test_buttons(self):
        src = FakeSource(None)
        p = MediaPage(src)
        self.assertTrue(p.on_button("MENU4"))
        self.assertTrue(p.on_button("MENU5"))
        self.assertFalse(p.on_button("MENU1"))
        self.assertEqual(src.sent, ["PlayPause", "Next"])

    def test_fmt_time(self):
        self.assertEqual(fmt_time(83), "1:23")
        self.assertEqual(fmt_time(3725), "1:02:05")

    def test_fmt_time_huge(self):
        for v in (2**63 // 10**6, 1e30, float("inf"), 100 * 3600):
            self.assertEqual(fmt_time(v), "--:--")
        self.assertEqual(fmt_time(99 * 3600 + 59), "99:00:59")

    def test_length_normalization(self):
        self.assertEqual(_length_seconds(2**63 - 1), (0.0, True))
        for raw in (0, -5, None, "x", 10**6 * 86400 * 30):
            self.assertEqual(_length_seconds(raw), (0.0, False))
        sec, live = _length_seconds(1794521000)
        self.assertAlmostEqual(sec, 1794.521)
        self.assertFalse(live)

    def test_live_render(self):
        t = Track(player="chromium", status="Playing", title="Lofi Radio 24/7 - YouTube",
                  position=5000, length=0.0, live=True, stamp=0)
        p = MediaPage(FakeSource(t))
        p._mono = lambda: 0.0
        img = p.render(NOW)
        self.assertEqual(img.size, (WIDTH, HEIGHT))
        self.assertEqual(img.mode, "1")
        self.assertTrue(img.getbbox())
        self.assertEqual(t.now_position(10.0), 5010.0)


if __name__ == "__main__":
    unittest.main()
