#!/usr/bin/env bash
# Install whisper-local systemd user units (wake proxy + idle stop).
# Optional: also install the shipboard daemon unit.
# Repo is expected to live at ~/Coding/shipboard — the unit files
# ExecStart paths point there.
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
echo "Optional — install the shipboard input daemon as a user service:"
echo "  install -m 0644 ${repo}/systemd/shipboard.service ${unit_dir}/"
echo "  systemctl --user daemon-reload && systemctl --user enable --now shipboard"
echo "(or run it from your compositor: spawn-at-startup \"shipboard\" / exec-once)"
