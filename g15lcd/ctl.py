"""g15ctl: command-line client for the g15lcd control socket."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

from .ipc import default_socket_path

TIMEOUT = 10.0
NO_DAEMON = "g15ctl: cannot reach g15lcd at {path}: {err}\n  is g15lcd running? systemctl --user status g15lcd"


def _unescape(s: str) -> str:
    return s.replace("\\n", "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="g15ctl", description="Control the g15lcd daemon")
    ap.add_argument("--socket", metavar="PATH", help="socket path (default: $XDG_RUNTIME_DIR/g15lcd.sock)")
    sub = ap.add_subparsers(dest="command", required=True)
    n = sub.add_parser("notify", help="show a notification overlay")
    n.add_argument("text")
    n.add_argument("-t", "--title")
    n.add_argument("-s", "--seconds", type=float)
    t = sub.add_parser("text", help="set the custom page text ('-' or no TEXT reads stdin)")
    t.add_argument("text", nargs="?", default="-")
    t.add_argument("--align", choices=("left", "center"), default="left")
    t.add_argument("--show", action="store_true", help="switch to the custom page")
    i = sub.add_parser("image", help="show an image on the custom page")
    i.add_argument("path")
    i.add_argument("--show", action="store_true")
    p = sub.add_parser("page", help="switch page: next, prev or a page name")
    p.add_argument("name")
    sub.add_parser("pages", help="list pages")
    b = sub.add_parser("backlight", help="set backlight level")
    b.add_argument("level", choices=("0", "1", "2", "toggle"))
    sub.add_parser("ping", help="check the daemon is alive")
    return ap


def build_request(args: argparse.Namespace, stdin: Any = None) -> dict[str, Any]:
    c = args.command
    if c == "notify":
        req: dict[str, Any] = {"cmd": "notify", "text": _unescape(args.text)}
        if args.title is not None:
            req["title"] = args.title
        if args.seconds is not None:
            req["seconds"] = args.seconds
        return req
    if c == "text":
        body = (stdin or sys.stdin).read() if args.text == "-" else _unescape(args.text)
        req = {"cmd": "text", "text": body.rstrip("\n") if args.text == "-" else body}
        if args.align != "left":
            req["align"] = args.align
        if args.show:
            req["show"] = True
        return req
    if c == "image":
        req = {"cmd": "image", "path": os.path.abspath(args.path)}
        if args.show:
            req["show"] = True
        return req
    if c == "page":
        return {"cmd": "page", "name": args.name}
    if c == "backlight":
        return {"cmd": "backlight", "level": args.level if args.level == "toggle" else int(args.level)}
    return {"cmd": c}


def send(path: str, req: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        with s.makefile("rb") as f:
            line = f.readline()
    if not line:
        raise ConnectionError("daemon closed the connection")
    return json.loads(line)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.socket or default_socket_path()
    req = build_request(args)
    try:
        reply = send(path, req)
    except (OSError, ConnectionError) as e:
        print(NO_DAEMON.format(path=path, err=e), file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"g15ctl: bad reply from daemon: {e}", file=sys.stderr)
        return 1
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
