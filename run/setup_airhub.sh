#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="${PROJECT_ROOT}/cache/airhub_env"
REQUIREMENTS="${PROJECT_ROOT}/requirements.txt"
MARKER="${ENV_DIR}/.airhub_requirements.sha256"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${PROJECT_ROOT}/cache/pip}"

cd "${PROJECT_ROOT}"
python3 - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit("AirHub requires Python 3.9 or newer")
PY
printf '[DONE] Python 版本检查\n'

if [[ ! -x "${ENV_DIR}/bin/python" ]]; then
    python3 -m venv "${ENV_DIR}"
    printf '[DONE] AirHub 项目内 Python 环境创建\n'
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
        printf '[WARN] PyPI 安装失败，切换清华 PyPI 备源。\n' >&2
        "${ENV_DIR}/bin/python" -m pip install \
            --timeout 600 --retries 10 \
            --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
            -r "${REQUIREMENTS}"
    fi
    printf '%s\n' "${REQUIREMENTS_HASH}" > "${MARKER}"
    printf '[DONE] AirHub Python 依赖安装\n'
else
    printf '[DONE] AirHub Python 依赖检查（已就绪）\n'
fi

"${ENV_DIR}/bin/python" - <<'PY'
import fitz
import requests
from PIL import Image

assert fitz is not None and requests is not None and Image is not None
PY
printf '[DONE] AirHub 核心依赖导入检查\n'
