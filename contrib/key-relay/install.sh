#!/bin/sh
# Install, update or remove the shipboard key relay. Run as root.
#   sudo ./install.sh                         install/update, trigger key rightalt
#   sudo ./install.sh --keys rightalt,pause   same, with your own keys (shipboard names)
#   sudo ./install.sh --uninstall
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
LIB=/usr/local/lib/shipboard-key-relay
UNIT=/etc/systemd/system/shipboard-key-relay.service
DROPIN=/etc/systemd/system/shipboard-key-relay.service.d
RULE=/etc/udev/rules.d/70-shipboard-key-relay.rules

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }

keys=""
case "${1:-}" in
  --uninstall)
    systemctl disable --now shipboard-key-relay.service 2>/dev/null || true
    rm -rf "$LIB" "$UNIT" "$DROPIN" "$RULE"
    systemctl daemon-reload
    udevadm control --reload-rules
    echo "shipboard key relay removed"
    exit 0 ;;
  --keys)
    keys="${2:?--keys needs a value, e.g. rightalt,pause}"
    case "$keys" in *[!a-zA-Z0-9_,]*) echo "keys: letters, digits, _ and commas only" >&2; exit 2 ;; esac
    /usr/bin/python3 -I "$HERE/shipboard-key-relay.py" --keys "$keys" --check ;;
  "") ;;
  *) echo "usage: $0 [--keys rightalt,pause] | --uninstall" >&2; exit 2 ;;
esac

install -d -m 0755 "$LIB"
install -m 0644 "$HERE/shipboard-key-relay.py" "$LIB/shipboard-key-relay.py"
install -m 0644 "$HERE/shipboard-key-relay.service" "$UNIT"
install -m 0644 "$HERE/70-shipboard-key-relay.rules" "$RULE"
if [ -n "$keys" ]; then
  install -d -m 0755 "$DROPIN"
  printf '[Service]\nEnvironment=KEYS=%s\n' "$keys" > "$DROPIN/keys.conf"
  chmod 0644 "$DROPIN/keys.conf"
fi
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable shipboard-key-relay.service
systemctl restart shipboard-key-relay.service
sleep 2
systemctl --no-pager --lines=8 status shipboard-key-relay.service || true
