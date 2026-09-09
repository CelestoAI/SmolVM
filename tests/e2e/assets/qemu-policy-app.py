"""Disposable QEMU test application. Never included in a published guest image."""

import base64
import hashlib
import json
import socket
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if self.path.startswith("/probe?"):
            transport = query.get("transport", ["tcp"])[0]
            family = socket.AF_INET6 if ":" in query["host"][0] else socket.AF_INET
            kind = socket.SOCK_DGRAM if transport in {"udp", "dns"} else socket.SOCK_STREAM
            with socket.socket(family, kind) as connection:
                connection.settimeout(1)
                try:
                    connection.connect((query["host"][0], int(query["port"][0])))
                    if transport == "dns":
                        connection.send(
                            bytes.fromhex("abcd01000001000000000000")
                            + b"\x07example\x03com\x00\x00\x01\x00\x01"
                        )
                        connection.recv(4096)
                    elif transport == "udp":
                        connection.send(b"policy-test")
                        connection.recv(4096)
                    reached = True
                except OSError:
                    reached = False
            body = json.dumps({"reached": reached}).encode()
        elif self.path == "/ws":
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header(
                "Sec-WebSocket-Accept",
                base64.b64encode(hashlib.sha1(key.encode()).digest()).decode(),
            )
            self.end_headers()
            # One small masked client frame and one unmasked echo are sufficient
            # to exercise an upgraded, bidirectional application connection.
            header = self.rfile.read(2)
            length = header[1] & 127
            if length == 126:
                length = struct.unpack("!H", self.rfile.read(2))[0]
            mask = self.rfile.read(4)
            payload = self.rfile.read(length)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.wfile.write(bytes([0x81, len(payload)]) + payload)
            return
        else:
            body = b"ready\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
