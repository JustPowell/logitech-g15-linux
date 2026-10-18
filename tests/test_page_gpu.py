import asyncio
import os
import tempfile
import unittest
from datetime import datetime

from g15lcd.device import HEIGHT, WIDTH
from g15lcd.pages.gpu import GpuPage, GpuStats, Sampler, find_card, shorten_name

NOW = datetime(2026, 1, 1, 12, 0, 0)


def put(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def make_dgpu(root, card="card1"):
    d = f"{card}/device"
    for f, v in {
        "gpu_busy_percent": "42\n", "mem_busy_percent": "17\n",
        "mem_info_vram_used": str(8 << 30), "mem_info_vram_total": str(16 << 30),
        "mem_info_gtt_used": str(1 << 30), "mem_info_gtt_total": str(15 << 30),
        "pp_dpm_sclk": "0: 500Mhz\n1: 2974Mhz *\n2: 2400Mhz\n",
        "pp_dpm_mclk": "0: 96Mhz\n1: 1258Mhz *\n",
        "pp_dpm_pcie": "0: 2.5GT/s, x16 250Mhz\n1: 16.0GT/s, x16 616Mhz *\n",
        "product_name": "Radeon RX 9070 XT 16GB\n",
        "hwmon/hwmon3/name": "amdgpu\n",
        "hwmon/hwmon3/temp1_input": "49000\n", "hwmon/hwmon3/temp1_label": "edge\n",
        "hwmon/hwmon3/temp2_input": "53000\n", "hwmon/hwmon3/temp2_label": "junction\n",
        "hwmon/hwmon3/temp3_input": "74000\n", "hwmon/hwmon3/temp3_label": "mem\n",
        "hwmon/hwmon3/power1_average": "50000000\n", "hwmon/hwmon3/power1_cap": "304000000\n",
        "hwmon/hwmon3/fan1_input": "1501\n", "hwmon/hwmon3/fan1_max": "3000\n",
        "hwmon/hwmon3/pwm1": "128\n", "hwmon/hwmon3/in0_input": "950\n",
        "hwmon/hwmon3/freq1_input": "71000000\n", "hwmon/hwmon3/freq2_input": "1258000000\n",
    }.items():
        put(root, f"{d}/{f}", v)


def make_igpu(root, card="card0"):
    d = f"{card}/device"
    put(root, f"{d}/gpu_busy_percent", "3\n")
    put(root, f"{d}/mem_busy_percent", "")          # empty
    put(root, f"{d}/mem_info_vram_used", "16789504\n")
    put(root, f"{d}/mem_info_vram_total", str(512 << 20))
    put(root, f"{d}/pp_dpm_pcie", "")
    put(root, f"{d}/hwmon/hwmon1/name", "amdgpu\n")
    put(root, f"{d}/hwmon/hwmon1/temp1_input", "45000\n")
    put(root, f"{d}/hwmon/hwmon1/power1_input", "20000000\n")
    put(root, f"{d}/hwmon/hwmon1/fan1_input", "\n")


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


class SamplerTests(TreeCase):
    def test_full_dgpu(self):
        make_dgpu(self.root)
        sm = Sampler(root=self.root)
        sm.resolve_name()
        s = sm.sample()
        self.assertTrue(s.present)
        self.assertEqual(s.name, "RX 9070 XT")
        self.assertEqual((s.busy, s.mem_busy), (42, 17))
        self.assertEqual((s.vram_used, s.vram_total), (8 << 30, 16 << 30))
        self.assertEqual((s.temp_edge, s.temp_junction, s.temp_mem), (49, 53, 74))
        self.assertEqual((s.power, s.power_cap, s.fan_rpm), (50, 304, 1501))
        self.assertAlmostEqual(s.fan_pct, 128 * 100 / 255)
        self.assertAlmostEqual(s.voltage, 0.95)
        self.assertEqual((s.sclk, s.mclk), (71, 1258))
        self.assertEqual(s.pcie, "16GT/s x16")
        self.assertEqual(s.busy_hist, [42])

    def test_sparse_igpu(self):
        make_igpu(self.root)
        sm = Sampler(root=self.root)
        s = sm.sample()
        self.assertEqual(s.name, "GPU 0")
        self.assertEqual((s.busy, s.mem_busy), (3, None))
        self.assertEqual(s.temp_edge, 45)
        self.assertIsNone(s.temp_junction)
        self.assertEqual(s.power, 20)
        self.assertIsNone(s.power_cap)
        self.assertIsNone(s.fan_rpm)
        self.assertIsNone(s.fan_pct)
        self.assertIsNone(s.pcie)
        self.assertIsNone(s.sclk)

    def test_dpm_fallback_for_clocks(self):
        make_igpu(self.root)
        put(self.root, "card0/device/pp_dpm_sclk", "0: 600Mhz *\n1: 2200Mhz\n")
        self.assertEqual(Sampler(root=self.root).sample().sclk, 600)

    def test_garbage_values_ignored(self):
        make_dgpu(self.root)
        put(self.root, "card1/device/gpu_busy_percent", "N/A\n")
        s = Sampler(root=self.root).sample()
        self.assertIsNone(s.busy)
        self.assertEqual(s.busy_hist, [])

    def test_history_is_bounded(self):
        make_dgpu(self.root)
        sm = Sampler(root=self.root)
        for _ in range(200):
            s = sm.sample()
        self.assertEqual(len(s.busy_hist), 64)

    def test_name_shortening(self):
        self.assertEqual(shorten_name("Radeon RX 9070 XT 16GB"), "RX 9070 XT")
        self.assertEqual(shorten_name("Granite Ridge [Radeon 610M]"), "610M")


class SelectionTests(TreeCase):
    def test_auto_picks_largest_vram(self):
        make_igpu(self.root)
        make_dgpu(self.root)
        self.assertEqual(find_card(root=self.root), f"{self.root}/card1/device")

    def test_explicit_card_name_and_index(self):
        make_igpu(self.root)
        make_dgpu(self.root)
        self.assertEqual(find_card("card0", self.root), f"{self.root}/card0/device")
        self.assertEqual(find_card(0, self.root), f"{self.root}/card0/device")
        self.assertEqual(find_card("0", self.root), f"{self.root}/card0/device")

    def test_missing_or_non_amdgpu(self):
        put(self.root, "card0/device/vendor", "0x10de\n")  # e.g. nvidia: no amdgpu files
        self.assertIsNone(find_card(root=self.root))
        self.assertIsNone(find_card("card5", self.root))
        self.assertFalse(Sampler(root=self.root).sample().present)


class FakeSampler:
    def sample(self):
        return GpuStats(name="RX 9070 XT", busy=50, busy_hist=[10, 50, 90], vram_used=8 << 30,
                        vram_total=16 << 30, temp_edge=49, power=50, power_cap=304, fan_rpm=1500)


class PageTests(unittest.TestCase):
    async def _run(self, p):
        await p.start()
        await asyncio.sleep(0.05)
        await p.stop()

    def test_render_both_views(self):
        p = GpuPage(sampler=FakeSampler())
        asyncio.run(self._run(p))
        views = []
        for _ in range(2):
            img = p.render(NOW)
            self.assertEqual((img.size, img.mode), ((WIDTH, HEIGHT), "1"))
            self.assertTrue(img.getbbox())
            views.append(img.tobytes())
            self.assertTrue(p.on_button("MENU5"))
        self.assertEqual(p.view, 0)
        self.assertNotEqual(views[0], views[1])
        self.assertTrue(p.on_button("MENU4"))
        self.assertEqual(p.view, 1)
        self.assertFalse(p.on_button("MENU2"))

    def test_render_with_empty_stats_does_not_crash(self):
        p = GpuPage(sampler=FakeSampler())
        for v in (0, 1):
            p.view = v
            self.assertTrue(p.render(NOW).getbbox())

    def test_no_gpu_state(self):
        p = GpuPage(sampler=FakeSampler())
        p.stats = GpuStats(present=False)
        self.assertTrue(p.render(NOW).getbbox())

    def test_real_sampler_with_tree(self):
        with tempfile.TemporaryDirectory() as root:
            make_igpu(root)
            sm = Sampler(root=root)
            p = GpuPage(sampler=sm)
            asyncio.run(self._run(p))
            self.assertEqual(p.stats.busy, 3)
            for v in (0, 1):
                p.view = v
                self.assertTrue(p.render(NOW).getbbox())


if __name__ == "__main__":
    unittest.main()
