#!/usr/bin/env bash
set -Eeuo pipefail

container="${WHISPER_CONTAINER:-whisper-local}"
marker="${WHISPER_IDLE_MARKER:-/tmp/whisper-local-last-use}"
idle_seconds="${WHISPER_IDLE_SECONDS:-300}"
docker="/usr/bin/docker"

state="$(${docker} inspect -f '{{.State.Status}}' "${container}" 2>/dev/null || true)"
[[ "${state}" == "running" ]] || exit 0

# A manually started container gets a five-minute grace period even before the
# first request. The adapter refreshes this file before and during each request.
if [[ ! -e "${marker}" ]]; then
    touch "${marker}"
    exit 0
fi

last_used="$(stat -c '%Y' "${marker}")"
now="$(date +%s)"
if (( now - last_used >= idle_seconds )); then
    "${docker}" stop --time 5 "${container}" >/dev/null
fi
