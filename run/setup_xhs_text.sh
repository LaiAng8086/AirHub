#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-600}"
export UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-8}"

cd "${PROJECT_ROOT}"
if ! command -v uv >/dev/null 2>&1; then
    printf '[ERROR] 未找到 uv，无法创建 XHS Python 3.12 隔离环境。\n' >&2
    exit 1
fi
printf '[DONE] XHS 环境工具与缓存路径检查\n'

if uv sync --project "${PROJECT_ROOT}/xhs" --no-dev --locked; then
    printf '[DONE] XHS 官方锁文件环境同步\n'
else
    printf '[WARN] 默认 PyPI 同步失败，切换清华 PyPI 备源。\n' >&2
    uv sync \
        --project "${PROJECT_ROOT}/xhs" \
        --no-dev \
        --locked \
        --default-index https://pypi.tuna.tsinghua.edu.cn/simple
    printf '[DONE] XHS 清华备源环境同步\n'
fi

"${PROJECT_ROOT}/xhs/.venv/bin/python" - <<'PY'
import importlib.util
from pathlib import Path

path = Path("xhs/airhub_text_export.py")
spec = importlib.util.spec_from_file_location("airhub_xhs_text_export", path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert callable(module.export)
PY
printf '[DONE] XHS 空登录态文本提取依赖检查\n'
