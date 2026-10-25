#!/usr/bin/env bash
# Install the boot-time Cloudflare quick tunnel for BrainSync on a Jetson (see README.md here).
#
#   sudo deploy/jetson/cloudflare/install.sh [--user NAME] [--dir DIR]
#   sudo deploy/jetson/cloudflare/install.sh --uninstall [--user NAME]
#   deploy/jetson/cloudflare/install.sh --print-unit --user NAME [--home DIR] [--dir DIR]
#
# Installing: cloudflared (the official arm64 .deb) and qrencode when missing; the user joins
# dialout and tty; /etc/brainsync/tunnel.env is written when absent; the unit is rendered for
# the user and installed; brainsync-server.service is disabled when it is enabled (both use
# port 8765); brainsync-tunnel.service is enabled and (re)started. Running it again is safe.
#
#   --user NAME   whose home gets ~/brainsync-link.txt and who runs the service (default: the
#                 user who ran sudo, $SUDO_USER)
#   --dir DIR     the BrainSync checkout with its .venv (default: the checkout holding this
#                 script, normally /opt/brainsync)
#   --home DIR    with --print-unit only: the home to use instead of NAME's
#   --uninstall   stop, disable and remove the unit and the link file; cloudflared, qrencode,
#                 the groups and /etc/brainsync/tunnel.env stay
#   --print-unit  print the rendered unit and exit (no root needed; used by the tests)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=brainsync-tunnel.service
UNIT_DST="/etc/systemd/system/$UNIT"
TUNNEL_ENV=/etc/brainsync/tunnel.env
DEB_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb"

