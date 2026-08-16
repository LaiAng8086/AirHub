from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from airhub.xhs import (
    download_xhs_texts,
    extract_xhs_candidates,
    extract_xhs_links,
)
from airhub.xhs_activity import (
    format_xhs_daily_activity,
    load_xhs_daily_activity,
    record_xhs_completion,
)
from airhub.xhs_classifier import (
    finalize_xhs_classification_job,
    prepare_xhs_classification_job,
)


class XHSPipelineTest(unittest.TestCase):
    def test_link_extraction_accepts_all_repository_forms_and_deduplicates(self):
        text = """
        文本 https://www.xiaohongshu.com/explore/abc?xsec_token=1，
        https://www.xiaohongshu.com/discovery/item/def?xsec_token=2
        www.xiaohongshu.com/user/profile/user123/ghi?xsec_token=3
        https://xhslink.com/aBcDeF。重复 https://xhslink.com/aBcDeF
        https://example.com/not-xhs
        """
        self.assertEqual(
            extract_xhs_links(text),
            [
                "https://www.xiaohongshu.com/explore/abc?xsec_token=1",
                "https://www.xiaohongshu.com/discovery/item/def?xsec_token=2",
                "https://www.xiaohongshu.com/user/profile/user123/ghi?xsec_token=3",
                "https://xhslink.com/aBcDeF",
            ],
        )

    def test_download_uses_second_precision_cache_folder_and_runner_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_python = root / "xhs" / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.touch()

            def fake_stream(command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                (output / "001_note.txt").write_text("正文", encoding="utf-8")
                (output / "_download_report.json").write_text(
                    json.dumps(
                        {
                            "saved": 1,
                            "failed": 1,
                            "entries": [
                                {
                                    "url": "https://www.xiaohongshu.com/explore/a?x=1",
                                    "title": "成功标题",
                                    "status": "saved",
                                },
                                {
                                    "url": "https://www.xiaohongshu.com/explore/b?x=1",
                                    "source_title": "失败标题",
                                    "status": "failed",
                                    "error": "fixture",
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return 0

            with patch("airhub.xhs._stream_command", side_effect=fake_stream) as stream:
                result = download_xhs_texts(
                    root,
                    [
                        "https://www.xiaohongshu.com/explore/a?x=1",
                        "https://www.xiaohongshu.com/explore/b?x=1",
                    ],
                    now=datetime(2026, 8, 13, 19, 10, 11),
                    source_text=(
                        "成功标题 https://www.xiaohongshu.com/explore/a?x=1\n"
                        "失败标题 https://www.xiaohongshu.com/explore/b?x=1"
                    ),
                )
            self.assertEqual(result.session_dir.name, "20260813_191011")
            self.assertEqual(result.saved, 1)
            self.assertEqual(result.failed, 1)
            self.assertEqual([entry.title for entry in result.entries], ["成功标题", "失败标题"])
            self.assertEqual(result.daily_sequence, 1)
            command = stream.call_args.args[0]
            self.assertIn("airhub_text_export.py", command[1])
            payload = json.loads(
                (result.session_dir / "_links.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payload["links"]), 2)
            self.assertEqual(payload["links"][1]["source_title"], "失败标题")

    def test_clipboard_candidates_keep_single_line_titles_and_deduplicate(self):
        candidates = extract_xhs_candidates(
            "标题：第一篇 https://www.xiaohongshu.com/explore/a\n"
            "第二篇 | https://xhslink.com/short\n"
            "重复 https://xhslink.com/short"
        )
        self.assertEqual([item.url for item in candidates], [
            "https://www.xiaohongshu.com/explore/a",
            "https://xhslink.com/short",
        ])
        self.assertEqual([item.source_title for item in candidates], ["第一篇", "第二篇"])

    def test_daily_activity_records_sequence_and_last_completion_to_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = record_xhs_completion(
                root,
                "download",
                "success",
                completed_at=datetime(2026, 8, 16, 10, 1, 2),
            )
            second = record_xhs_completion(
                root,
                "classify",
                "partial",
                completed_at=datetime(2026, 8, 16, 11, 2, 3),
            )
            self.assertEqual(first.count, 1)
            self.assertEqual(second.count, 2)
            loaded = load_xhs_daily_activity(root, on_date="2026-08-16")
            self.assertEqual([event["sequence"] for event in loaded.events], [1, 2])
            self.assertIn("11:02:03", format_xhs_daily_activity(loaded))

    def test_classifier_finalizer_commits_valid_lines_and_clears_all_captured_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "cache" / "xhs" / "20260813_100000"
            second = root / "cache" / "xhs" / "20260813_110000"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "001.txt").write_text(
                "作品标题：论文原帖\n介绍论文 https://arxiv.org/abs/2608.01234v2",
                encoding="utf-8",
            )
            (second / "001.txt").write_text(
                "作品标题：博客原帖\n博客 https://example.org/posts/one",
                encoding="utf-8",
            )
            (second / "_download_report.json").write_text("{}", encoding="utf-8")
            job = prepare_xhs_classification_job(
                root, now=datetime(2026, 8, 13, 19, 20, 30)
            )
            self.assertIsNotNone(job)
            assert job is not None
            result = {
                "version": 1,
                "items": [
                    {
                        "item_id": "xhs-00001",
                        "status": "success",
                        "targets": [
                            {"type": "arxiv", "value": "arXiv:2608.01234v2"}
                        ],
                    },
                    {
                        "item_id": "xhs-00002",
                        "status": "success",
                        "targets": [
                            {"type": "blog", "value": "https://example.org/posts/one"}
                        ],
                    },
                ],
            }
            job.result_path.write_text(json.dumps(result), encoding="utf-8")
            stats = finalize_xhs_classification_job(
                root,
                job.job_path,
                job.result_path,
                now=datetime(2026, 8, 13, 19, 21, 45),
            )
            self.assertEqual(stats["outputs"], 2)
            self.assertEqual(stats["failures"], 0)
            manual = root / stats["manual"]
            self.assertEqual(manual.name, "20260813_192145.txt")
            content = manual.read_text(encoding="utf-8")
            self.assertIn("2608.01234v2", content)
            self.assertIn("https://example.org/posts/one", content)
            self.assertIn("论文原帖", content)
            self.assertEqual(stats["item_details"][0]["title"], "论文原帖")
            self.assertEqual(list((root / "cache" / "xhs").iterdir()), [])
            self.assertTrue((job.work_dir / "summary.txt").is_file())

    def test_classifier_missing_result_still_writes_audit_and_clears_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache" / "xhs" / "batch"
            cache.mkdir(parents=True)
            (cache / "note.txt").write_text("无法识别", encoding="utf-8")
            job = prepare_xhs_classification_job(root)
            assert job is not None
            stats = finalize_xhs_classification_job(
                root, job.job_path, job.result_path, codex_status=9
            )
            self.assertEqual(stats["outputs"], 0)
            self.assertEqual(stats["failures"], 1)
            self.assertFalse(cache.exists())
            self.assertIn(
                "# Codex 退出码: 9",
                (root / stats["manual"]).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
