"""End-to-end tests: run tcpmeter as a subprocess in front of a local upstream."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "tcpmeter.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def recv_all(sock):
    total = 0
    while True:
        data = sock.recv(65536)
        if not data:
            return total
        total += len(data)


class Upstream(threading.Thread):
    """Reads until the client half-closes, then replies with `reply` bytes."""

    def __init__(self, reply):
        super().__init__(daemon=True)
        self.reply = reply
        self.received = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self.received.append(recv_all(conn))
                conn.sendall(b"y" * self.reply)


class TcpmeterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.upstream = Upstream(reply=3_000_000)
        self.upstream.start()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.upstream.sock.close()
        self.tmp.cleanup()

    def start_meter(self, *extra, new_route=True):
        """Start tcpmeter on a fresh port (or the previous one, to reuse its route)."""
        if new_route:
            self.port = free_port()
            self.route = f"127.0.0.1:{self.port}=127.0.0.1:{self.upstream.port}"
        self.proc = subprocess.Popen(
            [sys.executable, SCRIPT, "-s", self.state, "-i", "0.2", *extra, self.route],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = self.proc.stdout.readline()
        self.assertIn("relaying", line)

    def stop_meter(self):
        self.proc.send_signal(signal.SIGTERM)
        out, _ = self.proc.communicate(timeout=5)
        self.assertEqual(self.proc.returncode, 0, out)
        with open(self.state) as f:
            return json.load(f)["routes"][self.route]

    def transfer(self, nbytes):
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            c.sendall(b"x" * nbytes)
            c.shutdown(socket.SHUT_WR)  # half-close must reach the upstream
            return recv_all(c)

    def test_counts_exact_bytes(self):
        self.start_meter()
        for _ in range(2):
            self.assertEqual(self.transfer(5_000_000), 3_000_000)
        r = self.stop_meter()
        self.assertEqual(r["tx"], 10_000_000)
        self.assertEqual(r["rx"], 6_000_000)
        self.assertEqual(r["conns"], 2)

    def test_totals_persist_and_reset(self):
        self.start_meter()
        self.transfer(1000)
        self.stop_meter()

        self.start_meter(new_route=False)
        self.transfer(1000)
        r = self.stop_meter()
        self.assertEqual(r["tx"], 2000)
        self.assertEqual(r["rx"], 6_000_000)

        self.start_meter("--reset", new_route=False)
        self.transfer(1000)
        r = self.stop_meter()
        self.assertEqual(r["tx"], 1000)

    def test_upstream_unreachable(self):
        dead = free_port()
        self.port = free_port()
        self.route = f"127.0.0.1:{self.port}=127.0.0.1:{dead}"
        self.start_meter(new_route=False)
        with socket.create_connection(("127.0.0.1", self.port)) as c:
            self.assertEqual(recv_all(c), 0)  # closed, not hung
        self.proc.send_signal(signal.SIGTERM)
        out, _ = self.proc.communicate(timeout=5)
        self.assertIn("upstream connect failed", out)


if __name__ == "__main__":
    unittest.main()
