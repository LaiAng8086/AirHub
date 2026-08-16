#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python

if (($# != 1)); then
    printf '[ERROR] 用法: bash run/run_paper_digest_codex.sh inbox/or/processing/article.json\n' >&2
    printf '[ERROR] 如需编号选择文章，请运行: bash run/run_airhub.sh --action paper-digest\n' >&2
    exit 2
fi

cd "${PROJECT_ROOT}"

if ! command -v codex >/dev/null 2>&1; then
    printf '[ERROR] 未找到 codex CLI，请先安装 Codex。\n' >&2
    exit 1
fi
printf '[DONE] Codex CLI 检查\n'

set +e
DEEPSEEK_RUNTIME_OUTPUT="$(python3 -m airhub.deepseek_codex --root "${PROJECT_ROOT}")"
DEEPSEEK_RUNTIME_STATUS=$?
set -e
if ((DEEPSEEK_RUNTIME_STATUS != 0)); then
    exit "${DEEPSEEK_RUNTIME_STATUS}"
fi
mapfile -t DEEPSEEK_RUNTIME <<<"${DEEPSEEK_RUNTIME_OUTPUT}"
if ((${#DEEPSEEK_RUNTIME[@]} != 3)) || \
    [[ -z "${DEEPSEEK_RUNTIME[0]}" || -z "${DEEPSEEK_RUNTIME[1]}" || -z "${DEEPSEEK_RUNTIME[2]}" ]]; then
    printf '[ERROR] 无法解析 DeepSeek Codex 运行配置。\n' >&2
    exit 1
fi
DEEPSEEK_CODEX_HOME_DIR="${DEEPSEEK_RUNTIME[0]}"
CODEX_MODEL="${DEEPSEEK_RUNTIME[1]}"
CODEX_REASONING="${DEEPSEEK_RUNTIME[2]}"
printf 'Provider: deepseek\nModel: %s\nReasoning: %s\n' "${CODEX_MODEL}" "${CODEX_REASONING}"
printf '[DONE] DeepSeek Responses API 与 paper-digest skill 配置检查\n'

SELECT_ARGS=(python3 -m airhub.codex_digest --root "${PROJECT_ROOT}" --article "$1")
set +e
SELECTION_OUTPUT="$("${SELECT_ARGS[@]}")"
SELECTION_STATUS=$?
set -e
if ((SELECTION_STATUS != 0)); then
    exit "${SELECTION_STATUS}"
fi
if [[ -z "${SELECTION_OUTPUT}" ]]; then
    printf '[DONE] 当前没有尚未生成默认 HTML 的 inbox Article\n'
    exit 0
fi
mapfile -t TASK_PATHS <<<"${SELECTION_OUTPUT}"
if ((${#TASK_PATHS[@]} != 2)) || [[ -z "${TASK_PATHS[0]}" || -z "${TASK_PATHS[1]}" ]]; then
    printf '[ERROR] 无法解析 paper-digest 任务路径。\n' >&2
    exit 1
fi
ARTICLE_REL="${TASK_PATHS[0]}"
OUTPUT_REL="${TASK_PATHS[1]}"
OUTPUT_ABS="${PROJECT_ROOT}/${OUTPUT_REL}"
ARTICLE_ID="$(basename "${ARTICLE_REL}" .json)"
printf 'Article: %s\nOutput: %s\n' "${ARTICLE_REL}" "${OUTPUT_REL}"
printf '[DONE] paper-digest Article 选择\n'

mkdir -p "${PROJECT_ROOT}/Logs"
RUN_STAMP="$(date '+%Y%m%d_%H%M%S')"
CODEX_LOG="${PROJECT_ROOT}/Logs/codex_paper_digest_${ARTICLE_ID}_${RUN_STAMP}.log"
python3 -m airhub.digest_preflight --root "${PROJECT_ROOT}" "${ARTICLE_REL}" 2>&1 | tee "${CODEX_LOG}"

CODEX_RETRIES="${CODEX_PAPER_DIGEST_RETRIES:-3}"
CODEX_TIMEOUT="${CODEX_PAPER_DIGEST_TIMEOUT_SECONDS:-2700}"
if ! [[ "${CODEX_RETRIES}" =~ ^[1-9][0-9]*$ ]]; then
    printf '[ERROR] CODEX_PAPER_DIGEST_RETRIES 必须是正整数。\n' >&2
    exit 2
fi
if ! [[ "${CODEX_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
    printf '[ERROR] CODEX_PAPER_DIGEST_TIMEOUT_SECONDS 必须是正整数。\n' >&2
    exit 2
fi
printf -v CODEX_PROMPT '%s 请读取 %s，严格按照 skill 的全部规则逐节生成完整中文论文解读，并将最终自包含 HTML 写入 %s。只使用 Producer 已准备的本地 Article 字段、HTML 原文或 PDF、图片、表格和视频，不要联网补抓论文素材。交付前必须运行图片内嵌工具，并确认每个 img src 都是 data: URI。你的职责仅限生成和校验该 HTML：不要修改、移动或完成 Article JSON，不要运行 airhub.codex_digest --complete，不要更新队列状态，也不要创建 Work_dirs、Logs 或 code_review 维护文件；外层脚本会在校验通过后统一完成这些操作。' \
    '$paper-digest' "${ARTICLE_REL}" "${OUTPUT_REL}"

CODEX_STATUS=1
for ((ATTEMPT = 1; ATTEMPT <= CODEX_RETRIES; ATTEMPT++)); do
    printf '[INFO] Codex 调用 attempt=%d/%d model=%s reasoning=%s timeout=%ss\n' \
        "${ATTEMPT}" "${CODEX_RETRIES}" "${CODEX_MODEL}" "${CODEX_REASONING}" "${CODEX_TIMEOUT}" | tee -a "${CODEX_LOG}"
    set +e
    timeout --signal=TERM --kill-after=30s "${CODEX_TIMEOUT}s" \
        env CODEX_HOME="${DEEPSEEK_CODEX_HOME_DIR}" \
        codex exec \
        --ephemeral \
        --disable apps \
        --disable plugins \
        --disable remote_plugin \
        --disable in_app_browser \
        --disable browser_use \
        --disable computer_use \
        --disable image_generation \
        --skip-git-repo-check \
        --sandbox workspace-write \
        --color never \
        -m "${CODEX_MODEL}" \
        -c "model_reasoning_effort=\"${CODEX_REASONING}\"" \
        -C "${PROJECT_ROOT}" \
        "${CODEX_PROMPT}" 2>&1 | tee -a "${CODEX_LOG}"
    CODEX_STATUS=${PIPESTATUS[0]}
    set -e
    if ((CODEX_STATUS == 0)); then
        break
    fi
    if ((ATTEMPT == CODEX_RETRIES)); then
        break
    fi
    if ((CODEX_STATUS == 124 || CODEX_STATUS == 137)) || \
        rg -q -i 'Transport channel closed|Reconnecting|request timed out|failed to refresh available models|error sending request|WebSocket|timed out|response\.failed|insufficient_system_resource|429 Too Many Requests|500 Internal Server Error|503 Service Unavailable' "${CODEX_LOG}"; then
        RETRY_WAIT=$((ATTEMPT * 5))
        printf '[WARN] Codex 传输失败，%d 秒后自动重试。\n' "${RETRY_WAIT}" | tee -a "${CODEX_LOG}" >&2
        sleep "${RETRY_WAIT}"
        continue
    fi
    break
done
if ((CODEX_STATUS != 0)); then
    printf '[ERROR] Codex 调用失败 status=%d；完整日志: %s\n' \
        "${CODEX_STATUS}" "${CODEX_LOG#${PROJECT_ROOT}/}" >&2
    exit "${CODEX_STATUS}"
fi
printf '[DONE] DeepSeek Codex paper-digest 非交互调用\n'

if [[ ! -f "${OUTPUT_ABS}" ]]; then
    printf '[ERROR] Codex 返回成功，但没有生成预期 HTML: %s\n' "${OUTPUT_REL}" >&2
    exit 1
fi
python3 -m airhub.html.assets "${OUTPUT_ABS}" --base-dir "${PROJECT_ROOT}"
if rg -n "<img[^>]+src=['\"](https?:|/|\.\.?/|attachments/)" "${OUTPUT_ABS}"; then
    printf '[ERROR] HTML 中仍存在未内嵌的图片地址。\n' >&2
    exit 1
fi
printf '[DONE] HTML 自包含检查\n'
python3 -m airhub.codex_digest --root "${PROJECT_ROOT}" --complete "${ARTICLE_REL}" "${OUTPUT_REL}"
printf '[DONE] Article 状态更新并移入 finished\n'
printf '[DONE] paper-digest 完成 output=%s log=%s\n' "${OUTPUT_REL}" "${CODEX_LOG#${PROJECT_ROOT}/}"
