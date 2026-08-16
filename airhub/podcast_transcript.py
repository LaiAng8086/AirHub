"""使用本机 NVIDIA GPU 和 Whisper turbo 转录播客并生成对话 HTML。"""

from __future__ import annotations

import gc
import html
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .xiaoyuzhou import PodcastEpisode, XiaoyuzhouError


WHISPER_MODEL = "turbo"
WHISPER_UPSTREAM_MODEL = "openai/whisper-large-v3-turbo"
MODELSCOPE_TURBO_MODEL = "pengzhendong/faster-whisper-large-v3-turbo"


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
            "text": self.text,
        }


@dataclass(frozen=True)
class TranscriptResult:
    language: str
    language_probability: float
    duration: float
    gpu_index: int
    compute_type: str
    speaker_method: str
    segments: tuple[TranscriptSegment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": WHISPER_UPSTREAM_MODEL,
            "runtime": "faster-whisper",
            "language": self.language,
            "language_probability": round(self.language_probability, 6),
            "duration": round(self.duration, 3),
            "gpu_index": self.gpu_index,
            "compute_type": self.compute_type,
            "speaker_method": self.speaker_method,
            "segments": [item.to_dict() for item in self.segments],
        }


def select_gpu_index() -> int:
    """选择当前空闲显存最多的本机 GPU，不创建 Slurm 作业。"""

    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible not in {"-1", "NoDevFiles"}:
        # CUDA 进程看到的设备会重新从 0 编号；尊重外部已有的可见设备约束。
        return 0
    command = [
        "nvidia-smi",
        "--query-gpu=index,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XiaoyuzhouError("本机 NVIDIA GPU 状态读取失败，不能运行 Whisper turbo") from exc
    candidates: list[tuple[int, int]] = []
    for line in completed.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            continue
        try:
            candidates.append((int(fields[0]), int(fields[1])))
        except ValueError:
            continue
    if not candidates:
        raise XiaoyuzhouError("没有检测到可用的本机 NVIDIA GPU")
    return max(candidates, key=lambda item: (item[1], -item[0]))[0]


def _is_oom_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "out of memory",
            "cuda_error_out_of_memory",
            "failed to allocate",
            "cublas_status_alloc_failed",
        )
    )


def _looks_like_model_network_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "huggingface",
            "connection",
            "connecterror",
            "timed out",
            "timeout",
            "name resolution",
            "snapshot",
            "couldn't reach",
        )
    )


def _load_turbo_model(
    root: Path,
    gpu_index: int,
    compute_type: str,
    output: Callable[[str], None],
) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise XiaoyuzhouError(
            "缺少 faster-whisper，请先运行 bash run/setup_xiaoyuzhou_whisper.sh"
        ) from exc

    try:
        return WhisperModel(
            WHISPER_MODEL,
            device="cuda",
            device_index=gpu_index,
            compute_type=compute_type,
            cpu_threads=4,
            num_workers=1,
        )
    except Exception as primary_exc:
        if _is_oom_error(primary_exc) or not _looks_like_model_network_error(primary_exc):
            raise
        output("[WARN] HuggingFace turbo 模型获取失败，切换 ModelScope 备源。")
        try:
            from modelscope import snapshot_download

            model_path = snapshot_download(
                MODELSCOPE_TURBO_MODEL,
                cache_dir=str(root / "cache" / "modelscope"),
            )
            return WhisperModel(
                model_path,
                device="cuda",
                device_index=gpu_index,
                compute_type=compute_type,
                cpu_threads=4,
                num_workers=1,
            )
        except Exception as backup_exc:
            raise XiaoyuzhouError(
                "Whisper turbo 模型从 HuggingFace 与 ModelScope 获取均失败："
                f"{backup_exc}"
            ) from backup_exc


