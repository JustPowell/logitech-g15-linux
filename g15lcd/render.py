"""Drawing helpers for the 160x43 1-bit canvas (fill=1 means pixel ON/dark)."""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .device import HEIGHT, WIDTH

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

_FONT_PATHS = {
    False: [
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
    ],
    True: [
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/noto/NotoSansMono-Bold.ttf",
    ],
}


@lru_cache(maxsize=32)
def load_font(size: int, bold: bool = False, liberation: bool = False) -> Font:
    """Mono font. ``liberation`` prefers Liberation Mono, whose small sizes stay
    crisp in 1-bit (DejaVu bold smudges N/E/T and squashes '%' at 10px)."""
    paths = _FONT_PATHS[bold]
    if liberation:
        paths = [x for x in paths if "Liberation" in x] + [x for x in paths if "Liberation" not in x]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def new_canvas() -> Image.Image:
    return Image.new("1", (WIDTH, HEIGHT), 0)


def draw_for(img: Image.Image) -> ImageDraw.ImageDraw:
    """ImageDraw with antialiasing off so text stays crisp on 1-bit output."""
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    return d


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, font: Font,
         fill: int = 1, anchor: str = "la") -> None:
    draw.text(xy, s, font=font, fill=fill, anchor=anchor)


def text_width(draw: ImageDraw.ImageDraw, s: str, font: Font) -> int:
    return int(draw.textlength(s, font=font))


def ellipsize(draw: ImageDraw.ImageDraw, s: str, font: Font, width: int) -> str:
    """Shorten ``s`` with an ellipsis so it renders within ``width`` pixels."""
    if text_width(draw, s, font) <= width:
        return s
    while s and text_width(draw, s + "…", font) > width:
        s = s[:-1]
    return s + "…" if s else ""


text_fit = ellipsize


def hbar(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fraction: float) -> None:
    """Outlined progress bar filled left to right; box is inclusive (x0,y0,x1,y1)."""
    x0, y0, x1, y1 = box
    draw.rectangle(box, outline=1, fill=0)
    f = min(1.0, max(0.0, fraction))
    inner = x1 - x0 - 1
    n = round(inner * f)
    if n > 0 and y1 - y0 > 1:
        draw.rectangle((x0 + 1, y0 + 1, x0 + n, y1 - 1), fill=1)


def sparkline(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
              values: Sequence[float], lo: float | None = None, hi: float | None = None) -> None:
    """Line graph of the most recent values that fit the box width."""
    x0, y0, x1, y1 = box
    vals = list(values)[-(x1 - x0 + 1):]
    if len(vals) < 2:
        return
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    span = (hi - lo) or 1.0
    h = y1 - y0
    pts = [(x0 + i, y1 - round((min(max(v, lo), hi) - lo) / span * h)) for i, v in enumerate(vals)]
    draw.line(pts, fill=1)


