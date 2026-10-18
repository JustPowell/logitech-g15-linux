"""Persistent page whose content is pushed in over IPC (`g15ctl text` / `g15ctl image`)."""
from __future__ import annotations

from datetime import datetime

from PIL import Image

from ..render import draw_for, load_font, new_canvas, text, text_image, to_lcd_image
from . import Page, register


@register
class CustomPage(Page):
    name = "custom"
    interval = 1.0

    def __init__(self) -> None:
        self._frame: Image.Image | None = None

    @property
    def empty(self) -> bool:
        return self._frame is None

    def set_text(self, s: str, align: str = "left") -> None:
        if align not in ("left", "center"):
            raise ValueError("align must be 'left' or 'center'")
        self._frame = text_image(s, align) if s.strip() else None

    def set_image(self, img: Image.Image) -> None:
        self._frame = to_lcd_image(img)

    def render(self, now: datetime) -> Image.Image:
        if self._frame is not None:
            return self._frame.copy()
        img = new_canvas()
        d = draw_for(img)
        text(d, (80, 12), "custom page", load_font(11, bold=True), anchor="mm")
        text(d, (80, 28), 'g15ctl text "hi"', load_font(10), anchor="mm")
        return img
