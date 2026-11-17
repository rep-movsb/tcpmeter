# tcpmeter

[![test](https://github.com/rep-movsb/tcpmeter/actions/workflows/test.yml/badge.svg)](https://github.com/rep-movsb/tcpmeter/actions/workflows/test.yml)

A small byte-counting TCP relay. It shows how much data you actually send through a
**metered SOCKS5 or HTTP proxy**.

You point your applications at tcpmeter instead of at the proxy. tcpmeter forwards the
raw TCP stream to the real proxy without changing it, and counts every byte in each
direction. Because it never parses the stream, it works with any TCP-based proxy
protocol (SOCKS4/5, HTTP `CONNECT`, plain HTTP proxying), and authentication passes
through untouched.

```
app ──► 127.0.0.1:1080 (tcpmeter, counts bytes) ──► proxy.example.com:1080 ──► internet
```

- Single file, Python standard library only, no root required
- Exact per-direction byte counts, persisted across restarts
- Live throughput and running totals
- Several proxies at once, with separate totals for each

## Requirements

- Linux or another Unix-like OS (uses POSIX signals)
- Python 3.10 or newer

## Quick start

```bash
git clone https://github.com/rep-movsb/tcpmeter.git
cd tcpmeter
./tcpmeter.py -c 127.0.0.1:1080=proxy.example.com:1080
```

Then change your application's proxy setting from `proxy.example.com:1080` to
`127.0.0.1:1080`. Keep the same scheme and credentials:

```bash
curl -x socks5h://user:pass@127.0.0.1:1080 https://example.com
```

To meter several proxies, pass several routes:

```bash
./tcpmeter.py 127.0.0.1:1080=socks.example.com:1080 \
              127.0.0.1:8080=http.example.com:3128
```

## Output

```
2026-11-17 21:40:02 relaying 127.0.0.1:1080 -> proxy.example.com:1080
2026-11-17 21:40:02 since 2026-11-01 00:00:03: up 41.20 MiB  down 1.73 GiB  total 1.77 GiB  (1900389120 bytes)
2026-11-17 21:40:31 [127.0.0.1:1080=proxy.example.com:1080] #1 127.0.0.1:51544 closed after 2.3s  up 1.21 KiB  down 512.40 KiB
2026-11-17 21:40:32 active 3  rate up 2.10 KiB/s down 1.18 MiB/s  | session up 4.73 KiB down 3.52 MiB  | total 1.78 GiB
```

- A status line is printed every `--interval` seconds when there is traffic.
- `-c` / `--log-conns` adds one line per connection when it closes.
- `kill -USR1 <pid>` prints a full summary with totals for each route.
- `SIGINT` / `SIGTERM` save the totals and exit cleanly.

## Options

| Option | Default | Description |
|---|---|---|
| `LISTEN=UPSTREAM` | (required) | One or more routes, each `HOST:PORT=HOST:PORT`. IPv6 addresses go in brackets: `[::1]:1080=...` |
| `-s`, `--state FILE` | `~/.tcpmeter.json` | Where lifetime totals are stored. Pass `''` to disable. |
| `--reset` | | Start counting from zero, for example at the start of a billing period. |
| `-i`, `--interval SEC` | `10` | Seconds between status lines and state saves. |
| `-c`, `--log-conns` | off | Log bytes per connection on close. |
| `--connect-timeout SEC` | `15` | Timeout for connecting to the upstream proxy. |

The state file is plain JSON. You can read it at any time:

```json
{
  "counting_since": "2026-11-01 00:00:03",
  "routes": {
    "127.0.0.1:1080=proxy.example.com:1080": { "tx": 43201544, "rx": 1857187576, "conns": 1893 }
  }
}
```

Totals are stored per route string. If you change a route's listen or upstream address,
it starts a new entry.

## What exactly is counted

tcpmeter counts **TCP payload bytes on the proxy side of the relay**:

- **tx (up):** bytes successfully written to the proxy.
- **rx (down):** bytes read from the proxy. These are counted as soon as they arrive,
  even if your application has already disconnected, because the provider sent them
  and will bill for them.

This includes the SOCKS or `CONNECT` handshake, proxy authentication, and everything
inside the tunnel (for HTTPS sites, the TLS handshake and record overhead). It does
**not** include TCP/IP headers, ACKs or retransmissions. Most proxy providers meter
payload, so tcpmeter's total should match your provider's dashboard closely. Wire-level
usage (for example from `nftables` counters) will be a few percent higher.

## Running as a service

A systemd user unit is included in [`contrib/tcpmeter.service`](contrib/tcpmeter.service):

```bash
mkdir -p ~/.config/systemd/user
cp contrib/tcpmeter.service ~/.config/systemd/user/
# edit ExecStart: path to tcpmeter.py and your proxy address(es)
systemctl --user daemon-reload
systemctl --user enable --now tcpmeter
journalctl --user -u tcpmeter -f
```

To keep it running while you are logged out, run `loginctl enable-linger $USER`.

## Caveats

- **Listen on localhost only.** If you listen on `0.0.0.0`, anyone who can reach the
  port can use your paid proxy (and its credentials, if the proxy authenticates by
  source IP).
- **Proxies you connect to over TLS** (`https://` proxy URLs): the client checks the
  proxy's certificate against `127.0.0.1`, so the check fails. Plain SOCKS5 and HTTP
  proxies work fine, including HTTPS websites accessed through them.
- **Per-proxy totals only.** tcpmeter doesn't know which websites the traffic went to.
- When a connection is torn down, data still in the kernel's receive buffer (at most a
  few hundred KiB) may have been sent by the proxy without being read and counted.

## Development

Tests are end-to-end: they run tcpmeter in front of a local stand-in proxy and check
byte-exact totals.

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