def invert_region(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Invert pixels in box (x0,y0,x1,y1 inclusive), in place."""
    x0, y0, x1, y1 = box
    region = img.crop((x0, y0, x1 + 1, y1 + 1)).point(lambda p: 0 if p else 255).convert("1")
    img.paste(region, (x0, y0))


def label_image(s: str, font: Font | None = None) -> Image.Image:
    """Small inverted label (light text on a dark box, 1px clear margin)."""
    font = font or load_font(11, bold=True)
    probe = draw_for(new_canvas())
    w = min(WIDTH, text_width(probe, s, font) + 10)
    h = 17
    img = Image.new("1", (w, h), 0)
    d = draw_for(img)
    d.rectangle((1, 1, w - 2, h - 2), fill=1)
    text(d, (w / 2, h / 2), s, font, fill=0, anchor="mm")
    return img


# -- text layout ---------------------------------------------------------------
def _clean(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(c for c in s if c == "\n" or c.isprintable())


def wrap_text(draw: ImageDraw.ImageDraw, s: str, font: Font, width: int) -> list[str]:
    """Word-wrap to ``width`` px, honouring newlines; over-long words break mid-word."""
    lines: list[str] = []
    for para in _clean(s).split("\n"):
        cur = ""
        for word in para.split():
            cand = f"{cur} {word}" if cur else word
            if text_width(draw, cand, font) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while text_width(draw, word, font) > width and len(word) > 1:
                n = len(word) - 1
                while n > 1 and text_width(draw, word[:n], font) > width:
                    n -= 1
                lines.append(word[:n])
                word = word[n:]
            cur = word
        lines.append(cur)
    return lines


def line_height(font: Font) -> int:
    asc, desc = font.getmetrics() if hasattr(font, "getmetrics") else (10, 2)
    return asc + desc


def fit_text(s: str, width: int, height: int, sizes: Sequence[int],
             max_lines: int | None = None, bold: bool = False) -> tuple[Font, list[str], int]:
    """Pick the largest font in ``sizes`` (descending) whose wrapped text fits.

    Returns (font, lines, line_height). At the smallest size, overflow is cut
    and the last visible line ends with an ellipsis.
    """
    draw = draw_for(new_canvas())
    font, lines, lh, cap = None, [], 1, 1
    for size in sizes:
        font = load_font(size, bold)
        lh = line_height(font)
        cap = max(1, height // lh)
        if max_lines:
            cap = min(cap, max_lines)
        lines = wrap_text(draw, s, font, width)
        if len(lines) <= cap:
            return font, lines, lh
    assert font is not None
    kept = lines[:cap]
    rest = " ".join(x for x in lines[cap - 1:] if x)
    kept[-1] = ellipsize(draw, rest + "…", font, width)
    return font, kept, lh


def draw_lines(img: Image.Image, lines: Sequence[str], font: Font, lh: int,
               box: tuple[int, int, int, int], align: str = "left", valign: str = "top") -> None:
    x0, y0, x1, y1 = box
    d = draw_for(img)
    y = y0 + ((y1 - y0 - lh * len(lines)) // 2 if valign == "middle" else 0)
    for ln in lines:
        if align == "center":
            text(d, ((x0 + x1) / 2, y), ln, font, anchor="ma")
        else:
            text(d, (x0, y), ln, font)
        y += lh


def text_image(s: str, align: str = "left") -> Image.Image:
    """Full-screen auto-fitted text (used by the custom page)."""
    img = new_canvas()
    font, lines, lh = fit_text(s, WIDTH - 4, HEIGHT - 2, (16, 14, 12, 11, 10, 9, 8, 7))
    draw_lines(img, lines, font, lh, (2, 1, WIDTH - 3, HEIGHT - 1), align, "middle")
    return img


def notification_image(body: str, title: str | None = None) -> Image.Image:
    """Bordered full-screen notification: bold title line plus wrapped body (<= 3 lines total)."""
    img = Image.new("1", (WIDTH, HEIGHT), 0)
    d = draw_for(img)
    d.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=1)
    d.rectangle((1, 1, WIDTH - 2, HEIGHT - 2), outline=0)
    inner = (4, 3, WIDTH - 5, HEIGHT - 4)
    w, h = inner[2] - inner[0] + 1, inner[3] - inner[1] + 1
    y = inner[1]
    if title and title.strip():
        tf = load_font(10, bold=True)
        tl = line_height(tf)
        text(d, ((inner[0] + inner[2]) / 2, y), ellipsize(d, " ".join(_clean(title).split()), tf, w), tf, anchor="ma")
        d.line((inner[0], y + tl, inner[2], y + tl), fill=1)
        y += tl + 2
        max_lines = 2
    else:
        max_lines = 3
    font, lines, lh = fit_text(body, w, inner[3] - y + 1, (14, 12, 11, 10, 9, 8, 7), max_lines)
    top = y + max(0, (inner[3] - y + 1 - lh * len(lines)) // 2)
    draw_lines(img, lines, font, lh, (inner[0], top, inner[2], inner[3]), "center")
    return img


def to_lcd_image(src: Image.Image) -> Image.Image:
    """Fit/letterbox any image to 160x43, 1-bit Floyd-Steinberg; dark source pixels -> ON."""
    from PIL import ImageOps
    if src.mode in ("RGBA", "LA", "P"):
        src = src.convert("RGBA")
        bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
        src = Image.alpha_composite(bg, src)
    gray = src.convert("L")
    gray = ImageOps.contain(gray, (WIDTH, HEIGHT), Image.LANCZOS)
    canvas = Image.new("L", (WIDTH, HEIGHT), 255)
    canvas.paste(gray, ((WIDTH - gray.width) // 2, (HEIGHT - gray.height) // 2))
    return ImageOps.invert(canvas).convert("1", dither=Image.FLOYDSTEINBERG)
