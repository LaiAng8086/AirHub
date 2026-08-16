# AirHub scripts

All public entry scripts are one-click Bash commands. Run them from any working
directory; each script resolves the repository root itself. Generated state stays
under the checkout unless a cache environment variable is explicitly overridden.

## Core environment and menu

### `run/setup_airhub.sh`

Creates `cache/airhub_env/`, installs `requirements.txt`, retries downloads with
long timeouts, switches from PyPI to the Tsinghua mirror on failure, and verifies
the core imports. It takes no parameters.

### `run/run_airhub.sh [AirHub arguments]`

Ensures the core environment and starts `python -m airhub`. With no arguments it
opens the interactive menu. Examples:

```bash
bash run/run_airhub.sh
bash run/run_airhub.sh --action prepare
bash run/run_airhub.sh --action paper-digest
bash run/run_airhub.sh --action xhs-download
```

The underlying CLI accepts configuration, root, and action flags shown by:

```bash
bash run/run_airhub.sh --help
```

## Codex skill workflows

### `run/install_codex_skill.sh`

Copies the bundled `paper-digest`, `xhs-link-classifier`, and
`podcast-transcript-polisher` skills into `${CODEX_HOME:-$HOME/.codex}/skills/`.
It takes no parameters. Normal menu workflows can also build an isolated Codex
home under `cache/` without modifying the user's existing configuration.

### `run/run_paper_digest_codex.sh ARTICLE_JSON`

Runs one Producer-prepared Article through the paper-digest skill, validates the
package before model use, embeds local images, and commits the Article only after
the self-contained HTML passes validation.

- `ARTICLE_JSON`: a repository-relative JSON path in `inbox/` or `processing/`.
- `CODEX_PAPER_DIGEST_RETRIES`: positive integer; default `3`.
- `CODEX_PAPER_DIGEST_TIMEOUT_SECONDS`: seconds per attempt; default `2700`.

For numbered batch selection, use `--action paper-digest` instead.

### `run/run_xhs_classify_codex.sh`

Prepares one job from every cache entry present under `cache/xhs/`, invokes the
XHS classifier skill, validates the result, writes a timestamped `manual/*.txt`,
and clears only cache entries captured by that job. Cleanup runs after success or
failure by design.

### `run/run_podcast_polish_codex.sh`

Chunks all prepared Whisper transcripts, calls the transcript-polisher skill for
each chunk, validates exact segment identity/order, and finalizes HTML only when
all chunks for an episode pass. Failed episodes keep their Whisper draft.

The three Codex workflows require `config/deepseek.json` and a `codex` executable.
API credentials are copied only into an ignored, mode-0600 isolated runtime.

## XHS text environment

### `run/setup_xhs_text.sh`

Uses the core environment's `uv` to create `xhs/.venv/` from `xhs/uv.lock`,
obtaining Python 3.12 if required. It falls back from PyPI to the Tsinghua mirror.
The minimal adapter uses an empty cookie jar and contains no media downloader.
The managed interpreter and package cache remain under `cache/uv/`.

Normally use menu action `xhs-download`; it confirms the number of distinct valid
links, then stores one batch in `cache/xhs/YYYYMMDD_HHMMSS/` while printing each
post's progress, title, URL, and success/failure status.

## Xiaoyuzhou and Whisper

### `run/setup_xiaoyuzhou_whisper.sh`

Creates `cache/xiaoyuzhou_whisper_env/` from
`config/requirements-xiaoyuzhou-whisper.txt`. Package installation falls back
from PyPI to the Tsinghua mirror. Whisper weights are not shipped; at first use
the worker tries Hugging Face and then ModelScope.

### `run/run_xiaoyuzhou_podcast.sh`

Runs environment setup, public episode audio download, local NVIDIA Whisper turbo
transcription, and Codex/DeepSeek transcript polishing. Logs are written under
`Logs/`. The worker selects the local NVIDIA GPU with the most available memory
and reduces compute precision if necessary.

## Tests

### `run/run_tests.sh`

Ensures the core environment and runs the offline `unittest` suite. It does not
download model weights or call external APIs.

## Runtime directories

The application creates these ignored locations as needed:

- `cache/`: Python environments, source caches, XHS text, and model caches.
- `attachments/`: source HTML/PDF/images, Blog snapshots, audio, transcripts,
  and final HTML.
- `data/`: state, selection reports, frequency profiles, and Blog catalogue.
- `inbox/`, `processing/`, `finished/`, `filtered/`, `metadata/`: Article state.
- `Work_dirs/`: temporary classification/polishing jobs.
- `Logs/` and `logs/`: runtime diagnostics.

Do not commit these directories. Before publishing changes, also confirm that
`config/deepseek.json`, `config/xiaoyuzhou_credentials.json`, and personal scope
CSV exports are absent from the staged Git tree.
