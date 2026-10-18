import unittest

from PIL import Image

from g15lcd.device import pack, parse_buttons


def blank() -> Image.Image:
    return Image.new("1", (160, 43), 0)


class PackTests(unittest.TestCase):
    def test_header(self) -> None:
        buf = pack(blank())
        self.assertEqual(len(buf), 992)
        self.assertEqual(buf[0], 3)

    def test_all_off(self) -> None:
        self.assertEqual(set(pack(blank())[1:]), {0})

    def test_pixels(self) -> None:
        cases = [((0, 0), 32, 0), ((159, 42), 32 + 5 * 160 + 159, 2), ((7, 8), 32 + 160 + 7, 0)]
        for (x, y), idx, bit in cases:
            img = blank()
            img.putpixel((x, y), 1)
            buf = pack(img)
            self.assertEqual(buf[idx], 1 << bit, (x, y))
            self.assertEqual(sum(1 for b in buf[1:] if b), 1)

    def test_wrong_size(self) -> None:
        with self.assertRaises(ValueError):
            pack(Image.new("1", (160, 44)))

    def test_other_mode(self) -> None:
        buf = pack(Image.new("L", (160, 43), 255))
        self.assertEqual(buf[32], 0xFF)


class ButtonTests(unittest.TestCase):
    def rep(self, **kw: int) -> bytes:
        r = bytearray(9)
        r[0] = 2
        for k, v in kw.items():
            r[int(k[1:])] = v
        return bytes(r)

    def test_none(self) -> None:
        self.assertEqual(parse_buttons(self.rep()), frozenset())

    def test_menu1(self) -> None:
        self.assertEqual(parse_buttons(self.rep(i8=0x80)), {"MENU1"})

    def test_menu2_to_5(self) -> None:
        for i in range(4):
            self.assertEqual(parse_buttons(self.rep(**{f"i{2 + i}": 0x80})), {f"MENU{i + 2}"})

    def test_multi(self) -> None:
        self.assertEqual(parse_buttons(self.rep(i2=0x80, i5=0x80)), {"MENU2", "MENU5"})

    def test_invalid(self) -> None:
        self.assertEqual(parse_buttons(b"\x02\x00"), frozenset())
        self.assertEqual(parse_buttons(bytes([1]) + bytes([0xFF] * 8)), frozenset())


if __name__ == "__main__":
    unittest.main()
