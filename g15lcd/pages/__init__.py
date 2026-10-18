"""Page base class and registry."""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import re
from datetime import datetime
from typing import Any

from PIL import Image

from ..render import new_canvas

log = logging.getLogger("g15lcd.pages")


class Page:
    name = "page"
    interval = 1.0  # seconds between re-renders

    def render(self, now: datetime) -> Image.Image:
        return new_canvas()

    def on_button(self, name: str) -> bool:
        """Handle MENU4/MENU5 (or others). Return True if handled."""
        return False

    async def start(self) -> None:
        """Called when the daemon starts (spawn background work here)."""

    async def stop(self) -> None:
        """Called on daemon shutdown."""


_REGISTRY: dict[str, type[Page]] = {}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def register(cls: type[Page]) -> type[Page]:
    _REGISTRY[cls.name] = cls
    return cls


def _load(name: str) -> None:
    """Import ``g15lcd.pages.<name>`` so its @register runs (lazy registry)."""
    if not _NAME_RE.match(name):
        return
    modname = f"{__name__}.{name}"
    try:
        importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name != modname:  # the page's own dependency is missing: surface it
            raise


def _accepted_options(cls: type[Page], options: dict[str, Any]) -> dict[str, Any]:
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        params = {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(options)
    ok = {k: v for k, v in options.items()
          if k in params and params[k].kind is not inspect.Parameter.VAR_POSITIONAL}
    for k in options.keys() - ok.keys():
        log.warning("page %r ignores unknown option %r", cls.name, k)
    return ok


def create(name: str, options: dict[str, Any] | None = None) -> Page:
    """Instantiate page ``name``, importing its module on demand.

    ``options`` are passed as keyword arguments only if the class accepts them.
    """
    if name not in _REGISTRY:
        _load(name)
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"unknown page {name!r}; available: {', '.join(available())}")
    return cls(**_accepted_options(cls, options or {}))


def available() -> list[str]:
    mods = {m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")}
    return sorted(mods | set(_REGISTRY))
