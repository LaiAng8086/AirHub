#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python
ENV_DIR="${PROJECT_ROOT}/cache/xiaoyuzhou_whisper_env"
REQUIREMENTS="${PROJECT_ROOT}/config/requirements-xiaoyuzhou-whisper.txt"
MARKER="${ENV_DIR}/.airhub_requirements.sha256"

cd "${PROJECT_ROOT}"
if [[ ! -x "${ENV_DIR}/bin/python" ]]; then
  python -m venv "${ENV_DIR}"
  echo "[DONE] 小宇宙 Whisper 项目内 Python 环境创建"
fi

REQUIREMENTS_HASH="$(sha256sum "${REQUIREMENTS}" | cut -d' ' -f1)"
INSTALLED_HASH=""
if [[ -f "${MARKER}" ]]; then
  INSTALLED_HASH="$(tr -d '\r\n' < "${MARKER}")"
fi
if [[ "${REQUIREMENTS_HASH}" != "${INSTALLED_HASH}" ]]; then
  if ! "${ENV_DIR}/bin/python" -m pip install \
    --timeout 600 --retries 10 --index-url https://pypi.org/simple \
    -r "${REQUIREMENTS}"; then
    echo "[WARN] PyPI 下载失败，切换清华 PyPI 备源。"
    "${ENV_DIR}/bin/python" -m pip install \
      --timeout 600 --retries 10 \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      -r "${REQUIREMENTS}"
  fi
  echo "[DONE] 小宇宙 Whisper turbo 依赖安装"
else
  echo "[DONE] 小宇宙 Whisper turbo 依赖检查（已就绪）"
fi

"${ENV_DIR}/bin/python" -c \
  'import faster_whisper; from modelscope import snapshot_download; print("[DONE] faster-whisper 与 ModelScope 备源导入检查")'
printf '%s\n' "${REQUIREMENTS_HASH}" > "${MARKER}"