def transcribe_with_turbo(
    audio_path: Path,
    episode: PodcastEpisode,
    root: Path,
    *,
    output: Callable[[str], None] = print,
) -> TranscriptResult:
    """固定使用 openai/whisper-large-v3-turbo，在本机 GPU 上执行。"""

    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise FileNotFoundError(f"待转录音频不存在或为空：{audio_path}")
    gpu_index = select_gpu_index()
    names = speaker_candidates(episode)
    prompt = ""
    if names:
        prompt = "本期播客说话人可能包括：" + "、".join(names) + "。"

    last_error: BaseException | None = None
    for compute_type, beam_size in (("float16", 5), ("int8_float16", 1)):
        model: Any = None
        try:
            output(
                f"[INFO] 本机 GPU {gpu_index} 加载 {WHISPER_UPSTREAM_MODEL} "
                f"({compute_type})"
            )
            model = _load_turbo_model(root, gpu_index, compute_type, output)
            generated, info = model.transcribe(
                str(audio_path),
                task="transcribe",
                language=None,
                beam_size=beam_size,
                best_of=beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=True,
                initial_prompt=prompt or None,
                word_timestamps=False,
                log_progress=True,
            )
            raw_segments: list[TranscriptSegment] = []
            for index, segment in enumerate(generated, start=1):
                text = str(segment.text).strip()
                if text:
                    raw_segments.append(
                        TranscriptSegment(float(segment.start), float(segment.end), text)
                    )
                if index % 50 == 0:
                    output(f"[INFO] Whisper 已生成 {index} 个时间片段")
            if not raw_segments:
                raise XiaoyuzhouError("Whisper turbo 没有生成可用文本")
            assigned, method = assign_speakers(audio_path, raw_segments, names)
            merged = merge_speaker_turns(assigned)
            return TranscriptResult(
                language=str(getattr(info, "language", "")),
                language_probability=float(
                    getattr(info, "language_probability", 0.0) or 0.0
                ),
                duration=float(getattr(info, "duration", 0.0) or 0.0),
                gpu_index=gpu_index,
                compute_type=compute_type,
                speaker_method=method,
                segments=tuple(merged),
            )
        except Exception as exc:
            last_error = exc
            if not _is_oom_error(exc) or compute_type == "int8_float16":
                raise
            output(
                "[WARN] Whisper turbo 遇到显存不足，仍在本机使用 turbo，"
                "改为 int8_float16 和 beam_size=1 重试。"
            )
        finally:
            if model is not None:
                del model
            gc.collect()
    raise XiaoyuzhouError(f"Whisper turbo 转录失败：{last_error}")


def speaker_candidates(episode: PodcastEpisode) -> list[str]:
    """从结构化主播信息与 shownotes 中提取可展示的人名候选。"""

    names: list[str] = []
    for name in episode.podcasters:
        cleaned = name.strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)
    if not names and episode.author and episode.author != "未知主播":
        for part in re.split(r"\s*[/、,，&]\s*", episode.author):
            cleaned = part.strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
    shownotes = html.unescape(re.sub(r"<[^>]+>", " ", episode.shownotes))
    for match in re.finditer(
        r"(?:主播|主持人|嘉宾|本期嘉宾)\s*[：:]\s*([^\n；;。]{1,80})",
        shownotes,
    ):
        for part in re.split(r"\s*[/、,，&和]\s*", match.group(1)):
            cleaned = re.sub(r"[（(].*?[）)]", "", part).strip(" -*#")
            if 1 <= len(cleaned) <= 24 and cleaned not in names:
                names.append(cleaned)
    return names[:6]


def _segment_features(audio: Any, sampling_rate: int, segment: TranscriptSegment) -> Any:
    import numpy as np

    start = max(0, int(segment.start * sampling_rate))
    end = min(len(audio), int(segment.end * sampling_rate))
    samples = np.asarray(audio[start:end], dtype=np.float32)
    if samples.size < int(0.35 * sampling_rate):
        return None
    max_samples = 30 * sampling_rate
    if samples.size > max_samples:
        offset = (samples.size - max_samples) // 2
        samples = samples[offset : offset + max_samples]
    samples = samples - float(samples.mean())
    scale = float(np.max(np.abs(samples)))
    if scale > 1e-6:
        samples = samples / scale
    emphasized = np.concatenate((samples[:1], samples[1:] - 0.97 * samples[:-1]))
    frame_length = 400
    hop = 160
    if emphasized.size < frame_length:
        emphasized = np.pad(emphasized, (0, frame_length - emphasized.size))
    frame_count = 1 + (emphasized.size - frame_length) // hop
    indices = (
        np.arange(frame_length)[None, :]
        + hop * np.arange(frame_count)[:, None]
    )
    frames = emphasized[indices] * np.hanning(frame_length)[None, :]
    spectrum = np.abs(np.fft.rfft(frames, n=512)) ** 2
    log_spectrum = np.log(spectrum + 1e-8)
    bands = np.array_split(log_spectrum[:, 2:], 24, axis=1)
    band_energy = np.stack([band.mean(axis=1) for band in bands], axis=1)
    mean = band_energy.mean(axis=0)
    std = band_energy.std(axis=0)
    zcr = float(np.mean(np.abs(np.diff(np.signbit(samples)))))
    rms = float(np.sqrt(np.mean(samples**2) + 1e-9))
    return np.concatenate((mean, std, np.asarray([zcr, rms], dtype=np.float32)))


