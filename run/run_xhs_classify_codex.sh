#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python

cd "${PROJECT_ROOT}"
set +e
PREPARE_OUTPUT="$(python3 -m airhub.xhs_classifier --root "${PROJECT_ROOT}" --prepare)"
PREPARE_STATUS=$?
set -e
if ((PREPARE_STATUS != 0)); then
    printf '[ERROR] XHS 分类任务准备失败 status=%d。\n' "${PREPARE_STATUS}" >&2
    exit "${PREPARE_STATUS}"
fi
if [[ -z "${PREPARE_OUTPUT}" ]]; then
    printf '[DONE] cache/xhs 中没有待识别缓存\n'
    python3 -m airhub.xhs_activity --root "${PROJECT_ROOT}" --print-status
    exit 0
fi
mapfile -t TASK <<<"${PREPARE_OUTPUT}"
if ((${#TASK[@]} != 3)); then
    printf '[ERROR] 无法解析 XHS 分类任务。\n' >&2
    exit 1
fi
JOB_REL="${TASK[0]}"
RESULT_REL="${TASK[1]}"
ITEM_COUNT="${TASK[2]}"
CODEX_STATUS=1
FINALIZED=0

finalize_once() {
    if ((FINALIZED == 0)); then
        set +e
        python3 -m airhub.xhs_classifier \
            --root "${PROJECT_ROOT}" \
            --finalize "${JOB_REL}" "${RESULT_REL}" \
            --codex-status "${CODEX_STATUS}"
        FINALIZE_STATUS=$?
        set -e
        FINALIZED=1
        if ((FINALIZE_STATUS != 0)); then
            printf '[ERROR] XHS 分类收尾失败 status=%d\n' "${FINALIZE_STATUS}" >&2
            return "${FINALIZE_STATUS}"
        fi
        printf '[DONE] XHS 分类结果写入 manual/ 并清理对应 cache/xhs 缓存\n'
        python3 -m airhub.xhs_activity --root "${PROJECT_ROOT}" --print-status
    fi
}
trap 'finalize_once || true' EXIT

printf '[DONE] XHS 全量缓存分类任务准备 items=%s job=%s\n' "${ITEM_COUNT}" "${JOB_REL}"
if [[ "${ITEM_COUNT}" == "0" ]]; then
    CODEX_STATUS=0
    finalize_once
    trap - EXIT
    exit 0
fi

if ! command -v codex >/dev/null 2>&1; then
    printf '[ERROR] 未找到 codex CLI。\n' >&2
    exit 1
fi
printf '[DONE] Codex CLI 检查\n'

set +e
RUNTIME_OUTPUT="$(python3 -m airhub.deepseek_codex --root "${PROJECT_ROOT}")"
RUNTIME_STATUS=$?
set -e
if ((RUNTIME_STATUS != 0)); then
    printf '[ERROR] DeepSeek Codex 运行配置失败 status=%d。\n' "${RUNTIME_STATUS}" >&2
    exit "${RUNTIME_STATUS}"
fi
mapfile -t RUNTIME <<<"${RUNTIME_OUTPUT}"
if ((${#RUNTIME[@]} != 3)); then
    printf '[ERROR] 无法解析 DeepSeek Codex 运行配置。\n' >&2
    exit 1
fi
DEEPSEEK_CODEX_HOME_DIR="${RUNTIME[0]}"
CODEX_MODEL="${RUNTIME[1]}"
CODEX_REASONING="${RUNTIME[2]}"
printf '[DONE] DeepSeek Responses API 与 xhs-link-classifier skill 配置检查\n'

mkdir -p "${PROJECT_ROOT}/Logs"
RUN_STAMP="$(date '+%Y%m%d_%H%M%S')"
CODEX_LOG="${PROJECT_ROOT}/Logs/codex_xhs_classifier_${RUN_STAMP}.log"
CODEX_RETRIES="${CODEX_XHS_CLASSIFIER_RETRIES:-2}"
CODEX_TIMEOUT="${CODEX_XHS_CLASSIFIER_TIMEOUT_SECONDS:-2700}"
if ! [[ "${CODEX_RETRIES}" =~ ^[1-9][0-9]*$ && "${CODEX_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
    printf '[ERROR] XHS classifier 重试次数和超时必须是正整数。\n' >&2
    exit 2
fi
printf -v CODEX_PROMPT '%s 请读取 Producer 准备的 %s，处理全部 items，并严格按 skill schema 将唯一结果 JSON 写入 %s。允许联网检索与核验真实 arXiv 编号；不要修改 cache/xhs、manual、Article 队列或其他文件，外层脚本会统一校验、写入 manual 并清缓存。' \
    '$xhs-link-classifier' "${JOB_REL}" "${RESULT_REL}"

for ((ATTEMPT = 1; ATTEMPT <= CODEX_RETRIES; ATTEMPT++)); do
    printf '[INFO] XHS classifier attempt=%d/%d model=%s timeout=%ss\n' \
        "${ATTEMPT}" "${CODEX_RETRIES}" "${CODEX_MODEL}" "${CODEX_TIMEOUT}" | tee -a "${CODEX_LOG}"
    set +e
    timeout --signal=TERM --kill-after=30s "${CODEX_TIMEOUT}s" \
        env CODEX_HOME="${DEEPSEEK_CODEX_HOME_DIR}" \
        codex exec \
        --ephemeral \
        --disable apps \
        --disable plugins \
        --disable remote_plugin \
        --enable browser_use \
        --enable in_app_browser \
        --enable standalone_web_search \
        --disable computer_use \
        --disable image_generation \
        --skip-git-repo-check \
        --sandbox workspace-write \
        --color never \
        -m "${CODEX_MODEL}" \
        -c "model_reasoning_effort=\"${CODEX_REASONING}\"" \
        -c 'sandbox_workspace_write.network_access=true' \
        -C "${PROJECT_ROOT}" \
        "${CODEX_PROMPT}" 2>&1 | tee -a "${CODEX_LOG}"
    CODEX_STATUS=${PIPESTATUS[0]}
    set -e
    if ((CODEX_STATUS == 0)); then
        break
    fi
    if ((ATTEMPT < CODEX_RETRIES)) && \
        rg -q -i 'Transport channel closed|request timed out|response\.failed|429|500|503|WebSocket' "${CODEX_LOG}"; then
        sleep "$((ATTEMPT * 5))"
        continue
    fi
    break
done

finalize_once
trap - EXIT
if ((CODEX_STATUS != 0)); then
    printf '[ERROR] XHS Codex 分类调用失败 status=%d；缓存仍已按约定清理；日志: %s\n' \
        "${CODEX_STATUS}" "${CODEX_LOG#${PROJECT_ROOT}/}" >&2
    exit "${CODEX_STATUS}"
fi
printf '[DONE] XHS Codex 分类完成 log=%s\n' "${CODEX_LOG#${PROJECT_ROOT}/}"
