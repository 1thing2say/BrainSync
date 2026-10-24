# BrainSync server on a Jetson Orin Nano

The server (`python -m brainsync.server`) reads the headset, runs the decoder and asks
Claude Haiku about screenshots in point mode. The Jetson sits next to the headset, and
clients reach it through Tailscale. Nothing here has been run on a Jetson yet: no Jetson
was attached while it was written. Treat it as the plan, and fix it where the board
disagrees.

## First: BrainFlow for Linux aarch64

`--board cerelog` needs the Cerelog fork of BrainFlow, not the PyPI package (that one has no
Cerelog board, and its id 65 is another device). The fork is vendored only as macOS arm64
dylibs, so `deploy/jetson/brainflow/` builds it for Linux aarch64 and packages it as
`brainflow-0.0.1-py3-none-linux_aarch64.whl`. [brainflow/INSTALL.md](brainflow/INSTALL.md)
has the details; in short:

- **Build** with Docker on an Apple Silicon Mac (arm64 natively), or natively on the Jetson
  with `build_brainflow.sh`. The script clones the fork at a pinned commit (`932b3944`),
  patches a copy, builds it with CMake, makes the wheel, and checks it in a fresh venv: every
  `.so` loads, the Cerelog X8 runs at 250 Hz, and a fake Cerelog on a pseudo-terminal streams
  through the real driver. That was verified in a clean Ubuntu 22.04 arm64 container, not yet
  on a Jetson or with the headset.
- **What the patch is.** The vendored macOS build has an uncommitted change in `connect_v1`
  of the Cerelog driver: the re-opened serial port is put in raw mode with a 1 s read timeout.
  `cerelog-serial-timeout.patch` reconstructs it (the same `set_serial_port_settings(1000,
  false)` call `connect_v2` makes). Disassembling the macOS dylib showed the same call, error
  message and return code. Without it, a port that stays silent blocks the V1 check with no
  time limit (measured on a pseudo-terminal) instead of falling back to V2.
  `setuptools-pin.patch` pins `setuptools<81`, because the package imports `pkg_resources`.
- **Optional:** `cerelog-serial-timeout.reviewed.patch` also puts the *first* open (the
  9600-baud handshake) in raw mode, which the original does not do. On a tty nothing has
  configured yet, a 0x0A byte in the handshake goes out as 0x0D 0x0A. Use the plain patch to
  match the macOS build exactly.
- **Install** the wheel into the venv before the editable install (step 2). The service user
  must be in `dialout`, the group that owns `/dev/ttyUSB*` and `/dev/ttyACM*`; without it
  `prepare_session` fails with UNABLE_TO_OPEN_PORT_ERROR. For your own user:
  `sudo usermod -aG dialout "$USER"`, then log in again.

Until the wheel is installed, run the unit with `--board fake`: everything else, including
point mode, can be tried. `smoke_test.sh` runs this setup in containers behind a TLS proxy
(see the end of this file).

## Steps

1. **Python 3.11 or newer, installed system-wide.** JetPack 6 ships Ubuntu 22.04 with Python
   3.10, which is too old. Install 3.12 from deadsnakes, as the smoke test and
   [brainflow/INSTALL.md](brainflow/INSTALL.md) do:

   ```bash
   sudo add-apt-repository -y ppa:deadsnakes/ppa
   sudo apt-get install -y python3.12 python3.12-venv
   ```

   It must live outside `/home`. The venv's `bin/python` is a link to the interpreter it was
   made with, and the unit runs with `ProtectHome=yes` as the `brainsync` user, so a Python in
   your home (such as the one `uv python install` puts in `~/.local/share/uv`) cannot be
   reached and the unit fails with status 203/EXEC. With uv, install it elsewhere instead:
   `sudo UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install 3.12`, and make the venv
   with that interpreter.