def _deterministic_kmeans(features: Any, cluster_count: int) -> list[int]:
    import numpy as np

    values = np.asarray(features, dtype=np.float32)
    values = (values - values.mean(axis=0)) / (values.std(axis=0) + 1e-5)
    centers = [values[0]]
    while len(centers) < cluster_count:
        distances = np.min(
            np.stack(
                [np.sum((values - center) ** 2, axis=1) for center in centers],
                axis=1,
            ),
            axis=1,
        )
        centers.append(values[int(np.argmax(distances))])
    center_values = np.stack(centers)
    labels = np.zeros(len(values), dtype=np.int64)
    for _ in range(30):
        distances = np.stack(
            [np.sum((values - center) ** 2, axis=1) for center in center_values],
            axis=1,
        )
        updated = np.argmin(distances, axis=1)
        if np.array_equal(updated, labels):
            break
        labels = updated
        for cluster in range(cluster_count):
            members = values[labels == cluster]
            if len(members):
                center_values[cluster] = members.mean(axis=0)
    return [int(item) for item in labels]


def assign_speakers(
    audio_path: Path,
    segments: list[TranscriptSegment],
    names: list[str],
) -> tuple[list[TranscriptSegment], str]:
    """用本地声学聚类形成对话轮次；候选姓名按首次出现的声纹簇映射。"""

    if len(names) == 1:
        return (
            [TranscriptSegment(item.start, item.end, item.text, names[0]) for item in segments],
            "single-known-speaker",
        )
    try:
        from faster_whisper.audio import decode_audio

        audio = decode_audio(str(audio_path), sampling_rate=16000)
        valid_indexes: list[int] = []
        features: list[Any] = []
        for index, segment in enumerate(segments):
            feature = _segment_features(audio, 16000, segment)
            if feature is not None:
                valid_indexes.append(index)
                features.append(feature)
        requested_clusters = len(names) if names else 2
        cluster_count = min(max(1, requested_clusters), len(features), 6)
        if cluster_count <= 1:
            label = names[0] if names else "说话人 1"
            return (
                [TranscriptSegment(item.start, item.end, item.text, label) for item in segments],
                "single-acoustic-speaker",
            )
        labels = _deterministic_kmeans(features, cluster_count)
        cluster_order: list[int] = []
        for label in labels:
            if label not in cluster_order:
                cluster_order.append(label)
        cluster_names = {
            cluster: (
                names[position]
                if position < len(names)
                else f"说话人 {position + 1}"
            )
            for position, cluster in enumerate(cluster_order)
        }
        assigned_labels: dict[int, str] = {
            index: cluster_names[label]
            for index, label in zip(valid_indexes, labels)
        }
        previous = names[0] if names else "说话人 1"
        assigned: list[TranscriptSegment] = []
        for index, segment in enumerate(segments):
            speaker = assigned_labels.get(index, previous)
            previous = speaker
            assigned.append(
                TranscriptSegment(segment.start, segment.end, segment.text, speaker)
            )
        method = (
            "acoustic-clustering-with-name-order"
            if names
            else "acoustic-clustering-generic-speakers"
        )
        return assigned, method
    except Exception:
        fallback = names[0] if names else "说话人"
        return (
            [TranscriptSegment(item.start, item.end, item.text, fallback) for item in segments],
            "speaker-fallback",
        )


