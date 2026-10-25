# BrainSync behind a Cloudflare quick tunnel

At a venue whose Wi-Fi has client isolation, a phone cannot reach the Jetson at its local
address. This setup works anyway, because the Jetson only connects outward:

- At boot, `cloudflared` opens a quick tunnel. That needs no Cloudflare account, and it gives
  a random `https://<name>.trycloudflare.com` address.
- The BrainSync server starts behind the tunnel and allows exactly that address as its
  Origin.
- Any browser on any network (venue Wi-Fi, cellular) opens the link. It still talks
  WebSocket, as `wss://<name>.trycloudflare.com/ws`.

The link is written to `~/brainsync-link.txt` (one line, mode 0600) and printed on the
serial console every 60 s, with a QR code. If the tunnel or the server stops, both start
again with a **new** address, and the file and the serial console get the new link.

Nothing here has been run on a real Jetson yet. The tests (`tests/test_jetson_tunnel.py`)
run the script with a fake `cloudflared`, on macOS and in an Ubuntu 22.04 arm64 container.

## Install

`sudo deploy/jetson/install.sh` ([../README.md](../README.md), Quick start) does it all:
Python, the venv, the BrainFlow wheel, then this script as its last step. To run only this
part, on the Jetson, after steps 1 and 2 of [../README.md](../README.md):

```bash
cd /opt/brainsync
sudo deploy/jetson/cloudflare/install.sh --board cerelog   # as the login user, through sudo
```

It does the following, and running it again is safe:

- installs `cloudflared` (the official arm64 `.deb`, after checking that it is arm64) and
  `qrencode`;
- adds you to `dialout` (the headset's serial port) and `tty` (the serial consoles);
- writes `/etc/brainsync/tunnel.env` (`--board cerelog` or `--board fake` sets its `BOARD=`
  line and keeps your other settings) and installs and starts `brainsync-tunnel.service` as
  your user;
- stops and removes an old `brainsync-server.service` from an earlier version, which used
  the same port 8765.

`sudo deploy/jetson/cloudflare/install.sh --uninstall` removes the tunnel service again.

## Reading the link

- **File:** `cat ~/brainsync-link.txt`. It appears a minute or two after boot: first the
  Wi-Fi, then Cloudflare.
- **Serial, from a Mac with a USB cable:** `ls /dev/cu.usbmodem*`, then
  `screen /dev/cu.usbmodemXXXX 115200`. Press Ctrl-A then K to quit.
  - The USB-C port gives `/dev/ttyGS0` on the Jetson when JetPack's USB device mode is on
    (the default on the developer kit).
  - The debug UART (`/dev/ttyTCU0`) needs a 3.3 V USB-TTL adapter on the button header.
  - The link is repeated every 60 s, so plugging in late is fine. Scan the QR code with the
    phone.
- **Journal:** `journalctl -u brainsync-tunnel -f` shows `tunnel up: https://<name>.trycloudflare.com`.
  The journal never shows the token.

For the BrainPoint Chrome extension, set the server to `wss://<name>.trycloudflare.com/ws`
and the token to the part of the link after `#token=`.

## Settings

Settings go in `/etc/brainsync/tunnel.env`, one `KEY=VALUE` per line. After a change, run
`sudo systemctl restart brainsync-tunnel`. The full list is at the top of
`brainsync-tunnel.sh`.

| setting | default | for |
| --- | --- | --- |
| `BOARD` | `cerelog` | `fake` tries everything without the headset; `install.sh --board` sets it |
| `SERVER_ARGS` | | more server flags, e.g. `--no-detect` |
| `CLOUDFLARED_PROTOCOL` | cloudflared's (QUIC, UDP 7844) | `http2` when the venue blocks UDP |
| `SERIAL_DEVICES` | `/dev/ttyGS0 /dev/ttyTCU0` | which consoles get the link |
| `ANNOUNCE_EVERY` | `60` | seconds between serial announcements (`0`: once) |
| `LINK_WITH_TOKEN` | `1` | `0` leaves `#token=` out of the file and serial |

For point mode, put `ANTHROPIC_API_KEY=...` in `/etc/brainsync/env` (root-only).

## Security

- **Public address:** a trycloudflare address can be reached from the whole internet, and
  the token is the only gate. Don't post the link or show it on a projector. To start fresh,
  delete `~/.config/brainsync/token` and restart the service, which makes a new token.
- **`--no-auth` is refused:** `cloudflared` connects from 127.0.0.1, so the server's
  loopback-only rule for `--no-auth` does not protect the tunnel. The script refuses
  `--no-auth`, and any abbreviation of it, in `SERVER_ARGS`.
- **Where the token appears:** only in `~/brainsync-link.txt` (mode 0600) and on the serial
  console, which needs a cable at the Jetson. With `LINK_WITH_TOKEN=0`, neither has the
  token; you add `#token=` yourself from `~/.config/brainsync/token`.

## When it does not come up

| what you see | why, and what to do |
| --- | --- |
| no link after a few minutes; the journal says "no trycloudflare address" | no internet yet. A captive-portal login page blocks it: log in once with a monitor attached, or use a phone hotspot |
| the journal shows QUIC errors | set `CLOUDFLARED_PROTOCOL=http2` |
| "refuses a quick tunnel" | `~/.cloudflared/config.yml` exists (it is meant for named tunnels); rename it |
| "something already answers on 127.0.0.1:8765" | an old `brainsync-server.service` or another server is running: `sudo systemctl disable --now brainsync-server`, or run `sudo deploy/jetson/install.sh` again, which removes it |
| nothing on serial | check that `/dev/ttyGS0` exists while the USB-C cable is in, and that you are in group `tty` (`id -nG`) |
| the link changes every half minute; the journal says "server exited (status 1)" after an error about the board | `BOARD=cerelog` and no headset: the server opens the board after the link is out, exits when that fails, and each restart gets a new address. Plug the headset in, or set `BOARD=fake` |
| an old link shows a Cloudflare error page | the tunnel restarted, so the address changed; open the new link from the file or serial |
