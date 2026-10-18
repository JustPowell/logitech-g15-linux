# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`g15lcd`: a user-level Python (>=3.11) asyncio daemon that drives the 160x43 1-bpp LCD of the Logitech G15 gen 1 (`046d:c222`) through `/dev/hidraw*`, plus `g15ctl`, a JSON-over-Unix-socket CLI client. It coexists with the kernel `hid-lg-g15` driver (which owns G-keys/M-keys/backlights) — never detach or replace that driver, and never handle G-keys in the daemon. README.md has the hardware report format, install steps, and the control protocol table; keep that table in sync when adding IPC commands.

## Commands

The project runs from the checkout (no install; `pip install --user` is blocked on Arch). Dependencies are system packages: `python-pillow`, optional `python-psutil` (sysstats), `python-dbus` (media). Docs and command examples use fish syntax.

```fish
python -m unittest discover -v                               # all tests (stdlib unittest, no pytest)
python -m unittest tests.test_daemon                          # one module
python -m unittest tests.test_daemon.ClockTests.test_render   # one test
set -x PYTHONPATH $PWD; python -m g15lcd --render-page sysstats previews/out.png   # render a page, no device
python -m g15lcd -v                                           # run daemon in foreground (stop the service first)
systemctl --user restart g15lcd; journalctl --user -u g15lcd -f
python scripts/spike.py pattern|buttons|clear                 # raw hardware check
```

`packaging/g15lcd` and `packaging/g15ctl` are shell wrappers symlinked into `~/.local/bin` that set `PYTHONPATH` to this checkout, so the installed service runs the working tree — a restart picks up edits. No linter is configured.

## Architecture

- **`device.py`** — hidraw discovery (by `HID_ID` in sysfs uevent), `pack()` image→992-byte report `0x03`, `parse_buttons()` for input report `0x02`, `ButtonEdges` (press edges only). Errors surface as `DeviceGone`.
- **`daemon.py`** — `Daemon` owns everything: `PageManager` (cyclic page list), `Overlay` (transient full-screen images: page-name labels and a queued notification FIFO), `Backlight` (sysfs), the device, and the IPC server. Single asyncio loop: `_render_due()` renders the current page at its `interval` (faster while an overlay is up), composites the overlay, writes the frame, and returns the next wakeup; `wake()` forces an immediate re-render. The device fd is read via `loop.add_reader`; on `DeviceGone` it detaches and retries opening every `RETRY_SECONDS` (hot-replug). MENU1–3 are handled by the daemon (backlight, prev, next); MENU4/5 go to `page.on_button()`. While a notification (not a label) is showing, MENU2–5 are consumed to dismiss it (`Overlay.dismiss`/`notifying`); `Overlay.upsert` coalesces notifications by key/`replaces_id`. Sends `sd_notify READY=1` (Type=notify unit).
- **`notifybridge.py`** — optional (`[notify.desktop] enabled`) desktop-notification mirror: a thread with a private session-bus connection monitors `org.freedesktop.Notifications.Notify` (BecomeMonitor, eavesdrop fallback) and hands formatted results to `Daemon.notify_desktop` via `call_soon_threadsafe`. Parsing/filtering/markup stripping are pure functions (tested without D-Bus); keep it that way.
- **`pages/`** — `Page` base (`name`, `interval`, `render(now) -> 1-bit 160x43 Image`, `on_button`, async `start`/`stop`). Pages register with `@register` and are **lazily imported by name** (`pages.create(name, options)` imports `g15lcd.pages.<name>`); a page whose optional dependency is missing raises and the daemon skips it with a log. `[page.<name>]` config tables are passed as constructor kwargs, filtered to the parameters the constructor accepts (unknown keys warn). Adding a page = new module in `pages/` + `@register`; no central list to edit (only `DEFAULT_PAGES` in `config.py` for the default set).
  - `sysstats` samples in an asyncio task; `media` polls MPRIS via dbus in a **separate thread** (`MprisSource`) with commands queued to it — keep dbus calls off the event loop. Both take injectable sources/clocks so tests run without psutil/dbus/hardware.
- **`render.py`** — all drawing helpers (fonts, bars, sparklines, wrapping/fit-text, notification/text images, `to_lcd_image` fit+dither). Canvases are mode `"1"`, pixel 1 = ON. Use these rather than drawing ad hoc.
- **`ipc.py`** — JSON-lines server at `$XDG_RUNTIME_DIR/g15lcd.sock`; `make_handlers(daemon)` is the command table (handlers may be sync or async; raise `ValueError` for client errors → `{"ok": false}`). Input limits (`MAX_LINE`, `MAX_IMAGE`, `MAX_PIXELS`, `MAX_TEXT`) are enforced here. `text`/`image` commands need the `custom` page enabled.
- **`ctl.py`** — argparse → request dict (`build_request`) → `send()`; `packaging/g15ctl.fish` completes page names from the live daemon.
- **`config.py`** — TOML at `$XDG_CONFIG_HOME/g15lcd/config.toml`, all keys optional, strict validation; `ConfigError` → exit status 2. Document new keys in `packaging/config.example.toml`.

## Conventions

- `previews/` is git-ignored scratch; refreshed screenshots for the README go in `docs/img/` (4x PNG, dark = ON).
- Tests use fakes rather than hardware; new pages should accept injectable data sources so `render()` is testable.