from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airhub.podcast_transcript import (
    WHISPER_MODEL,
    WHISPER_UPSTREAM_MODEL,
    TranscriptResult,
    TranscriptSegment,
    render_dialogue_html,
)
from airhub.podcast_worker import create_podcast_job, process_job
from airhub.xiaoyuzhou import (
    PodcastEpisode,
    PublicEpisode,
    PublicEpisodeDownloader,
    XiaoyuzhouAuthClient,
    XiaoyuzhouCredentials,
    podcast_download_state,
    save_credentials,
)
from airhub.xiaoyuzhou import XiaoyuzhouError


EID = "0123456789abcdef01234567"
EID_TWO = "abcdef0123456789abcdef01"


def episode(eid: str = EID, title: str = "一期节目") -> PodcastEpisode:
    return PodcastEpisode(
        eid=eid,
        pid="podcast-id",
        title=title,
        podcast_title="测试播客",
        author="主持人甲 / 嘉宾乙",
        pub_date="2026-08-13T08:00:00.000Z",
        duration=120,
        shownotes="主播：主持人甲\n嘉宾：嘉宾乙",
        description="节目简介",
        podcasters=("主持人甲", "嘉宾乙"),
        raw={},
    )


class FakeCookies:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict | None = None,
        headers: dict | None = None,
        text: str = "",
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text
        self._chunks = chunks

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self._chunks


class PublicSession:
    def __init__(self, page: str) -> None:
        self.headers = {"Authorization": "secret", "Cookie": "bad=1"}
        self.cookies = FakeCookies()
        self.auth = ("user", "password")
        self.page = page
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if "xiaoyuzhoufm.com/episode" in url:
            return FakeResponse(text=self.page)
        return FakeResponse(headers={"Content-Type": "audio/mpeg"}, chunks=(b"audio",))


