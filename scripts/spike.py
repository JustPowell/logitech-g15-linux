#!/usr/bin/env python3
"""Hardware spike: pattern | buttons | clear."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image, ImageDraw

from g15lcd import G15LCD, find_hidraw, pack, parse_buttons


def pattern() -> None:
    img = Image.new("1", (160, 43), 0)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 159, 42), outline=1)
    d.line((0, 0, 42, 42), fill=1)
    d.text((10, 4), "G15 OK", fill=1)
    d.rectangle((150, 3, 156, 9), fill=1)  # top-right orientation marker
    path = find_hidraw()
    with G15LCD(path) as lcd:
        data = pack(img)
        lcd.write_raw(data)
    print(f"wrote {len(data)} bytes to {path}")


def clear() -> None:
    with G15LCD() as lcd:
        lcd.write_image(Image.new("1", (160, 43), 0))
    print("cleared")


def buttons() -> None:
    path = find_hidraw()
    if path is None:
        sys.exit("G15 hidraw not found")
    fd = os.open(path, os.O_RDONLY)
    print(f"reading {path}; Ctrl-C to stop")
    try:
        while True:
            r = os.read(fd, 64)
            print(r.hex(" "), sorted(parse_buttons(r)), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)


def main() -> None:
    cmds = {"pattern": pattern, "buttons": buttons, "clear": clear}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit("usage: spike.py pattern|buttons|clear")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
