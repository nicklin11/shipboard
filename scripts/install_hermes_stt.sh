#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
hermes_home="${HERMES_HOME:-${HOME}/.hermes}"
stt_dir="${hermes_home}/stt"

mkdir -p "${stt_dir}"
install -m 0755 "${repo_dir}/scripts/whisper_cpp_stt.py" \
    "${stt_dir}/whisper_cpp_stt.py"

if [[ ! -e "${stt_dir}/whisper.env" ]]; then
    install -m 0644 "${repo_dir}/scripts/whisper.env.example" \
        "${stt_dir}/whisper.env"
fi

if command -v hermes >/dev/null 2>&1; then
    hermes config set stt.providers.whisperdocker.command \
        "/usr/bin/python3 ${stt_dir}/whisper_cpp_stt.py --input {input_path} --output {output_path} --language {language}"
    hermes config set stt.providers.whisperdocker.language auto
fi

echo "Installed Hermes STT client: ${stt_dir}/whisper_cpp_stt.py"
echo "Edit ${stt_dir}/whisper.env for the local or tailnet STT URL."
