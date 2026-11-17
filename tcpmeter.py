#!/usr/bin/env python3
"""tcpmeter - a byte-counting TCP relay.

Point your applications at tcpmeter instead of the real proxy; it forwards the
raw TCP stream unchanged and counts every payload byte in each direction.
Because it never parses the stream, it works for SOCKS5, HTTP CONNECT, or any
other TCP protocol.

    tcpmeter.py 127.0.0.1:1080=proxy.example.com:1080 \
                127.0.0.1:8080=proxy.example.com:3128

Counted bytes are TCP payload: exactly what the upstream proxy receives from
you (tx) and sends back to you (rx), including the SOCKS/CONNECT handshake and
any TLS inside the tunnel. TCP/IP headers and retransmits are not included.

Totals persist in a JSON state file across restarts. Send SIGUSR1 to print a
summary; SIGINT/SIGTERM saves and exits.
"""
import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import time

BUF_SIZE = 64 * 1024


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def parse_addr(s):
    host, sep, port = s.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {s!r}")
    return host.strip("[]") or "0.0.0.0", int(port)


def parse_route(s):
    listen, sep, upstream = s.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected LISTEN=UPSTREAM, got {s!r}")
    return s, parse_addr(listen), parse_addr(upstream)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class Meter:
    """Byte counters per route: lifetime (persisted) and since this start."""

    def __init__(self, state_path, reset=False):
        self.state_path = state_path
        self.total = {}  # route -> {"tx", "rx", "conns"}, persisted
        self.started = time.time()
        self.session_tx = self.session_rx = 0
        self.active = 0
        self.dirty = False
        self.counting_since = time.strftime("%Y-%m-%d %H:%M:%S")
        if state_path and os.path.exists(state_path) and not reset:
            with open(state_path) as f:
                state = json.load(f)
            self.total = state.get("routes", {})
            self.counting_since = state.get("counting_since", self.counting_since)

    def route(self, name):
        return self.total.setdefault(name, {"tx": 0, "rx": 0, "conns": 0})

    def add(self, name, direction, n):
        self.route(name)[direction] += n
        if direction == "tx":
            self.session_tx += n
        else:
            self.session_rx += n
        self.dirty = True

    def totals(self):
        tx = sum(r["tx"] for r in self.total.values())
        rx = sum(r["rx"] for r in self.total.values())
        return tx, rx

    def save(self):
        if not self.state_path or not self.dirty:
            return
        state = {"counting_since": self.counting_since, "routes": self.total}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self.state_path)
        self.dirty = False

    def summary(self):
        tx, rx = self.totals()
        lines = [f"since {self.counting_since}: up {fmt_bytes(tx)}  down {fmt_bytes(rx)}"
                 f"  total {fmt_bytes(tx + rx)}  ({tx + rx} bytes)"]
        for name, r in self.total.items():
            lines.append(f"  {name}: up {fmt_bytes(r['tx'])}  down {fmt_bytes(r['rx'])}"
                         f"  conns {r['conns']}")
        return "\n".join(lines)


async def pump(reader, writer, meter, route, direction, conn):
    """Copy reader -> writer until EOF, counting bytes. Returns True on clean EOF.

    Counting happens on the upstream side of the relay: tx once written to the
    proxy, rx as soon as it is read from the proxy (even if the client is gone),
    since that is what the proxy provider sees and bills.
    """
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            if direction == "rx":
                meter.add(route, direction, len(data))
                conn[direction] += len(data)
            writer.write(data)
            await writer.drain()
            if direction == "tx":
                meter.add(route, direction, len(data))
                conn[direction] += len(data)
        if writer.can_write_eof():
            writer.write_eof()  # propagate half-close
        return True
    except (ConnectionError, OSError):
        return False


def set_nodelay(writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


def make_handler(route, upstream, meter, args):
    counter = iter(range(1, sys.maxsize))

    async def handle(creader, cwriter):
        conn_id = next(counter)
        peer = cwriter.get_extra_info("peername")
        try:
            ureader, uwriter = await asyncio.wait_for(
                asyncio.open_connection(*upstream), args.connect_timeout)
        except (OSError, asyncio.TimeoutError) as e:
            log(f"[{route}] #{conn_id} upstream connect failed: {e!r}")
            cwriter.close()
            return

        set_nodelay(cwriter)
        set_nodelay(uwriter)
        meter.route(route)["conns"] += 1
        meter.active += 1
        conn = {"tx": 0, "rx": 0}
        start = time.monotonic()

        up = asyncio.create_task(pump(creader, uwriter, meter, route, "tx", conn))
        down = asyncio.create_task(pump(ureader, cwriter, meter, route, "rx", conn))
        pending = {up, down}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(not t.result() for t in done):
                # A reset/error on one side: tear down the whole connection.
                for t in pending:
                    t.cancel()
                break

        for w in (cwriter, uwriter):
            w.close()
        meter.active -= 1
        if args.log_conns:
            log(f"[{route}] #{conn_id} {peer[0]}:{peer[1]} closed after "
                f"{time.monotonic() - start:.1f}s  up {fmt_bytes(conn['tx'])}  "
                f"down {fmt_bytes(conn['rx'])}")

    return handle


async def reporter(meter, args):
    last_tx, last_rx = meter.session_tx, meter.session_rx
    last_t = time.monotonic()
    while True:
        await asyncio.sleep(args.interval)
        now = time.monotonic()
        dtx, drx = meter.session_tx - last_tx, meter.session_rx - last_rx
        dt = now - last_t
        last_tx, last_rx, last_t = meter.session_tx, meter.session_rx, now
        meter.save()
        if dtx or drx or meter.active:
            tx, rx = meter.totals()
            log(f"active {meter.active}  rate up {fmt_bytes(dtx / dt)}/s down {fmt_bytes(drx / dt)}/s"
                f"  | session up {fmt_bytes(meter.session_tx)} down {fmt_bytes(meter.session_rx)}"
                f"  | total {fmt_bytes(tx + rx)}")


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("routes", nargs="+", type=parse_route, metavar="LISTEN=UPSTREAM",
                   help="e.g. 127.0.0.1:1080=proxy.example.com:1080")
    p.add_argument("-s", "--state", default=os.path.expanduser("~/.tcpmeter.json"),
                   help="file holding persistent totals (default: %(default)s; '' to disable)")
    p.add_argument("--reset", action="store_true", help="start counting from zero")
    p.add_argument("-i", "--interval", type=float, default=10,
                   help="seconds between status lines / state saves (default: %(default)s)")
    p.add_argument("-c", "--log-conns", action="store_true", help="log each connection on close")
    p.add_argument("--connect-timeout", type=float, default=15)
    args = p.parse_args()

    meter = Meter(args.state, reset=args.reset)
    if args.reset:
        meter.dirty = True

    servers = []
    for name, listen, upstream in args.routes:
        srv = await asyncio.start_server(make_handler(name, upstream, meter, args), *listen)
        servers.append(srv)
        log(f"relaying {listen[0]}:{listen[1]} -> {upstream[0]}:{upstream[1]}")
    log(meter.summary())

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGUSR1, lambda: log(meter.summary()))

    rep = asyncio.create_task(reporter(meter, args))
    await stop.wait()
    rep.cancel()
    for srv in servers:
        srv.close()
    meter.save()
    log(meter.summary())


if __name__ == "__main__":
    asyncio.run(main())
