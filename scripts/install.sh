#!/usr/bin/env bash
# Install whisper-local systemd user units (wake proxy + idle stop).
# Optional: also install the alter-talk daemon unit.
set -Eeuo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "${unit_dir}"
install -m 0644 \
    "${repo}/systemd/whisper-wake-proxy.service" \
    "${repo}/systemd/whisper-idle-stop.service" \
    "${repo}/systemd/whisper-idle-stop.timer" \
    "${unit_dir}/"

systemctl --user daemon-reload
systemctl --user enable --now whisper-wake-proxy.service whisper-idle-stop.timer

echo "whisper-local units installed and enabled."
echo
echo "Optional — install the alter-talk input daemon as a user service:"
echo "  install -m 0644 ${repo}/alter-talk/alter-talk.service ${unit_dir}/"
echo "  systemctl --user daemon-reload && systemctl --user enable --now alter-talk"
echo "(or run it from your compositor: spawn-at-startup \"alter-talk\" / exec-once)"
