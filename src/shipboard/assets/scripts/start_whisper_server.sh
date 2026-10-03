#!/usr/bin/env bash
set -Eeuo pipefail

model_name="${WHISPER_MODEL:-large-v3-turbo}"
models_dir="${WHISPER_MODEL_DIR:-/models}"
model_path="${models_dir}/ggml-${model_name}.bin"
vad_enabled="${WHISPER_VAD:-true}"
vad_name="${WHISPER_VAD_MODEL:-silero-v6.2.0}"
vad_path="${models_dir}/ggml-${vad_name}.bin"

mkdir -p "${models_dir}"
mkdir -p "${XDG_CACHE_HOME:-/tmp/cache}"

if [[ ! -s "${model_path}" ]]; then
    echo "Downloading Whisper model ${model_name} into ${models_dir}" >&2
    /app/models/download-ggml-model.sh "${model_name}" "${models_dir}"
fi

vad_args=()
if [[ "${vad_enabled,,}" == "true" || "${vad_enabled}" == "1" || "${vad_enabled,,}" == "yes" ]]; then
    if [[ ! -s "${vad_path}" ]]; then
        echo "Downloading Whisper VAD model ${vad_name} into ${models_dir}" >&2
        /app/models/download-vad-model.sh "${vad_name}" "${models_dir}"
    fi
    vad_args=(
        --vad
        --vad-model "${vad_path}"
        --vad-min-speech-duration-ms "${WHISPER_VAD_MIN_SPEECH_MS:-150}"
        --vad-min-silence-duration-ms "${WHISPER_VAD_MIN_SILENCE_MS:-500}"
        --vad-max-speech-duration-s "${WHISPER_VAD_MAX_SPEECH_S:-28}"
        --vad-speech-pad-ms "${WHISPER_VAD_PAD_MS:-200}"
    )
fi

suppress_nst_args=()
if [[ "${WHISPER_SUPPRESS_NST:-false}" == "true" ]]; then
    suppress_nst_args+=(--suppress-nst)
fi

exec whisper-server \
    --model "${model_path}" \
    --host "${WHISPER_HOST:-0.0.0.0}" \
    --port "${WHISPER_PORT:-8080}" \
    --language "${WHISPER_LANGUAGE:-auto}" \
    --threads "${WHISPER_THREADS:-6}" \
    --beam-size "${WHISPER_BEAM:-5}" \
    --best-of "${WHISPER_BEST_OF:-5}" \
    "${suppress_nst_args[@]}" \
    "${vad_args[@]}"
