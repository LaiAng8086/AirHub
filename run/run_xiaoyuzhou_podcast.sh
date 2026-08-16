#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python
ENV_DIR="${PROJECT_ROOT}/cache/xiaoyuzhou_whisper_env"
LOG_DIR="${PROJECT_ROOT}/Logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_PATH="${LOG_DIR}/xiaoyuzhou_podcast_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_ROOT}"
exec > >(tee -a "${LOG_PATH}") 2>&1

echo "[INFO] 日志：${LOG_PATH}"
bash run/setup_xiaoyuzhou_whisper.sh
echo "[INFO] 固定模型：openai/whisper-large-v3-turbo；执行：本机 NVIDIA GPU；Slurm：禁用"
set +e
"${ENV_DIR}/bin/python" -u -m airhub.podcast_worker
WORKER_STATUS=$?
set -e
if ((WORKER_STATUS == 0)); then
    echo "[DONE] 小宇宙公开音频下载与 Whisper turbo 草稿 HTML"
else
    printf '[ERROR] 部分小宇宙下载或 Whisper 转录失败 status=%d；继续润色成功转录的节目。\n' \
        "${WORKER_STATUS}" >&2
fi

set +e
bash run/run_podcast_polish_codex.sh
POLISH_STATUS=$?
set -e
if ((WORKER_STATUS != 0 || POLISH_STATUS != 0)); then
    printf '[ERROR] 小宇宙完整流水线未全部成功 worker=%d polish=%d。\n' \
        "${WORKER_STATUS}" "${POLISH_STATUS}" >&2
    exit 1
fi
echo "[DONE] 小宇宙 Whisper turbo + DeepSeek 校订对话 HTML"
