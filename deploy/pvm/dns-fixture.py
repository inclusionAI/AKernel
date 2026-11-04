#!/usr/bin/env python3
"""Answer reserved PVM CI names inside the disposable L1, without public DNS changes."""

import socket
import socketserver
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
subprocess.run(["python3", str(ROOT / "src/sandboxd/test/e2e/pvm-l1.py")], check=True)
NAMES = {
    "blocked.pvm-ci.test": "192.0.2.44",
    "child.blocked.pvm-ci.test": "192.0.2.44",
    "allowed.pvm-ci.test": "172.17.0.1",
}


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data, server = self.request
        if len(data) < 12:
            return
        labels = []
        offset = 12
        while offset < len(data) and data[offset]:
            length = data[offset]
            if length > 63 or offset + length + 1 >= len(data):
                return
            labels.append(
                data[offset + 1 : offset + length + 1].decode("ascii", errors="ignore")
            )
            offset += length + 1
        offset += 1
        if offset + 4 > len(data):
            return
        name = ".".join(labels).lower()
        kind, cls = struct.unpack_from("!HH", data, offset)
        question = data[12 : offset + 4]
        known = name in NAMES
        answer = (
            (
                b"\xc0\x0c"
                + struct.pack("!HHIH", 1, 1, 5, 4)
                + socket.inet_aton(NAMES[name])
            )
            if known and kind == 1 and cls == 1
            else b""
        )
        response = (
            data[:2]
            + struct.pack(
                "!HHHHH", 0x8180 if known else 0x8183, 1, 1 if answer else 0, 0, 0
            )
            + question
            + answer
        )
        server.sendto(response, self.client_address)


with socketserver.UDPServer(("172.17.0.1", 53), Handler) as server:
    server.serve_forever()
