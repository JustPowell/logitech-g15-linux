"""TOML configuration: $XDG_CONFIG_HOME/g15lcd/config.toml (all keys optional)."""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("g15lcd.config")

DEFAULT_PAGES = ["clock", "sysstats", "gpu", "media", "custom"]
_KNOWN = {"pages": None, "notify": {"default_seconds", "desktop"}, "backlight": {"on_level"}, "page": None}
_DESKTOP_KEYS = {"enabled", "include_apps", "exclude_apps", "min_urgency", "hide_body", "show_app", "seconds"}
URGENCIES = ("low", "normal", "critical")


class ConfigError(Exception):
    pass


@dataclass
class DesktopConfig:
    """``[notify.desktop]``: mirror org.freedesktop.Notifications pop-ups onto the LCD."""
    enabled: bool = False
    include_apps: list[str] = field(default_factory=list)  # empty = all
    exclude_apps: list[str] = field(default_factory=list)
    min_urgency: str = "normal"
    hide_body: bool = False
    show_app: bool = True
    seconds: float | None = None  # None = [notify] default_seconds


@dataclass
class Config:
    pages: list[str] = field(default_factory=lambda: list(DEFAULT_PAGES))
    notify_seconds: float = 5.0
    backlight_on: int = 2
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    page_options: dict[str, dict[str, Any]] = field(default_factory=dict)


def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "g15lcd", "config.toml")


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    v = data.get(key, {})
    if not isinstance(v, dict):
        raise ConfigError(f"[{key}] must be a table")
    return v


def _parse_desktop(tbl: dict[str, Any]) -> DesktopConfig:
    for k in tbl.keys() - _DESKTOP_KEYS:
        log.warning("config: unknown key %r in [notify.desktop] ignored", k)
    out = DesktopConfig()

    def bad(k: str) -> ConfigError:
        return ConfigError(f"[notify.desktop] {k} has an invalid value: {tbl[k]!r}")

    for k in ("enabled", "hide_body", "show_app"):
        if k in tbl:
            if not isinstance(tbl[k], bool):
                raise bad(k)
            setattr(out, k, tbl[k])
    for k in ("include_apps", "exclude_apps"):
        if k in tbl:
            v = tbl[k]
            if not (isinstance(v, list) and all(isinstance(a, str) for a in v)):
                raise bad(k)
            setattr(out, k, list(v))
    if "min_urgency" in tbl:
        if tbl["min_urgency"] not in URGENCIES:
            raise bad("min_urgency")
        out.min_urgency = tbl["min_urgency"]
    if "seconds" in tbl:
        v = tbl["seconds"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 300:
            raise bad("seconds")
        out.seconds = float(v)
    return out


def parse(data: dict[str, Any]) -> Config:
    cfg = Config()
    for k in data.keys() - _KNOWN.keys():
        log.warning("config: unknown key %r ignored", k)
    if "pages" in data:
        pages = data["pages"]
        if not (isinstance(pages, list) and pages and all(isinstance(p, str) for p in pages)):
            raise ConfigError("'pages' must be a non-empty list of page names")
        cfg.pages = list(dict.fromkeys(pages))  # dedupe, keep order
    for section, attr, check in (
        ("notify", "default_seconds", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 300),
        ("backlight", "on_level", lambda v: v in (0, 1, 2) and isinstance(v, int) and not isinstance(v, bool)),
    ):
        tbl = _table(data, section)
        for k in tbl.keys() - _KNOWN[section]:
            log.warning("config: unknown key %r in [%s] ignored", k, section)
        if attr in tbl:
            if not check(tbl[attr]):
                raise ConfigError(f"[{section}] {attr} has an invalid value: {tbl[attr]!r}")
            setattr(cfg, "notify_seconds" if section == "notify" else "backlight_on",
                    float(tbl[attr]) if section == "notify" else tbl[attr])
    desktop = _table(data, "notify").get("desktop", {})
    if not isinstance(desktop, dict):
        raise ConfigError("[notify.desktop] must be a table")
    cfg.desktop = _parse_desktop(desktop)
    for name, opts in _table(data, "page").items():
        if not isinstance(opts, dict):
            raise ConfigError(f"[page.{name}] must be a table")
        if name not in cfg.pages:
            log.warning("config: [page.%s] given but page not in 'pages'", name)
        cfg.page_options[name] = opts
    return cfg


def load(path: str | None = None) -> Config:
    """Load config. A missing default file gives defaults; a missing explicit path is an error."""
    explicit = path is not None
    path = path or default_config_path()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"config file not found: {path}") from None
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse(data)