class XiaoyuzhouIntegrationTest(unittest.TestCase):
    def test_credentials_are_private_and_startup_status_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = save_credentials(
                XiaoyuzhouCredentials("access-secret", "refresh-secret"), root
            )
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

            class Session:
                def get(self, url, **kwargs):
                    self.url = url
                    self.kwargs = kwargs
                    return FakeResponse(payload={"data": {"uid": "u1", "nickname": "小明"}})

            session = Session()
            status = XiaoyuzhouAuthClient(root, session=session).check_login_status()
            self.assertTrue(status.authenticated)
            self.assertEqual(status.nickname, "小明")
            self.assertEqual(
                session.kwargs["headers"]["x-jike-access-token"], "access-secret"
            )

    def test_authenticated_headers_keep_required_empty_device_fields(self):
        headers = XiaoyuzhouAuthClient._app_headers("access-secret")
        self.assertIn("x-jike-device-id", headers)
        self.assertIn("x-jike-device-properties", headers)
        self.assertEqual(headers["x-jike-device-id"], "")
        self.assertEqual(headers["x-jike-device-properties"], "")

    def test_subscription_updates_accept_current_top_level_list_and_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_credentials(XiaoyuzhouCredentials("access-secret"), root)

            class Session:
                def __init__(self):
                    self.posts = []

                def get(self, url, **kwargs):
                    return FakeResponse(payload={"data": {"uid": "u", "nickname": "n"}})

                def post(self, url, **kwargs):
                    self.posts.append(kwargs)
                    if len(self.posts) == 1:
                        return FakeResponse(
                            payload={
                                "data": [episode(EID).raw or {
                                    "eid": EID,
                                    "title": "第一页",
                                    "pubDate": "2026-08-13T00:00:00Z",
                                    "podcast": {"title": "播客", "author": "主播"},
                                }],
                                "loadMoreKey": {"pubDate": "date", "id": EID},
                            }
                        )
                    return FakeResponse(
                        payload={
                            "data": [{
                                "eid": EID_TWO,
                                "title": "第二页",
                                "pubDate": "2026-08-12T00:00:00Z",
                                "podcast": {"title": "播客", "author": "主播"},
                            }]
                        }
                    )

            session = Session()
            updates = XiaoyuzhouAuthClient(root, session=session).list_subscription_updates(
                max_items=40
            )
            self.assertEqual([item.eid for item in updates], [EID, EID_TWO])
            self.assertEqual(
                session.posts[1]["json"]["loadMoreKey"],
                {"pubDate": "date", "id": EID},
            )
            for call in session.posts:
                self.assertIn("x-jike-device-id", call["headers"])
                self.assertIn("x-jike-device-properties", call["headers"])

    def test_public_download_session_has_no_login_state_or_auth_headers(self):
        payload = {
            "props": {
                "pageProps": {
                    "episode": {
                        "eid": EID,
                        "title": "公开节目",
                        "pubDate": "2026-08-13T00:00:00Z",
                        "podcast": {
                            "pid": "podcast-id",
                            "title": "公开播客",
                            "author": "主持人",
                        },
                        "media": {"id": "media-id", "source": {"mode": "PUBLIC"}},
                        "enclosure": {"url": "https://media.xyzcdn.net/audio/test.mp3"},
                    }
                }
            }
        }
        page = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
        session = PublicSession(page)
        downloader = PublicEpisodeDownloader(session=session, retries=1)
        self.assertIsNone(session.auth)
        self.assertTrue(session.cookies.cleared)
        self.assertNotIn("Authorization", session.headers)

        public = downloader.fetch_public_episode(EID)
        with tempfile.TemporaryDirectory() as tmp:
            audio = downloader.download_audio(public, Path(tmp))
            self.assertEqual(audio.read_bytes(), b"audio")
        for _, kwargs in session.calls:
            lower_headers = {name.lower() for name in kwargs["headers"]}
            self.assertNotIn("authorization", lower_headers)
            self.assertNotIn("cookie", lower_headers)
            self.assertNotIn("x-jike-access-token", lower_headers)

    def test_public_page_keeps_html_entities_and_rejects_private_media_host(self):
        payload = {
            "props": {
                "pageProps": {
                    "episode": {
                        "eid": EID,
                        "title": "含 &quot; 引号的节目",
                        "podcast": {"title": "播客", "author": "主播"},
                        "media": {"source": {"mode": "PUBLIC"}},
                        "enclosure": {
                            "url": "https://private-media.xyzcdn.net/audio/test.mp3"
                        },
                    }
                }
            }
        }
        page = '<script id="__NEXT_DATA__">' + json.dumps(payload) + "</script>"
        with self.assertRaisesRegex(XiaoyuzhouError, "私有媒体域名"):
            PublicEpisodeDownloader(session=PublicSession(page)).fetch_public_episode(EID)

    def test_worker_writes_dialogue_html_and_standard_article_state(self):
        listed = episode()
        public = PublicEpisode(
            episode=listed,
            audio_url="https://media.xyzcdn.net/audio/test.mp3",
            extension=".mp3",
            media_id="media-id",
            raw={},
        )

        class Downloader:
            def fetch_public_episode(self, eid):
                self.eid = eid
                return public

            def download_audio(self, public_episode, target_dir):
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{public_episode.episode.article_id}.mp3"
                target.write_bytes(b"fake audio")
                return target

        transcript = TranscriptResult(
            language="zh",
            language_probability=0.99,
            duration=10.0,
            gpu_index=0,
            compute_type="float16",
            speaker_method="test-speakers",
            segments=(
                TranscriptSegment(0, 4, "你好，欢迎收听。", "主持人甲"),
                TranscriptSegment(4, 9, "谢谢邀请。", "嘉宾乙"),
            ),
        )

        def fake_transcribe(audio_path, selected, root, *, output):
            self.assertEqual(selected.eid, EID)
            self.assertTrue(audio_path.is_file())
            output("[DONE] fake turbo")
            return transcript

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = create_podcast_job(root, [listed])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["whisper_model"], "turbo")
            self.assertFalse(job["slurm_allowed"])
            self.assertTrue(job["public_download_only"])
            self.assertNotIn("access", json.dumps(job).lower())

            result = process_job(
                root,
                downloader=Downloader(),
                transcribe_fn=fake_transcribe,
                output=lambda _: None,
            )
            self.assertEqual(result["failed"], 0)
            article_path = root / "finished" / f"xiaoyuzhou-{EID}.json"
            article = json.loads(article_path.read_text(encoding="utf-8"))
            self.assertTrue(article["status"]["processed"])
            self.assertEqual(article["metadata"]["transcription"]["model"], WHISPER_UPSTREAM_MODEL)
            self.assertFalse(article["metadata"]["download"]["authenticated"])
            html = (root / article["html"]).read_text(encoding="utf-8")
            self.assertIn('class="speaker">主持人甲：</span>', html)
            self.assertIn('class="content">你好，欢迎收听。</span>', html)
            self.assertEqual(podcast_download_state(root, listed), "已完成")
            summary = (root / result["summary"]).read_text(encoding="utf-8")
            self.assertIn("本机 NVIDIA GPU（禁止 Slurm）", summary)

    def test_html_declares_turbo_and_name_content_dialogue(self):
        result = TranscriptResult(
            language="zh",
            language_probability=1,
            duration=4,
            gpu_index=0,
            compute_type="float16",
            speaker_method="test",
            segments=(TranscriptSegment(0, 4, "内容", "人名"),),
        )
        rendered = render_dialogue_html(episode(), result)
        self.assertEqual(WHISPER_MODEL, "turbo")
        self.assertIn("openai/whisper-large-v3-turbo", rendered)
        self.assertIn('class="speaker">人名：</span><span class="content">内容</span>', rendered)


if __name__ == "__main__":
    unittest.main()
