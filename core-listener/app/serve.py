# File:         serve.py
# Purpose:      Starts the listener exactly as the old `uvicorn app.main:app ...` command line did (same flags) and, when asked, ALSO opens the STAFF DOOR: a second port served by the
#               same server, so the application can tell which door a request used. Staff may sign in only through that door (main.py); Core publishes it on its own loopback and
#               reaches it only through Tailscale Serve, so only the two office PCs can ever get there.
# Inputs:       The uvicorn flags the compose file passes (--host, --port, --timeout-graceful-shutdown, --no-proxy-headers); optional environment WATCHDOG_STAFF_DOOR_PORT (the port to
#               open, 1024 to 65535, never the main one) and WATCHDOG_STAFF_DOOR_HOST (default 0.0.0.0, the container's own network).
# Outputs:      A running listener. WATCHDOG_STAFF_DOOR_BOUND is set in its environment to the door's port, and ONLY if the second port really opened.
# Dependencies: uvicorn (imported when the server starts, so the rules below can be tested without it), app.main.
"""Why this exists, in plain words: one process, one set of background loops, two doors. uvicorn can listen on several sockets for one server (Server.run(sockets=[...])), and
for every request it fills scope["server"] from the socket that accepted the connection, never from the request, so a client cannot fake which door it used. The staff door can
never stop the listener: if its port is busy or unusable the listener starts without it, says so in the log, and staff simply cannot sign in until that is fixed (alerting, the
dashboard and the super user are unaffected).
"""
import argparse
import logging
import os
import socket
import sys
from typing import Optional

logger = logging.getLogger("serve")

DOOR_ENV = "WATCHDOG_STAFF_DOOR_PORT"           # what the operator asks for
DOOR_HOST_ENV = "WATCHDOG_STAFF_DOOR_HOST"      # where the door listens (inside the container, the whole container network)
BOUND_ENV = "WATCHDOG_STAFF_DOOR_BOUND"         # what the application believes: set here, only after the second socket exists
MIN_DOOR_PORT, MAX_DOOR_PORT = 1024, 65535
DOOR_HOST_DEFAULT = "0.0.0.0"  # nosec B104 -- inside the container this MUST be the whole container network or Docker's published port could never reach the door; compose publishes it on Core's loopback only (127.0.0.1:8421)
APP = "app.main:app"


def parse_arguments(argv) -> argparse.Namespace:
    """The flags production has always started uvicorn with. A flag this launcher does not know stops it loudly instead of being ignored, because a flag that silently does
    nothing (--no-proxy-headers above all) would change who the listener thinks is asking."""
    parser = argparse.ArgumentParser(prog="serve", description="Start the listener (and, when asked, the staff door).")
    parser.add_argument("--host", default="127.0.0.1")                           # the safe default; production always names its address (compose and the Dockerfile pass --host 0.0.0.0, the unit passes 127.0.0.1)
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--timeout-graceful-shutdown", dest="timeout_graceful_shutdown", type=int, default=None)
    parser.add_argument("--no-proxy-headers", dest="no_proxy_headers", action="store_true")
    return parser.parse_args(argv)


def door_plan(environ, main_port: int) -> tuple:
    """(the port to open as the staff door or None, a note saying why not). Nothing asked is not a complaint; anything asked that cannot be used is no door, with the reason."""
    text = str(environ.get(DOOR_ENV, "")).strip()
    if not text:
        return None, ""
    if not (text.isascii() and text.isdigit()) or not MIN_DOOR_PORT <= int(text) <= MAX_DOOR_PORT:
        return None, f"{DOOR_ENV}={text[:20]!r} is not a usable port ({MIN_DOOR_PORT} to {MAX_DOOR_PORT}); there is no staff door"
    if int(text) == main_port:
        return None, f"{DOOR_ENV} is the main port ({main_port}); a door must be a second port, so there is no staff door"
    return int(text), ""


def open_door(host: str, port: int) -> socket.socket:
    """The staff door's socket, bound (uvicorn starts listening on it). Written here, not taken from uvicorn's own helper: that one ends the whole process when a port is busy
    (sys.exit), and a busy staff port must only ever mean "no staff door", never "no listener"."""
    door = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        door.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        door.bind((host, port))
        door.set_inheritable(True)
    except OSError:
        door.close()
        raise
    return door


def main(argv: Optional[list] = None) -> None:
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    import uvicorn                                                       # here, not at the top: the rules above need no server to be tested

    os.environ.pop(BOUND_ENV, None)                                      # only this launcher may say a door exists; one inherited from outside is never believed
    config = uvicorn.Config(APP, host=args.host, port=args.port, proxy_headers=not args.no_proxy_headers, timeout_graceful_shutdown=args.timeout_graceful_shutdown)
    sockets = [config.bind_socket()]
    port, note = door_plan(os.environ, args.port)
    if note:
        logger.warning("[serve.py::main] %s", note)
    if port is not None:
        try:
            door = open_door(os.environ.get(DOOR_HOST_ENV, DOOR_HOST_DEFAULT), port)
        except OSError as error:
            logger.error("[serve.py::main] the staff door could not open on port %s (%s); the listener starts without it and staff cannot sign in until it is fixed", port, error)
        else:
            sockets.append(door)
            os.environ[BOUND_ENV] = str(port)
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    main()
