#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"

mkdir -p "$CODEX_HOME_DIR/skills"
for SKILL_NAME in paper-digest xhs-link-classifier podcast-transcript-polisher; do
    SOURCE_DIR="$ROOT_DIR/codex_skills/$SKILL_NAME"
    TARGET_DIR="$CODEX_HOME_DIR/skills/$SKILL_NAME"
    mkdir -p "$TARGET_DIR"
    cp -R "$SOURCE_DIR/." "$TARGET_DIR/"
    printf '[DONE] installed Codex %s skill at %s\n' "$SKILL_NAME" "$TARGET_DIR"
done
