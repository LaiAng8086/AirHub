#!/usr/bin/env bash

AIRHUB_RUN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${AIRHUB_RUN_DIR}/.." && pwd)"
AIRHUB_ENV_DIR="${PROJECT_ROOT}/cache/airhub_env"

export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${PROJECT_ROOT}/cache/pip}"
export HF_HOME="${HF_HOME:-${PROJECT_ROOT}/cache/huggingface}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-${PROJECT_ROOT}/cache/modelscope}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${PROJECT_ROOT}/cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${PROJECT_ROOT}/cache/uv/python}"
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

ensure_airhub_python() {
    if [[ ! -x "${AIRHUB_ENV_DIR}/bin/python" ]]; then
        bash "${PROJECT_ROOT}/run/setup_airhub.sh"
    fi
    export PATH="${AIRHUB_ENV_DIR}/bin:${PATH}"
}
