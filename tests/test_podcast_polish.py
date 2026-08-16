from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from airhub.models import Article, Attachment
from airhub.podcast_polish import (
    accept_podcast_polish_task,
    finalize_podcast_polish_job,
    prepare_podcast_polish_job,
)
from airhub.podcast_worker import create_podcast_job
from airhub.storage import ArticleStorage
from airhub.xiaoyuzhou import PodcastEpisode


EID = "0123456789abcdef01234567"


def episode() -> PodcastEpisode:
    return PodcastEpisode(
        eid=EID,
        pid="podcast-id",
        title="聊 vLLM 与机器人",
        podcast_title="测试播客",
        author="主持人甲 / 嘉宾乙",
        pub_date="2026-08-16T08:00:00.000Z",
        duration=120,
        shownotes="主播：主持人甲\n嘉宾：嘉宾乙\n本期讨论 vLLM。",
        description="技术访谈",
        podcasters=("主持人甲", "嘉宾乙"),
        raw={},
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_completed_episode(root: Path) -> tuple[Path, Path]:
    selected = episode()
    job_path = create_podcast_job(root, [selected])
    raw_path = root / "attachments" / "transcript" / selected.date / f"{selected.article_id}.json"
    write_json(
        raw_path,
        {
            "model": "openai/whisper-large-v3-turbo",
            "runtime": "faster-whisper",
            "language": "zh",
            "language_probability": 0.99,
            "duration": 15,
            "gpu_index": 0,
            "compute_type": "float16",
            "speaker_method": "test",
            "segments": [
                {"start": 0, "end": 4, "speaker": "说话人 1", "text": "欢迎今天的嘉宾乙。"},
                {"start": 4, "end": 9, "speaker": "说话人 2", "text": "我们聊一下 v l l m。"},
                {"start": 9, "end": 14, "speaker": "说话人 1", "text": "它在推理服务里很常见。"},
            ],
        },
    )
    html_path = root / "attachments" / "html" / f"{selected.article_id}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text("<html>WHISPER DRAFT</html>", encoding="utf-8")
    article = Article(
        id=selected.article_id,
        type="podcast_episode",
        source="xiaoyuzhou",
        title=selected.title,
        authors=[selected.author],
        publish_date=selected.pub_date,
        url=f"https://www.xiaoyuzhoufm.com/episode/{selected.eid}",
        attachments=[
            Attachment(type="transcript", path=raw_path.relative_to(root).as_posix()),
            Attachment(type="html", path=html_path.relative_to(root).as_posix()),
        ],
        metadata={
            "podcast": {
                "title": selected.podcast_title,
                "author": selected.author,
                "duration": selected.duration,
                "podcasters": list(selected.podcasters),
            }
        },
    )
    ArticleStorage(root).complete(article, html_path.relative_to(root).as_posix())
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["status"] = "completed"
    job["episodes"][0].update(
        {
            "status": "completed",
            "stage": "completed",
            "article_id": selected.article_id,
            "html": html_path.relative_to(root).as_posix(),
        }
    )
    write_json(job_path, job)
    return job_path, html_path


def write_valid_results(root: Path, manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_ids = []
    for task in manifest["tasks"]:
        task_ids.append(task["task_id"])
        chunk_job = json.loads((root / task["job_path"]).read_text(encoding="utf-8"))
        segments = []
        for source in chunk_job["segments"]:
            text = source["text"].replace("v l l m", "vLLM")
            speaker = "嘉宾乙" if "vLLM" in text else "主持人甲"
            segments.append(
                {"segment_id": source["segment_id"], "speaker": speaker, "text": text}
            )
        write_json(
            root / task["result_path"],
            {
                "version": 1,
                "chunk_id": chunk_job["chunk_id"],
                "segments": segments,
                "notes": ["根据 shownotes 将 v l l m 校正为 vLLM"],
            },
        )
        accepted = accept_podcast_polish_task(
            root, manifest_path, task["task_id"], codex_status=0
        )
        if accepted["status"] != "accepted":
            raise AssertionError(accepted)
    return task_ids


class PodcastPolishTest(unittest.TestCase):
    def test_full_episode_is_chunked_validated_and_committed_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path, html_path = prepare_completed_episode(root)
            prepared = prepare_podcast_polish_job(
                root,
                now=datetime(2026, 8, 16, 12, 30, 45),
                max_chars=40,
                max_segments=2,
            )
            self.assertIsNotNone(prepared)
            assert prepared is not None
            self.assertEqual(prepared.task_count, 2)
            manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
            first_job = json.loads(
                (root / manifest["tasks"][0]["job_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["segment_id"] for item in first_job["segments"]],
                ["seg-000001", "seg-000002"],
            )
            self.assertEqual(
                [item["segment_id"] for item in first_job["context_after"]],
                ["seg-000003"],
            )

            write_valid_results(root, prepared.manifest_path)
            result = finalize_podcast_polish_job(root, prepared.manifest_path)

            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["failed"], 0)
            rendered = html_path.read_text(encoding="utf-8")
            self.assertIn("WHISPER + DEEPSEEK", rendered)
            self.assertIn("vLLM", rendered)
            self.assertNotIn("WHISPER DRAFT", rendered)
            article = ArticleStorage(root).load_existing(episode().article_id)
            assert article is not None
            self.assertEqual(article.metadata["podcast_polish"]["status"], "success")
            polished = [item for item in article.attachments if item.type == "transcript_polished"]
            self.assertEqual(len(polished), 1)
            self.assertTrue((root / polished[0].path).is_file())
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["episodes"][0]["polish_status"], "success")
            self.assertEqual(job["episodes"][0]["stage"], "completed-polished")

            # Matching source hashes make repeated preparation idempotent.
            self.assertIsNone(prepare_podcast_polish_job(root))

    def test_invalid_chunk_marks_failure_and_preserves_draft_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path, html_path = prepare_completed_episode(root)
            prepared = prepare_podcast_polish_job(root, max_chars=10_000)
            assert prepared is not None
            manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
            task = manifest["tasks"][0]
            chunk_job = json.loads((root / task["job_path"]).read_text(encoding="utf-8"))
            write_json(
                root / task["result_path"],
                {
                    "version": 1,
                    "chunk_id": chunk_job["chunk_id"],
                    "segments": [],
                    "notes": [],
                },
            )

            accepted = accept_podcast_polish_task(
                root, prepared.manifest_path, task["task_id"], codex_status=0
            )
            self.assertEqual(accepted["status"], "failed")
            result = finalize_podcast_polish_job(root, prepared.manifest_path)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(html_path.read_text(encoding="utf-8"), "<html>WHISPER DRAFT</html>")
            article = ArticleStorage(root).load_existing(episode().article_id)
            assert article is not None
            self.assertEqual(article.metadata["podcast_polish"]["status"], "failed")
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["episodes"][0]["polish_status"], "failed")


if __name__ == "__main__":
    unittest.main()