2. **Code and venv** in `/opt/brainsync`, owned by a service user in `dialout` (the group that
   owns `/dev/ttyUSB*` and `/dev/ttyACM*`):

   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout brainsync
   sudo git clone <this repo> /opt/brainsync
   cd /opt/brainsync
   sudo python3.12 -m venv .venv
   sudo .venv/bin/python -m pip install "setuptools<81"
   sudo .venv/bin/python -m pip install ~/brainflow-0.0.1-py3-none-linux_aarch64.whl
   sudo .venv/bin/python -m pip install -e .
   ```

   The wheel comes from `deploy/jetson/brainflow/` (above). It goes in first so that nothing
   pulls the PyPI `brainflow` instead; add `--force-reinstall` if an older one is installed.

   The editable install matters: the server serves the reference page from `clients/web/`
   and `clients/js/` next to the package. numpy, scipy, websockets and anthropic all have
   aarch64 wheels.
3. **API key** (optional, for point mode), readable by root only. systemd reads it before
   dropping privileges:

   ```bash
   sudo install -d -m 0755 /etc/brainsync
   sudo install -m 0600 /dev/null /etc/brainsync/env
   sudoedit /etc/brainsync/env          # ANTHROPIC_API_KEY=...
   ```
4. **The unit**:

   ```bash
   sudo cp deploy/jetson/brainsync-server.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now brainsync-server
   journalctl -u brainsync-server -f     # "BrainSync server on ws://127.0.0.1:8765/ws ..."
   curl -s http://127.0.0.1:8765/health  # {"ok":true,"board":"streaming",...}
   ```

   On the first start the server writes a new token to `/var/lib/brainsync/token` (mode
   0600) and prints the page address with it once, so it is in the journal. Read it later
   with `sudo cat /var/lib/brainsync/token`. To change it, delete the file and restart.
5. **Tailscale.** Install Tailscale, log in, and let it serve HTTPS in front of the server:

   ```bash
   sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
   tailscale serve status
   ```

   The syntax of `tailscale serve` has changed across versions (older releases used
   `tailscale serve https / http://127.0.0.1:8765`), so check `tailscale serve --help` on the
   Jetson. HTTPS certificates must be enabled for the tailnet in the admin console.
   Clients then use:
   - the reference page: `https://<jetson>.<tailnet>.ts.net/#token=<token>`;
   - the BrainPoint extension: server `wss://<jetson>.<tailnet>.ts.net/ws` and the token;
   - the Python client: `--url wss://<jetson>.<tailnet>.ts.net/ws`.

   If the page gets HTTP 403, the proxy did not pass the page's own host through; add
   `--origin 'https://<jetson>\.<tailnet>\.ts\.net'` to `ExecStart`.

   No Tailscale, and a Wi-Fi with client isolation? [cloudflare/README.md](cloudflare/README.md)
   sets up a Cloudflare quick tunnel instead. It starts at boot, and it writes the link to
   `~/brainsync-link.txt` and the serial console.

## Notes

- Keep `--host 127.0.0.1`: only `tailscale serve` should reach the port. `--no-auth` is
  refused on any other host.
- The display that shows the flicker belongs to the client, so its refresh rate is checked
  per client; PROTOCOL-v3 lists the refresh rates the server accepts. The Jetson needs no
  display.
- `/var/log/brainsync/session.jsonl` holds the session log (no token, no screenshots). Add
  `--log-scans /var/lib/brainsync/scans` to `ExecStart` only when you want screenshots kept.
- Stop with `sudo systemctl stop brainsync-server`: clients get `phase stop` and the
  flicker ends at once.

## Smoke test in containers

`deploy/jetson/smoke_test.sh` tries this setup without a Jetson. On an Apple Silicon Mac
(or any Docker host that runs linux/arm64) it builds an Ubuntu 22.04 arm64 image with
Python 3.12, the BrainFlow wheel and `pip install -e .` as in step 2, runs the server with
`--board fake --host 0.0.0.0 --token-file /tmp/token` on a private Docker network, and puts
`caddy:2` with `tls internal` for `jetson.tailtest.ts.net` in front of it, the stand-in for
`tailscale serve`. A client container then checks `/health` and the reference page over
HTTPS, a controller with the page's Origin and the token that gets a welcome and, after gaze,
a selection over wss, a wrong token (close 4001) and a foreign Origin (HTTP 403). Caddy
passes the Host through, so the server accepts the `*.ts.net` Origin as its own address
without `--origin`.

```bash
WHEEL=deploy/jetson/brainflow/out/brainflow-0.0.1-py3-none-linux_aarch64.whl deploy/jetson/smoke_test.sh
```

Without `WHEEL` it takes the wheel from `brainflow/out/`, or builds it with the kit first.
Each check prints PASS or FAIL, and the exit code is 1 when any failed. Only Caddy's port 443
is published, on 127.0.0.1. The token stays inside the containers.
