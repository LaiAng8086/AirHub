#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_environment.sh"
ensure_airhub_python

cd "${PROJECT_ROOT}"
set +e
PREPARE_OUTPUT="$(python3 -m airhub.podcast_polish --root "${PROJECT_ROOT}" --prepare)"
PREPARE_STATUS=$?
set -e
if ((PREPARE_STATUS != 0)); then
    printf '[ERROR] 播客 DeepSeek 润色任务准备失败 status=%d。\n' "${PREPARE_STATUS}" >&2
    exit "${PREPARE_STATUS}"
fi
if [[ -z "${PREPARE_OUTPUT}" ]]; then
    printf '[DONE] 当前小宇宙任务没有需要 DeepSeek 润色的已转录节目\n'
    exit 0
fi
mapfile -t PREPARED <<<"${PREPARE_OUTPUT}"
if ((${#PREPARED[@]} != 2)); then
    printf '[ERROR] 无法解析播客润色任务。\n' >&2
    exit 1
fi
MANIFEST_REL="${PREPARED[0]}"
TASK_COUNT="${PREPARED[1]}"
printf '[DONE] 播客润色分块准备 tasks=%s manifest=%s\n' "${TASK_COUNT}" "${MANIFEST_REL}"

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
printf '[DONE] DeepSeek Responses API 与 podcast-transcript-polisher skill 配置检查\n'

mkdir -p "${PROJECT_ROOT}/Logs"
RUN_STAMP="$(date '+%Y%m%d_%H%M%S')"
CODEX_LOG="${PROJECT_ROOT}/Logs/codex_podcast_polish_${RUN_STAMP}.log"
CODEX_RETRIES="${CODEX_PODCAST_POLISH_RETRIES:-2}"
CODEX_TIMEOUT="${CODEX_PODCAST_POLISH_TIMEOUT_SECONDS:-2700}"
if ! [[ "${CODEX_RETRIES}" =~ ^[1-9][0-9]*$ && "${CODEX_TIMEOUT}" =~ ^[1-9][0-9]*$ ]]; then
    printf '[ERROR] 播客润色重试次数和超时必须是正整数。\n' >&2
    exit 2
fi

FINALIZED=0
FINALIZE_STATUS=0
finalize_once() {
    if ((FINALIZED == 0)); then
        set +e
        python3 -m airhub.podcast_polish \
            --root "${PROJECT_ROOT}" \
            --finalize "${MANIFEST_REL}"
        FINALIZE_STATUS=$?
        set -e
        FINALIZED=1
        if ((FINALIZE_STATUS != 0)); then
            printf '[ERROR] 播客润色收尾存在失败 status=%d；失败节目保留 Whisper 草稿 HTML。\n' \
                "${FINALIZE_STATUS}" >&2
            return "${FINALIZE_STATUS}"
        fi
        printf '[DONE] 播客 DeepSeek 润色 HTML 与 Article 状态归档\n'
    fi
}
trap 'finalize_once || true' EXIT

while true; do
    set +e
    NEXT_OUTPUT="$(python3 -m airhub.podcast_polish \
        --root "${PROJECT_ROOT}" --next "${MANIFEST_REL}")"
    NEXT_STATUS=$?
    set -e
    if ((NEXT_STATUS != 0)); then
        printf '[ERROR] 读取下一个播客润色分块失败 status=%d。\n' "${NEXT_STATUS}" >&2
        break
    fi
    if [[ -z "${NEXT_OUTPUT}" ]]; then
        break
    fi
    mapfile -t TASK <<<"${NEXT_OUTPUT}"
    if ((${#TASK[@]} != 5)); then
        printf '[ERROR] 无法解析播客润色分块。\n' >&2
        break
    fi
    TASK_ID="${TASK[0]}"
    JOB_REL="${TASK[1]}"
    RESULT_REL="${TASK[2]}"
    PROGRESS_INDEX="${TASK[3]}"
    PROGRESS_TOTAL="${TASK[4]}"
    printf -v CODEX_PROMPT '%s 请读取 Producer 准备的播客分块任务 %s，处理其中全部 segments，并严格按 skill schema 将唯一结果 JSON 写入 %s。可按需联网核验人名和专有名词；不得改动原始 transcript、HTML、Article、manifest 或其他文件。' \
        '$podcast-transcript-polisher' "${JOB_REL}" "${RESULT_REL}"

    CODEX_STATUS=1
    for ((ATTEMPT = 1; ATTEMPT <= CODEX_RETRIES; ATTEMPT++)); do
        printf '[INFO] 播客润色 %s/%s task=%s attempt=%d/%d model=%s timeout=%ss\n' \
            "${PROGRESS_INDEX}" "${PROGRESS_TOTAL}" "${TASK_ID}" \
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

    set +e
    ACCEPT_OUTPUT="$(python3 -m airhub.podcast_polish \
        --root "${PROJECT_ROOT}" \
        --accept "${MANIFEST_REL}" "${TASK_ID}" \
        --codex-status "${CODEX_STATUS}")"
    ACCEPT_STATUS=$?
    set -e
    if ((ACCEPT_STATUS != 0)); then
        printf '[ERROR] 播客润色分块校验命令失败 task=%s status=%d。\n' \
            "${TASK_ID}" "${ACCEPT_STATUS}" >&2
        break
    fi
    mapfile -t ACCEPTED <<<"${ACCEPT_OUTPUT}"
    if [[ "${ACCEPTED[0]:-}" == "accepted" ]]; then
        printf '[DONE] 播客润色分块校验 %s/%s task=%s\n' \
            "${PROGRESS_INDEX}" "${PROGRESS_TOTAL}" "${TASK_ID}"
    else
        printf '[ERROR] 播客润色分块未通过 task=%s reason=%s\n' \
            "${TASK_ID}" "${ACCEPTED[1]:-未知错误}" >&2
    fi
done

finalize_once
trap - EXIT
if ((FINALIZE_STATUS != 0)); then
    printf '[ERROR] 播客 DeepSeek 润色未全部成功；日志: %s\n' \
        "${CODEX_LOG#${PROJECT_ROOT}/}" >&2
    exit "${FINALIZE_STATUS}"
fi
printf '[DONE] 播客 DeepSeek 润色完成 log=%s\n' "${CODEX_LOG#${PROJECT_ROOT}/}"
