# BrainSync server on a Jetson Orin Nano

The server (`python -m brainsync.server`) reads the headset, runs the decoder and asks
Claude Haiku about screenshots in point mode. The Jetson sits next to the headset, and
clients reach it through a Cloudflare quick tunnel ([cloudflare/README.md](cloudflare/README.md)):
the Jetson only connects outward, so this works on Wi-Fi with client isolation too, and it
needs no Cloudflare account. Nothing here has been run on a Jetson yet: no Jetson was
attached while it was written. Treat it as the plan, and fix it where the board disagrees.

The laptop that shows the flicker can be a Mac or a Windows PC: it only runs a client (the
reference page in a browser, the Python client or BrainPoint). BrainFlow and the headset are
only on the Jetson, so the macOS-only vendored BrainFlow no longer limits the laptop.

## Quick start

1. **Flash the Jetson** with JetPack 6 (Ubuntu 22.04) or 7 (Ubuntu 24.04), finish the first-boot
   setup (user name, password, network), and connect it to the network the laptop is on.
2. **SSH from the laptop** (below), so setup and updates can run from the laptop, by you or by
   a Claude Code session.
3. **Plug in the headset** and check that `/dev/ttyUSB0` (V1) or `/dev/ttyACM0` (V2) appears.
   With `--board cerelog` the server exits while there is no headset, and every restart gives
   a new link; `--board fake` keeps one link without it.
4. **Install**, on the Jetson, as your login user through sudo (the server runs as you):

   ```bash
   sudo git clone https://github.com/1thing2say/BrainSync /opt/brainsync
   sudo /opt/brainsync/deploy/jetson/install.sh                 # builds BrainFlow, --board cerelog
   sudo /opt/brainsync/deploy/jetson/install.sh --board fake    # quicker first try, no headset
   ```

   `install.sh` does steps 1-5 below and the BrainFlow build ([brainflow/INSTALL.md](brainflow/INSTALL.md)
   (b)-(c)) in one go: it ends by installing and starting `brainsync-tunnel.service`, which
   runs the server behind the tunnel, waits for the link and checks that it stays up. Run
   it again after `sudo git -C /opt/brainsync pull` to update (without `--board` it keeps the
   board it has), or with the other `--board` to switch. The BrainFlow build takes several
   minutes the first time; later runs reuse the wheel in `~/.cache/brainsync-brainflow`.
   Add the API key (step 3 below) by hand. An old
   `brainsync-server.service` from an earlier version is stopped and removed; the token now
   lives in `~/.config/brainsync/token`.
5. **Open the link**: `cat ~/brainsync-link.txt` on the Jetson (or the serial console, see
   [cloudflare/README.md](cloudflare/README.md)). It holds the token: do not post it. The log:
   `journalctl -u brainsync-tunnel -f`.

## SSH from the laptop

On the **Jetson** (keyboard and screen, or the USB serial console):

```bash
sudo systemctl enable --now ssh   # on by default in JetPack
hostname -I                       # its address, e.g. 192.168.1.42
```

On the **laptop**: a Mac's Terminal, or WSL on Windows (Windows' own OpenSSH in PowerShell works
too, with the same commands):

```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id <user>@192.168.1.42      # asks the Jetson password once
cat >> ~/.ssh/config <<'EOF'
Host jetson
    HostName 192.168.1.42
    User <user>
    ServerAliveInterval 30
EOF
ssh -o BatchMode=yes jetson 'cat /proc/device-tree/model; echo'   # no password prompt
```

- WSL often cannot resolve `.local` names (`<hostname>.local`); use the IP address.
- Venue and campus Wi-Fi often stops devices from reaching each other (client isolation).
  Then use an Ethernet cable from the laptop, a phone hotspot both join, or the USB serial
  console. The clients themselves (the page, BrainPoint) do not need this: they go through
  the Cloudflare tunnel (step 5 under Steps).
- `install.sh` needs root. To let a Claude Code session run it without typing the password,
  allow passwordless sudo for the setup and remove it afterwards:

  ```bash
  echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-setup && sudo chmod 0440 /etc/sudoers.d/90-setup
  sudo rm /etc/sudoers.d/90-setup     # when setup is done
  ```

  Or run `sudo .../install.sh` yourself in an `ssh jetson` terminal and leave sudo as it is.

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
- **Install** the wheel into the venv before the editable install (step 2). The user the
  server runs as (your login user) must be in `dialout`, the group that owns `/dev/ttyUSB*`
  and `/dev/ttyACM*`; without it `prepare_session` fails with UNABLE_TO_OPEN_PORT_ERROR. The
  service gets the group anyway; for your own shells: `sudo usermod -aG dialout "$USER"`,
  then log in again.

