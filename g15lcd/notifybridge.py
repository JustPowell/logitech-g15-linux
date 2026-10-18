"""Mirror desktop notifications (org.freedesktop.Notifications) onto the LCD.

A dedicated thread owns a private session-bus connection that *monitors*
``Notify`` method calls (``BecomeMonitor``, falling back to the classic
eavesdrop match). The server's own pop-ups are untouched. Results reach the
daemon through ``deliver``, which the daemon wires to ``call_soon_threadsafe``.

Everything except ``NotifyBridge`` is pure so it can be unit tested without D-Bus.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .config import URGENCIES, DesktopConfig
from .ipc import MAX_TEXT

log = logging.getLogger("g15lcd.notifybridge")

IFACE = "org.freedesktop.Notifications"
RULE = f"type='method_call',interface='{IFACE}',member='Notify'"
MAX_SECONDS = 300.0

_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_TAG = re.compile(r"<[^>]*>")


@dataclass
class DesktopNote:
    app: str
    replaces_id: int
    summary: str
    body: str
    urgency: int  # 0 low, 1 normal, 2 critical


def strip_markup(s: str) -> str:
    """Remove the simple HTML subset notification bodies allow (<b>, <i>, <u>, <a href>,
    <img>, <br>); <br> becomes a newline. Entities are unescaped; whitespace is collapsed
    within each line; the result is truncated to MAX_TEXT."""
    s = _BR.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    lines = (" ".join(ln.split()) for ln in s.replace("\r", "\n").split("\n"))
    return "\n".join(ln for ln in lines if ln)[:MAX_TEXT]


def parse_notify(args: Sequence[Any]) -> DesktopNote | None:
    """Parse Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)."""
    if len(args) < 7:
        return None
    app, rid, _icon, summary, body, _actions, hints = args[:7]
    urgency = 1
    if hints and "urgency" in hints:
        try:
            urgency = int(hints["urgency"])
        except (TypeError, ValueError):
            pass
    return DesktopNote(str(app), int(rid), str(summary), str(body), min(2, max(0, urgency)))


def allowed(note: DesktopNote, cfg: DesktopConfig) -> bool:
    """Apply app include/exclude (case-insensitive) and the minimum urgency."""
    app = note.app.lower()
    if cfg.include_apps and app not in {a.lower() for a in cfg.include_apps}:
        return False
    if app in {a.lower() for a in cfg.exclude_apps}:
        return False
    return note.urgency >= URGENCIES.index(cfg.min_urgency)


def display_seconds(note: DesktopNote, cfg: DesktopConfig, default: float) -> float:
    base = cfg.seconds if cfg.seconds is not None else default
    return min(MAX_SECONDS, base * 2) if note.urgency >= 2 else min(MAX_SECONDS, base)


def format_note(note: DesktopNote, cfg: DesktopConfig) -> tuple[str | None, str]:
    """(title, text) for ``notification_image``.

    With ``show_app`` the bold title line is the app name and the body area holds the
    summary then the message text; otherwise the summary is the title and the body is
    the text. ``hide_body`` drops the message text.
    """
    summary = " ".join(strip_markup(note.summary).split())
    body = "" if cfg.hide_body else " ".join(strip_markup(note.body).split())
    app = " ".join(note.app.split())
    if cfg.show_app and app:
        return app, "\n".join(x for x in (summary, body) if x)
    return summary or None, body


def prepare(note: DesktopNote, cfg: DesktopConfig, default_seconds: float
            ) -> tuple[str | None, str, float, tuple, int] | None:
    """Filter and format: (title, text, seconds, key, replaces_id), or None to skip."""
    if not allowed(note, cfg):
        return None
    if not (strip_markup(note.summary) or strip_markup(note.body)):
        return None
    title, text = format_note(note, cfg)
    if not (title or text):
        return None
    key = (note.app.lower(), " ".join(strip_markup(note.summary).split()))
    return title, text, display_seconds(note, cfg, default_seconds), key, note.replaces_id


class NotifyBridge:
    """Runs the monitor thread. ``deliver(title, text, seconds, key, rid)`` is called from
    that thread, so it must be thread-safe (use ``loop.call_soon_threadsafe``)."""

    def __init__(self, cfg: DesktopConfig, default_seconds: float,
                 deliver: Callable[[str | None, str, float, tuple, int], None]) -> None:
        self.cfg = cfg
        self.default_seconds = default_seconds
        self.deliver = deliver
        self._thread: threading.Thread | None = None
        self._glib_loop: Any = None
        self._bus: Any = None
        self._started = threading.Event()
        self.active = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="notifybridge", daemon=True)
        self._thread.start()
        self._started.wait(5.0)

    def stop(self) -> None:
        loop = self._glib_loop
        if loop is not None:
            loop.quit()  # thread-safe
        if self._thread is not None:
            self._thread.join(3.0)
            self._thread = None

    # -- thread body ----------------------------------------------------
    def _run(self) -> None:
        try:
            import dbus
            import dbus.mainloop.glib
            from gi.repository import GLib
        except Exception as e:
            log.warning("desktop notification bridge disabled: %s", e)
            self._started.set()
            return
        try:
            # dbus-python's GLib integration attaches to the default main context; nothing
            # else in this process iterates it, and this thread is the only one that runs it.
            bus = dbus.SessionBus(mainloop=dbus.mainloop.glib.DBusGMainLoop(), private=True)
            self._bus = bus
            if not self._monitor(dbus, bus):
                log.warning("desktop notification bridge disabled: could not monitor the session bus")
                self._started.set()
                return
            bus.add_message_filter(self._filter)
            self._glib_loop = GLib.MainLoop()
            self.active = True
            log.info("desktop notification bridge active")
            self._started.set()
            self._glib_loop.run()
        except Exception as e:
            log.warning("desktop notification bridge disabled: %s", e)
        finally:
            self.active = False
            self._started.set()
            if self._bus is not None:
                try:
                    self._bus.close()
                except Exception:
                    pass
                self._bus = None

    @staticmethod
    def _monitor(dbus: Any, bus: Any) -> bool:
        try:
            obj = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            dbus.Interface(obj, "org.freedesktop.DBus.Monitoring").BecomeMonitor([RULE], 0)
            log.debug("using BecomeMonitor")
            return True
        except Exception as e:
            log.debug("BecomeMonitor failed (%s); trying eavesdrop match", e)
        try:
            bus.add_match_string("eavesdrop='true'," + RULE)
            log.debug("using eavesdrop match")
            return True
        except Exception as e:
            log.debug("eavesdrop match failed: %s", e)
            return False

    def _filter(self, _bus: Any, msg: Any) -> None:
        try:
            if msg.get_member() != "Notify" or msg.get_interface() != IFACE:
                return
            note = parse_notify(msg.get_args_list())
            if note is None:
                return
            out = prepare(note, self.cfg, self.default_seconds)
            log.debug("notify from %r: %s", note.app, "mirrored" if out else "filtered")
            if out:
                self.deliver(*out)
        except Exception:
            log.exception("failed to process a notification")
