# File:         matrix_fixtures.py
# Purpose:      Shared test material for the AV matrix tests: the REAL answer the owner's matrix gave on 2026-11-09, an independent builder of matrix answers (written from
#               the manufacturer's guide, not from matrix.py, so a mistake in matrix.py cannot hide in both), a fake matrix with the same two methods as the real link, and a stateful
#               stand-in matrix that speaks real UDP on the loopback address.
# Inputs:       None (nothing here talks to a real matrix).
# Outputs:      Constants, builders and test doubles imported by test_matrix_logic.py, test_matrix_wiring.py, test_matrix_panel.py and test_matrix_live.py.
# Dependencies: core-listener/app/matrix.py (only for MatrixError, so the fake raises what the real link raises). Python standard library otherwise.
import importlib
import re
import socket
import sys
import threading
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
matrix = importlib.import_module("app.matrix")

# Read from the real matrix (Core host and listener container, 2026-11-09 about 06:45 New York): output 1..8 <- input.
LIVE_ROUTING = (2, 2, 2, 3, 7, 3, 7, 7)
# The 64 bytes of that real answer, exactly as the matrix sent them. The 35 zero bytes after the eight outputs are the empty IP status and device name.
REAL_REPLY = bytes.fromhex("a56c4000820101" + "00" * 9 + "5300" + "0202020307030707" + "00" * 35 + "4902" + "ae")
# The live routing with only output 5 moved: every output of MAIN PC is right (and outputs 3, 4, 7, 8 are whatever they were).
MAIN_ROUTING = (2, 2, 2, 3, 2, 3, 7, 7)
BACKUP_ROUTING = (7, 7, 2, 3, 7, 5, 7, 7)
EVERYTHING_ON_BRIGHTSIGN = (1, 1, 1, 1, 1, 1, 1, 1)

_ROUTE = re.compile(r"^([1-8])v([1-8](?:,[1-8])*)\.$")


def status_reply(outputs, status=0x00, command=0x53, length_delta=0, checksum_delta=0, end=0xAE):
    """A 64-byte read-status answer built from the manufacturer's layout: header a5 6c, length (low byte first), device type 82, id 01, interface 01, nine reserved
    bytes, command, status, the eight outputs, zero padding, checksum (byte sum, low byte first), end ae. The *_delta arguments make a deliberately wrong one."""
    body = bytes([0xA5, 0x6C, 0, 0, 0x82, 0x01, 0x01]) + bytes(9) + bytes([command, status]) + bytes(outputs)
    body += bytes(61 - len(body))
    body = body[:2] + (64 + length_delta).to_bytes(2, "little") + body[4:]
    return body + ((sum(body) + checksum_delta) & 0xFFFF).to_bytes(2, "little") + bytes([end])


class FakeMatrix:
    """Same two methods as matrix.UdpLink: send(datagram) and read_status() -> tuple of eight numbers (or raises matrix.MatrixError). It behaves like the real
    matrix: a route datagram changes its routing, nothing comes back, a read returns all eight outputs. Switches let a test make it lose, ignore or fail."""

    def __init__(self, routing=LIVE_ROUTING):
        self.routing = list(routing)
        self.sent, self.reads = [], 0
        self.offline = False
        self.drop_sends = 0                      # the next N datagrams are lost on the way
        self.ignore = lambda text: False         # a datagram the matrix receives but does not apply
        self.fail_reads_after_send = False       # once anything was sent, the matrix stops answering reads
        self.fail_sends = 0                      # the next N datagrams cannot be sent at all (the operating system refuses): send() raises MatrixError, nothing reaches the matrix
        self.send_error = lambda text: False     # a datagram whose send raises MatrixError (for example the rollback's)
        self.after_send = None                   # called with the datagram text after it was applied (someone else acts meanwhile)
        self.on_send = None                      # called with the text BEFORE it is applied (a test can hold the switch here)

    def send(self, data):
        text = data.decode("ascii")
        self.sent.append(text)
        if self.on_send:
            self.on_send(text)
        if self.fail_sends > 0 or self.send_error(text):
            self.fail_sends = max(0, self.fail_sends - 1)
            raise matrix.MatrixError("the datagram could not be sent")
        if self.drop_sends > 0:
            self.drop_sends -= 1
            return
        if self.offline or self.ignore(text):
            return
        found = _ROUTE.match(text)
        if found:
            for output in found.group(2).split(","):
                self.routing[int(output) - 1] = int(found.group(1))
        if self.after_send:
            self.after_send(text)

    def read_status(self):
        self.reads += 1
        if self.offline or (self.fail_reads_after_send and self.sent):
            raise matrix.MatrixError("the matrix did not answer")
        return tuple(self.routing)


class StandInMatrix:
    """A stateful matrix on a real UDP socket (127.0.0.1, any free port), for the transport and end-to-end tests. It answers the read-status packet with a valid 64-byte
    reply and applies the ASCII route form. `replies` is a list that overrides what the next read answers: bytes = send exactly that, None = stay silent, and an
    empty list means answer normally."""

    def __init__(self, routing=LIVE_ROUTING):
        self.routing = list(routing)
        self.received, self.sources, self.replies = [], [], []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                data, source = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.received.append(data)
            self.sources.append(source)                # where each question came from, so a test can answer it from somewhere else
            if len(data) == 20 and data[:2] == b"\xa5\x6c" and data[16] == 0x53:
                reply = self.replies.pop(0) if self.replies else status_reply(self.routing)
                for chunk in (reply if isinstance(reply, list) else [reply]):
                    if chunk is not None:
                        self.sock.sendto(chunk, source)
                continue
            found = _ROUTE.match(data.decode("ascii", "replace"))
            if found:
                for output in found.group(2).split(","):
                    self.routing[int(output) - 1] = int(found.group(1))

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()
