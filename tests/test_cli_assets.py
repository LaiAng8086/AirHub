from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from airhub.cli import _format_strategy_result, _menu, execute_action, run_menu
from airhub.config import load_settings
from airhub.html.assets import embed_local_images
from airhub.models import Article
from airhub.xiaoyuzhou import XiaoyuzhouLoginStatus
from airhub.xiaoyuzhou import PodcastEpisode
from airhub.xhs import XHSTextDownloadResult


class CliAndAssetsTest(unittest.TestCase):
    def test_paper_digest_action_lists_candidates_and_runs_selected_article_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output: list[str] = []
            inbox = root / "inbox"
            inbox.mkdir()
            for article_id, rank in (("rank-one", 1), ("rank-two", 2)):
                article = Article(
                    id=article_id,
                    type="paper",
                    source="test",
                    title=f"Title {rank}",
                    metadata={
                        "download_date": "2026-08-13",
                        "priority": {"rank": rank},
                    },
                )
                (inbox / f"{article_id}.json").write_text(
                    json.dumps(article.to_dict()), encoding="utf-8"
                )
            with patch("airhub.cli.subprocess.run") as run:
                run.return_value.returncode = 0
                execute_action(
                    "paper-digest",
                    root,
                    input_fn=lambda _: "2",
                    output=output.append,
                )
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual(
                args[0],
                ["bash", "run/run_paper_digest_codex.sh", "inbox/rank-two.json"],
            )
            self.assertEqual(kwargs["cwd"], root)
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["env"]["CODEX_PAPER_DIGEST_RETRIES"], "1")
            rendered = "\n".join(output)
            self.assertIn("1. 添加日期 2026-08-13 | rank-one | 优选排名 1", rendered)
            self.assertIn("2. 添加日期 2026-08-13 | rank-two | 优选排名 2", rendered)
            self.assertIn("已选择：rank-two", rendered)
            self.assertIn("成功 1，失败并跳过 0", rendered)

    def test_paper_digest_batch_skips_failures_and_continues_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output: list[str] = []
            inbox = root / "inbox"
            inbox.mkdir()
            for index in range(1, 4):
                article = Article(
                    id=f"paper-{index}",
                    type="paper",
                    source="test",
                    title=f"Paper {index}",
                    metadata={
                        "download_date": "2026-08-13",
                        "priority": {"rank": index},
                    },
                )
                (inbox / f"paper-{index}.json").write_text(
                    json.dumps(article.to_dict()), encoding="utf-8"
                )
            results = [
                type("Result", (), {"returncode": returncode})()
                for returncode in (7, 0, 9)
            ]
            with patch("airhub.cli.subprocess.run", side_effect=results) as run:
                execute_action(
                    "paper-digest",
                    root,
                    input_fn=lambda _: "1 2 3",
                    output=output.append,
                )

            self.assertEqual(run.call_count, 3)
            self.assertEqual(
                [item.args[0][-1] for item in run.call_args_list],
                ["inbox/paper-1.json", "inbox/paper-2.json", "inbox/paper-3.json"],
            )
            self.assertTrue(
                all(
                    item.kwargs["env"]["CODEX_PAPER_DIGEST_RETRIES"] == "1"
                    for item in run.call_args_list
                )
            )
            rendered = "\n".join(output)
            self.assertIn("inbox/paper-1.json 调用失败（退出码 7）；已跳过", rendered)
            self.assertIn("[DONE] inbox/paper-2.json 解读完成", rendered)
            self.assertIn("inbox/paper-3.json 调用失败（退出码 9）；已跳过", rendered)
            self.assertIn("选择 3，成功 1，失败并跳过 2", rendered)

    def test_paper_digest_action_does_not_run_when_no_candidates_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output: list[str] = []
            with patch("airhub.cli.subprocess.run") as run:
                execute_action("paper-digest", root, output=output.append)
            run.assert_not_called()
            self.assertIn("当前没有待解读文章。", output)

    def test_priority_result_explains_ranked_articles_beyond_daily_limit(self):
        rendered = _format_strategy_result(
            {
                "candidates": 300,
                "existing": 6,
                "eligible": 294,
                "selected": 20,
                "stored": 20,
                "not_selected": 274,
                "errors": 0,
            },
            "priority",
        )
        self.assertIn("参与优选排名 294", rendered)
        self.assertIn("进入每日上限 20", rendered)
        self.assertIn("排名后未进入上限 274", rendered)
        self.assertIn("不表示通过了额外的硬筛选", rendered)

    def test_menu_updates_daily_limit_with_numeric_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "scope").mkdir()
            (root / "scope" / "library.csv").write_text("Author\n", encoding="utf-8")
            values = iter(["1", "31", "0"])
            output: list[str] = []
            run_menu(root, input_fn=lambda _: next(values), output=output.append)
            self.assertEqual(load_settings(root).daily_article_limit, 31)
            self.assertGreaterEqual(output.count("\033[2J\033[H"), 2)

    def test_information_action_waits_before_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            values = iter(["7", "return", "0"])
            prompts: list[str] = []
            output: list[str] = []

            def fake_input(prompt: str) -> str:
                prompts.append(prompt)
                return next(values)

            run_menu(root, input_fn=fake_input, output=output.append)
            self.assertTrue(any("按任意键" in prompt for prompt in prompts))
            self.assertGreaterEqual(output.count("\033[2J\033[H"), 2)

    def test_wide_dos_menu_groups_features_and_shows_login_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("airhub.cli.shutil.get_terminal_size") as terminal:
                terminal.return_value.columns = 160
                rendered = _menu(
                    root,
                    XiaoyuzhouLoginStatus(True, nickname="测试用户"),
                )
            self.assertIn("AIRHUB / DAILY INTELLIGENCE CONSOLE", rendered)
            self.assertIn("论文采集", rendered)
            self.assertIn("解读与队列", rendered)
            self.assertIn("播客与系统", rendered)
            self.assertIn("[✓] 已登录：测试用户", rendered)
            panel_line = next(line for line in rendered.splitlines() if "论文采集" in line)
            self.assertIn("解读与队列", panel_line)
            self.assertIn("播客与系统", panel_line)
            self.assertIn("XHS 与 Blog", rendered)

    def test_xhs_download_uses_multiline_textarea_confirms_count_and_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "cache" / "xhs" / "20260813_190000"
            log = root / "Logs" / "xhs.log"
            values = iter(
                [
                    "说明 https://www.xiaohongshu.com/explore/one?xsec=1",
                    "https://xhslink.com/short https://xhslink.com/short",
                    "",
                    "y",
                ]
            )
            output: list[str] = []
            result = XHSTextDownloadResult(
                session,
                2,
                2,
                0,
                log,
                daily_sequence=3,
                completed_at="2026-08-13T19:00:01+08:00",
            )
            with patch("airhub.cli.download_xhs_texts", return_value=result) as download:
                execute_action(
                    "xhs-download",
                    root,
                    input_fn=lambda _: next(values),
                    output=output.append,
                )
            download.assert_called_once()
            self.assertEqual(len(download.call_args.args[1]), 2)
            rendered = "\n".join(output)
            self.assertIn("识别到 2 个", rendered)
            self.assertIn("未下载图片/视频", rendered)
            self.assertIn("第 #3 次", rendered)
            self.assertEqual(download.call_args.kwargs["source_text"].splitlines()[0][:2], "说明")

    def test_blog_delete_accepts_space_separated_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from airhub.blog_archive import BlogSiteStore

            store = BlogSiteStore(root)
            store.add("https://one.example/post")
            store.add("https://two.example/post")
            store.add("https://three.example/post")
            output: list[str] = []
            execute_action(
                "blog-delete",
                root,
                input_fn=lambda _: "3 1 3",
                output=output.append,
            )
            self.assertEqual(
                [site.origin for site in store.list()], ["https://two.example"]
            )
            self.assertIn("删除 2 个", "\n".join(output))

    def test_priority_feedback_selects_completed_papers_by_space_separated_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = [
                SimpleNamespace(
                    article_id=f"paper-{index}",
                    title=f"Paper {index}",
                    authors=("Alice",),
                    institutions=("example university",),
                    already_added=False,
                )
                for index in range(1, 3)
            ]
            output: list[str] = []
            with (
                patch("airhub.cli.PriorityFeedbackStore") as store_class,
                patch("airhub.cli.update_priority_strategy") as update,
            ):
                store = store_class.return_value
                store.list_completed_papers.return_value = candidates
                store.add_articles.return_value = {
                    "requested": 2,
                    "added": 2,
                    "skipped": 0,
                    "article_ids": ["paper-2", "paper-1"],
                    "author_increments": 2,
                    "institution_increments": 2,
                }
                update.return_value.author_counts = {"alice": 2}
                update.return_value.institution_counts = {"example university": 2}
                execute_action(
                    "priority-feedback",
                    root,
                    input_fn=lambda _: "2 1 2",
                    output=output.append,
                )
            store.add_articles.assert_called_once_with(["paper-2", "paper-1"])
            update.assert_called_once_with(root)
            self.assertIn("作者权重 +2", "\n".join(output))

    def test_podcast_action_lists_state_and_accepts_space_separated_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "data" / "podcast" / "jobs" / "current.json"
            job_path.parent.mkdir(parents=True)
            candidates = [
                PodcastEpisode(
                    eid=f"{index:024x}",
                    pid="podcast",
                    title=f"单集 {index}",
                    podcast_title="测试播客",
                    author=f"博主 {index}",
                    pub_date="2026-08-13T00:00:00Z",
                    duration=60,
                    shownotes="",
                    description="",
                    podcasters=(),
                    raw={},
                )
                for index in range(1, 4)
            ]
            output: list[str] = []
            with (
                patch("airhub.cli.XiaoyuzhouAuthClient") as auth_client,
                patch("airhub.cli.create_podcast_job", return_value=job_path) as create,
                patch("airhub.cli.subprocess.run") as run,
            ):
                auth_client.return_value.check_login_status.return_value = (
                    XiaoyuzhouLoginStatus(True, nickname="用户")
                )
                auth_client.return_value.list_subscription_updates.return_value = candidates
                run.return_value.returncode = 0
                execute_action(
                    "xiaoyuzhou-podcast",
                    root,
                    input_fn=lambda _: "3 1 3",
                    output=output.append,
                )
            create.assert_called_once_with(root, [candidates[2], candidates[0]])
            run.assert_called_once_with(
                ["bash", "run/run_xiaoyuzhou_podcast.sh"], cwd=root, check=False
            )
            rendered = "\n".join(output)
            self.assertIn("日期", rendered)
            self.assertIn("博主", rendered)
            self.assertIn("播客", rendered)
            self.assertIn("下载状态", rendered)
            self.assertIn("已选择：2026-08-13 | 博主 3", rendered)

    def test_defer_pending_action_accepts_space_separated_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            for article_id in ("one", "two"):
                article = Article(
                    id=article_id,
                    type="paper",
                    source="test",
                    title=article_id,
                    metadata={"download_date": "2026-08-13"},
                )
                (inbox / f"{article_id}.json").write_text(
                    json.dumps(article.to_dict()), encoding="utf-8"
                )
            output: list[str] = []
            with patch("airhub.cli.defer_pending_articles") as defer:
                defer.return_value = {
                    "deferred": 2,
                    "article_ids": ["two", "one"],
                    "cache_paths": [],
                }
                execute_action(
                    "defer-pending",
                    root,
                    input_fn=lambda _: "2 1 2",
                    output=output.append,
                )
            defer.assert_called_once_with(root, ["two", "one"])
            self.assertIn("已将 2 篇文章", "\n".join(output))

    def test_project_relative_svg_is_embedded_as_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "attachments" / "image" / "2026-08-11" / "figure.svg"
            image.parent.mkdir(parents=True)
            image.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>',
                encoding="utf-8",
            )
            html = root / "attachments" / "html" / "digest.html"
            html.parent.mkdir(parents=True)
            html.write_text(
                '<html><img src="attachments/image/2026-08-11/figure.svg"></html>',
                encoding="utf-8",
            )
            stats = embed_local_images(html, base_dir=root)
            self.assertEqual(stats["embedded"], 1)
            self.assertEqual(stats["missing"], 0)
            self.assertIn('src="data:image/svg+xml;base64,', html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
