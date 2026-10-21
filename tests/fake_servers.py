"""Tiny stand-ins for the servers the Output tab talks to: an MQTT 3.1.1 broker (login, topic permissions,
slow / silent modes) and an HTTP server. Only what the connection check and the bridge need."""
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_packet(sock):
    first = _read_exact(sock, 1)[0]
    mult, length = 1, 0
    while True:
        b = _read_exact(sock, 1)[0]
        length += (b & 127) * mult
        if not b & 128: break
        mult *= 128
    return first >> 4, first & 15, _read_exact(sock, length) if length else b""


def _str(buf, off):
    n = int.from_bytes(buf[off:off + 2], "big")
    return buf[off + 2:off + 2 + n].decode(), off + 2 + n


class FakeMqttBroker:
    """users=None -> anonymous clients allowed; users={"name": "pw"} -> login required."""
    def __init__(self, users=None, deny_subscribe=(), connack_delay=0.0, silent=False):
        self.users, self.deny_subscribe = users, set(deny_subscribe)
        self.connack_delay, self.silent = connack_delay, silent
        self.connects = []          # (username, password) of every CONNECT received
        self.subscribed = []
        outer = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                s = self.request
                try:
                    while True:
                        ptype, flags, body = _read_packet(s)
                        if ptype == 1:
                            outer._on_connect(s, body)
                        elif ptype == 8:
                            outer._on_subscribe(s, body)
                        elif ptype == 12:
                            s.sendall(b"\xd0\x00")
                        elif ptype == 14:
                            return
                except (ConnectionError, OSError):
                    return

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _on_connect(self, s, body):
        off = 2 + int.from_bytes(body[:2], "big") + 1          # protocol name + level
        cflags = body[off]; off += 1 + 2                       # connect flags + keepalive
        _, off = _str(body, off)                               # client id
        if cflags & 0x04:
            _, off = _str(body, off); n = int.from_bytes(body[off:off + 2], "big"); off += 2 + n
        user = pw = None
        if cflags & 0x80: user, off = _str(body, off)
        if cflags & 0x40: pw, off = _str(body, off)
        self.connects.append((user, pw))
        if self.silent: return
        time.sleep(self.connack_delay)
        rc = 0
        if self.users is not None:
            if user is None: rc = 5                            # not authorised
            elif self.users.get(user) != pw: rc = 4            # bad user name or password
        s.sendall(bytes([0x20, 0x02, 0x00, rc]))

    def _on_subscribe(self, s, body):
        pid = body[:2]; off = 2; codes = bytearray()
        while off < len(body):
            topic, off = _str(body, off); qos = body[off]; off += 1
            self.subscribed.append(topic)
            codes.append(0x80 if topic in self.deny_subscribe else min(qos, 1))
        s.sendall(bytes([0x90, 2 + len(codes)]) + pid + bytes(codes))

    def close(self):
        self.server.shutdown(); self.server.server_close()


class FakeHttpServer:
    def __init__(self, status=200):
        outer = self
        self.status, self.requests = status, []

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                outer.requests.append(self.command)
                self.send_response(outer.status); self.end_headers()
            do_HEAD = do_GET = do_POST = _answer
            def log_message(self, *a): pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown(); self.server.server_close()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