user=""
dir=""
home=""
action=install

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# apt_install PACKAGE...: apt-get install, after an apt-get update when the first try fails
apt_install() { apt-get install -y "$@" || { apt-get update && apt-get install -y "$@"; }; }
usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
    case "$1" in
        --user) (($# >= 2)) || die "--user needs a name"; user="$2"; shift 2 ;;
        --dir) (($# >= 2)) || die "--dir needs a path"; dir="$2"; shift 2 ;;
        --home) (($# >= 2)) || die "--home needs a path"; home="$2"; shift 2 ;;
        --uninstall) action=uninstall; shift ;;
        --print-unit) action=print; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

dir="${dir:-$(cd "$HERE/../../.." && pwd)}"
user="${user:-${SUDO_USER:-}}"
[[ -n "$user" ]] || die "pass --user NAME, or run with sudo from the login user"
[[ "$user" =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "not a user name systemd accepts: $user"
if [[ -z "$home" ]]; then
    if command -v getent >/dev/null 2>&1; then
        home="$(getent passwd "$user" | cut -d: -f6)" || true
    fi
fi
[[ "$home" == /* && -n "${home#/}" ]] || die "no home directory for $user (pass --home)"
for path in "$dir" "$home"; do
    # the unit takes these unquoted; sed below uses | as its separator
    [[ "$path" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "the path '$path' has characters the unit cannot carry"
done

render_unit() {
    sed -e "s|@USER@|$user|g" -e "s|@HOME@|$home|g" -e "s|@BRAINSYNC_DIR@|$dir|g" "$HERE/$UNIT"
}

if [[ "$action" == print ]]; then
    render_unit
    exit 0
fi

((EUID == 0)) || die "run with sudo"
[[ "$user" != root ]] || die "the tunnel should not run as root; pass --user NAME"
id "$user" >/dev/null 2>&1 || die "no user $user"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found: this needs systemd"
LINK="$home/brainsync-link.txt"

if [[ "$action" == uninstall ]]; then
    say "stopping and removing $UNIT"
    systemctl disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DST"
    systemctl daemon-reload
    rm -f "$LINK"
    say "removed $UNIT_DST and $LINK"
    say "kept: cloudflared, qrencode, the dialout/tty groups of $user, $TUNNEL_ENV"
    say "the Tailscale setup, if you want it back: sudo systemctl enable --now brainsync-server"
    exit 0
fi

SCRIPT="$dir/deploy/jetson/cloudflare/brainsync-tunnel.sh"
[[ -f "$SCRIPT" ]] || die "$SCRIPT not found; pass --dir with the BrainSync checkout"
chmod 0755 "$SCRIPT"
[[ -x "$dir/.venv/bin/python" ]] \
    || say "warning: no $dir/.venv/bin/python yet; make the venv (deploy/jetson/README.md step 2), or the tunnel cannot start"

# 1. cloudflared and qrencode
if command -v cloudflared >/dev/null 2>&1; then
    say "cloudflared already installed: $(cloudflared --version 2>&1 | head -n 1)"
else
    command -v dpkg >/dev/null 2>&1 || die "no dpkg: install cloudflared by hand (https://github.com/cloudflare/cloudflared/releases)"
    arch="$(dpkg --print-architecture)"
    [[ "$arch" == arm64 ]] || die "this machine is $arch; the package here is for arm64 (a Jetson)"
    command -v curl >/dev/null 2>&1 || { say "installing curl"; apt_install curl; }
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    deb="$tmp/cloudflared-linux-arm64.deb"
    say "downloading $DEB_URL"
    curl -fL --retry 3 --connect-timeout 20 -o "$deb" "$DEB_URL"
    pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null)" || die "the download is not a .deb package"
    pkg_arch="$(dpkg-deb -f "$deb" Architecture)"
    [[ "$pkg" == cloudflared && "$pkg_arch" == arm64 ]] \
        || die "expected the arm64 cloudflared package, got $pkg ($pkg_arch)"
    say "installing $pkg $(dpkg-deb -f "$deb" Version) ($pkg_arch)"
    apt_install "$deb"
    cloudflared --version
fi
if command -v qrencode >/dev/null 2>&1; then
    say "qrencode already installed"
else
    say "installing qrencode (the QR code on the serial console)"
    apt_install qrencode || say "warning: qrencode did not install; the link still goes out, without a QR code"
fi

# 2. groups: the unit gets them through SupplementaryGroups anyway; this is for your own shells
for group in dialout tty; do
    if id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
        say "$user is in $group"
    else
        usermod -aG "$group" "$user"
        say "added $user to $group (log in again for your own shells)"
    fi
done

# 3. settings
install -d -m 0755 /etc/brainsync
if [[ -e "$TUNNEL_ENV" ]]; then
    say "kept $TUNNEL_ENV"
else
    cat >"$TUNNEL_ENV" <<'EOF'
# Settings for brainsync-tunnel.service (deploy/jetson/cloudflare/README.md): KEY=VALUE lines,
# comments on their own lines only. After a change: sudo systemctl restart brainsync-tunnel
#
# fake: the simulated board, to try everything without the headset
#BOARD=cerelog
# more server arguments, e.g. --no-detect, or --log /home/<you>/session.jsonl
#SERVER_ARGS=
# http2 when the venue blocks UDP 7844 (QUIC); empty: cloudflared's default
#CLOUDFLARED_PROTOCOL=
#SERIAL_DEVICES=/dev/ttyGS0 /dev/ttyTCU0
# 0: the link in ~/brainsync-link.txt and on serial leaves out #token= (add it by hand)
#LINK_WITH_TOKEN=1
#ANNOUNCE_EVERY=60
#URL_TIMEOUT=90
#PORT=8765
EOF
    chmod 0644 "$TUNNEL_ENV"
    say "wrote $TUNNEL_ENV (all settings commented out: the defaults)"
fi
for cfg in "$home/.cloudflared/config.yml" "$home/.cloudflared/config.yaml" \
    /etc/cloudflared/config.yml /etc/cloudflared/config.yaml; do
    [[ ! -e "$cfg" ]] || say "warning: $cfg exists; quick tunnels refuse to start while it does (rename it)"
done

# 4. the unit
rendered="$(render_unit)"
printf '%s\n' "$rendered" >"$UNIT_DST.tmp"
chmod 0644 "$UNIT_DST.tmp"
mv -f "$UNIT_DST.tmp" "$UNIT_DST"
say "installed $UNIT_DST (User=$user, $dir)"
if systemctl is-enabled --quiet brainsync-server.service 2>/dev/null; then
    systemctl disable --now brainsync-server.service
    say "disabled brainsync-server.service: it uses the same port 8765 (back to Tailscale: sudo $0 --uninstall, then sudo systemctl enable --now brainsync-server)"
fi
systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT"
say "started $UNIT; it starts at every boot from now on"

cat <<EOF

The link (a new one after every restart; it holds the token, do not post it):
  cat $LINK
  journalctl -u brainsync-tunnel -f     # "tunnel up: https://<name>.trycloudflare.com"
  serial: from a Mac, ls /dev/cu.usbmodem*, then screen /dev/cu.usbmodemXXXX 115200
          (the USB-C device-mode port, /dev/ttyGS0; or the debug UART, /dev/ttyTCU0)
The first link takes up to a minute or two after boot (Wi-Fi, then Cloudflare).
EOF
