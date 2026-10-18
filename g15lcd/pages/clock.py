"""Big clock page: HH:MM, date, seconds progress bar."""
from __future__ import annotations

from datetime import datetime

from PIL import Image

from ..render import draw_for, hbar, load_font, new_canvas, text
from . import Page, register


@register
class ClockPage(Page):
    name = "clock"
    interval = 0.25

    def render(self, now: datetime) -> Image.Image:
        img = new_canvas()
        d = draw_for(img)
        text(d, (2, 34), now.strftime("%H:%M"), load_font(36, bold=True), anchor="ls")
        right = 157
        text(d, (right, 5), now.strftime("%a"), load_font(14, bold=True), anchor="ra")
        text(d, (right, 21), now.strftime("%d %b"), load_font(11), anchor="ra")
        hbar(d, (0, 38, 159, 42), (now.second + now.microsecond / 1e6) / 60)
        return img