Until the wheel is installed, use `install.sh --board fake` (or `BOARD=fake` in
`/etc/brainsync/tunnel.env`): everything else, including point mode, can be tried.
`smoke_test.sh` runs this setup in containers behind a TLS front end (see the end of this
file).

## Steps

1. **Python 3.11 or newer.** JetPack 6 ships Ubuntu 22.04 with Python
   3.10, which is too old. Install 3.12 from deadsnakes, as the smoke test and
   [brainflow/INSTALL.md](brainflow/INSTALL.md) do:

   ```bash
   sudo add-apt-repository -y ppa:deadsnakes/ppa
   sudo apt-get install -y python3.12 python3.12-venv
   ```

   The venv's `bin/python` is a link to the interpreter it was made with. The service runs
   as your login user and can read (and write) your home, so any interpreter you can read
   works, including one from `uv python install` in `~/.local/share/uv`.
2. **Code and venv** in `/opt/brainsync`, owned by root and readable by everyone (your login
   user runs the server from it):

   ```bash
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
4. **The serial port.** Remove `brltty` (it grabs the Cerelog V1's CH340 on Ubuntu desktop
   images) and stop ModemManager (it probes new `/dev/ttyACM*` ports, the Cerelog V2, with
   AT commands):

   ```bash
   sudo apt-get remove -y brltty
   sudo systemctl disable --now ModemManager
   ```
5. **The tunnel**, which runs the server: [cloudflare/README.md](cloudflare/README.md).

   ```bash
   sudo deploy/jetson/cloudflare/install.sh --board cerelog   # or --board fake
   journalctl -u brainsync-tunnel -f     # "tunnel up: https://<name>.trycloudflare.com ..."
   cat ~/brainsync-link.txt              # https://<name>.trycloudflare.com/#token=...
   ```

   It installs `cloudflared`, adds you to `dialout` and `tty`, and starts
   `brainsync-tunnel.service` as you, now and at every boot. The service starts a quick
   tunnel, then the server on `127.0.0.1:8765` with exactly that address as its `--origin`,
   and writes the link to `~/brainsync-link.txt` and the serial console. On the first start
   the server writes a new token to `~/.config/brainsync/token` (mode 0600); to change it,
   delete the file and restart. Every restart gives a new address. Clients then use:
   - the reference page: the link, `https://<name>.trycloudflare.com/#token=<token>`;
   - the BrainPoint extension: server `wss://<name>.trycloudflare.com/ws` and the token;
   - the Python client: `--url wss://<name>.trycloudflare.com/ws`.

## Notes

- The server listens on `127.0.0.1` only: only `cloudflared` reaches the port. The address
  is public, so the token is the gate: the tunnel script refuses `--no-auth`, and the server
  refuses it on any other host.
- The display that shows the flicker belongs to the client, so its refresh rate is checked
  per client; PROTOCOL-v3 lists the refresh rates the server accepts. The Jetson needs no
  display.
- For a session log (no token, no screenshots), set `SERVER_ARGS=--log /home/<you>/session.jsonl`
  in `/etc/brainsync/tunnel.env`; the service can write only in your home. Add
  `--log-scans /home/<you>/scans` only when you want screenshots kept.
- Stop with `sudo systemctl stop brainsync-tunnel`: clients get `phase stop` and the
  flicker ends at once.

## Smoke test in containers

`deploy/jetson/smoke_test.sh` tries this setup without a Jetson. On an Apple Silicon Mac
(or any Docker host that runs linux/arm64) it builds an Ubuntu 22.04 arm64 image with
Python 3.12, the BrainFlow wheel and `pip install -e .` as in step 2, runs the server with
`--board fake --host 0.0.0.0 --token-file /tmp/token` and `--origin` for the site, as the
tunnel script does, on a private Docker network, and puts `caddy:2` with `tls internal` for
`jetson-smoke.trycloudflare.com` in front of it, the stand-in for the Cloudflare edge. A
client container then checks `/health` and the reference page over HTTPS, a controller with
the page's Origin and the token that gets a welcome and, after gaze, a selection over wss, a
wrong token (close 4001) and a foreign Origin (HTTP 403).

```bash
WHEEL=deploy/jetson/brainflow/out/brainflow-0.0.1-py3-none-linux_aarch64.whl deploy/jetson/smoke_test.sh
```

Without `WHEEL` it takes the wheel from `brainflow/out/`, or builds it with the kit first.
Each check prints PASS or FAIL, and the exit code is 1 when any failed. Only Caddy's port 443
is published, on 127.0.0.1. The token stays inside the containers.
