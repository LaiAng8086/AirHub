from __future__ import annotations

import json
import unittest

import airhub_text_export as adapter


class XHSTextAdapterTests(unittest.TestCase):
    def test_parses_public_initial_state_without_media_urls(self):
        note = {
            "noteId": "abc123",
            "title": "公开标题",
            "desc": "正文 https://example.org/post",
            "type": "normal",
            "imageList": [{}],
            "time": 1700000000000,
            "lastUpdateTime": 1700000100000,
            "tagList": [{"name": "研究"}],
            "interactInfo": {"likedCount": "8"},
            "user": {"userId": "user1", "nickname": "作者"},
        }
        state = {"noteData": {"data": {"noteData": note}}}
        html = (
            "<html><script>window.__INITIAL_STATE__="
            + json.dumps(state, ensure_ascii=False)
            + "</script></html>"
        )
        result = adapter._extract_note(
            html, "https://www.xiaohongshu.com/explore/abc123"
        )
        self.assertEqual(result["作品ID"], "abc123")
        self.assertEqual(result["作品标题"], "公开标题")
        self.assertEqual(result["作品标签"], "研究")
        content = adapter._text_content(result)
        self.assertIn("作品描述：\n正文 https://example.org/post", content)
        self.assertNotIn("imageList", content)

    def test_rejects_non_xhs_and_profile_only_urls(self):
        self.assertFalse(adapter._supported_note_url("https://example.org/post"))
        self.assertFalse(
            adapter._supported_note_url(
                "https://www.xiaohongshu.com/user/profile/user1"
            )
        )
        self.assertTrue(
            adapter._supported_note_url(
                "https://www.xiaohongshu.com/discovery/item/abc123"
            )
        )


if __name__ == "__main__":
    unittest.main()