def merge_speaker_turns(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    merged: list[TranscriptSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].speaker == segment.speaker
            and segment.start - merged[-1].end <= 2.0
            and len(merged[-1].text) + len(segment.text) <= 1200
        ):
            previous = merged[-1]
            merged[-1] = TranscriptSegment(
                previous.start,
                segment.end,
                f"{previous.text} {segment.text}".strip(),
                previous.speaker,
            )
        else:
            merged.append(segment)
    return merged


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_dialogue_html(
    episode: PodcastEpisode,
    transcript: TranscriptResult,
    *,
    polished: bool = False,
    polish_notes: list[str] | tuple[str, ...] = (),
) -> str:
    """生成可直接阅读的自包含对话 HTML。"""

    title = html.escape(episode.title)
    podcast = html.escape(episode.podcast_title)
    author = html.escape(episode.author)
    source_url = f"https://www.xiaoyuzhoufm.com/episode/{episode.eid}"
    turns: list[str] = []
    for segment in transcript.segments:
        turns.append(
            "<article class=\"turn\">"
            f"<time>{_clock(segment.start)}</time>"
            f"<p class=\"dialogue\"><span class=\"speaker\">{html.escape(segment.speaker)}：</span>"
            f"<span class=\"content\">{html.escape(segment.text)}</span></p>"
            "</article>"
        )
    if polished:
        note = (
            "文本已由 DeepSeek 根据原始 Whisper 转录、节目公开信息与上下文校订；"
            "重点复核了说话人名称、轮次分段和专有名词。原始 Whisper JSON 保持不变，"
            "重要内容仍建议结合音频核验。"
        )
        if polish_notes:
            note += " 校订备注：" + "；".join(polish_notes[:8])
    else:
        note = (
            "说话人标签由本地声学聚类和节目公开主播名单自动整理；多人节目中的姓名映射"
            "可能需要人工校对。"
        )
    eyebrow = "AIRHUB · WHISPER + DEEPSEEK 对话转录" if polished else "AIRHUB · 小宇宙播客对话转录"
    polish_footer = " · DeepSeek 文本与说话人校订" if polished else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}｜对话转录</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#697386; --line:#dce2ea;
      --paper:#f5f1e8; --card:#fffdf8; --accent:#0a6e68; --accent2:#d97706; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--paper);
      font:17px/1.75 system-ui,-apple-system,"Noto Sans SC",sans-serif; }}
    main {{ width:min(1080px,calc(100% - 32px)); margin:32px auto 72px; }}
    header {{ padding:34px 38px; color:white; border-radius:18px;
      background:linear-gradient(125deg,#063b3a,#0a6e68 62%,#b45309); box-shadow:0 18px 50px #183b3a26; }}
    .eyebrow {{ letter-spacing:.12em; opacity:.82; font-size:.82rem; }}
    h1 {{ margin:.35rem 0 .65rem; font-size:clamp(1.8rem,4vw,3.25rem); line-height:1.18; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; opacity:.9; }}
    .meta a {{ color:white; }}
    .notice {{ margin:22px 0; padding:14px 18px; border-left:4px solid var(--accent2);
      border-radius:8px; background:#fff8e8; color:#65420f; }}
    .turns {{ display:grid; gap:14px; }}
    .turn {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
      padding:18px 22px; box-shadow:0 5px 16px #3341550b; }}
    .speaker {{ color:var(--accent); font-weight:800; font-size:1.05rem; }}
    time {{ float:right; margin:.35rem 0 0 12px; color:var(--muted);
      font:600 .78rem/1.2 ui-monospace,monospace; }}
    p.dialogue {{ margin:0; white-space:pre-wrap; }}
    .content {{ margin-left:.3em; }}
    footer {{ margin-top:28px; color:var(--muted); font-size:.86rem; text-align:center; }}
    @media (max-width:600px) {{ main {{ width:min(100% - 18px,1080px); margin-top:9px; }}
      header {{ padding:24px 20px; border-radius:12px; }} .turn {{ padding:15px 16px; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">{eyebrow}</div>
    <h1>{title}</h1>
    <div class="meta"><span>播客：{podcast}</span><span>博主：{author}</span>
      <span>日期：{html.escape(episode.date)}</span>
      <a href="{source_url}">公开单集页</a></div>
  </header>
  <aside class="notice">{html.escape(note)}</aside>
  <section class="turns" aria-label="播客对话">
    {''.join(turns)}
  </section>
  <footer>使用 {WHISPER_UPSTREAM_MODEL} 在本机 NVIDIA GPU 转录{polish_footer} · 语言：{html.escape(transcript.language or '自动检测')}</footer>
</main>
</body>
</html>
"""


def write_transcript_json(path: Path, transcript: TranscriptResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path
