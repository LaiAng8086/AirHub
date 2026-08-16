"""小宇宙登录、订阅更新与公开单集下载。

认证客户端和公开下载器故意使用彼此独立的 Session。公开下载器会清空
Cookie 和认证请求头，并且绝不回退到登录 API，以避免下载行为携带账号状态。
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .models import utc_now_iso
from .paths import PROJECT_ROOT


API_BASE_URL = "https://api.xiaoyuzhoufm.com"
PODCASTER_API_BASE_URL = "https://podcaster-api.xiaoyuzhoufm.com"
PUBLIC_EPISODE_BASE_URL = "https://www.xiaoyuzhoufm.com/episode/"
EPISODE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
NEXT_DATA_PATTERN = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL
)
AUTH_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-jike-access-token",
    "x-jike-refresh-token",
    "x-jike-device-id",
    "x-jike-device-properties",
}
PUBLIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


class XiaoyuzhouError(RuntimeError):
    """小宇宙操作失败，但错误文本不包含认证凭据。"""


@dataclass
class XiaoyuzhouCredentials:
    access_token: str
    refresh_token: str = ""
    uid: str = ""
    nickname: str = ""
    saved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "XiaoyuzhouCredentials":
        return cls(
            access_token=str(data.get("access_token", "")).strip(),
            refresh_token=str(data.get("refresh_token", "")).strip(),
            uid=str(data.get("uid", "")).strip(),
            nickname=str(data.get("nickname", "")).strip(),
            saved_at=str(data.get("saved_at", "")).strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "uid": self.uid,
            "nickname": self.nickname,
            "saved_at": self.saved_at or utc_now_iso(),
        }


@dataclass(frozen=True)
class XiaoyuzhouLoginStatus:
    authenticated: bool
    nickname: str = ""
    uid: str = ""
    reason: str = ""

    @property
    def menu_text(self) -> str:
        if self.authenticated:
            identity = self.nickname or self.uid or "账号"
            return f"[✓] 已登录：{identity}"
        return f"[ ] {self.reason or '未登录，请选择登录功能'}"


@dataclass(frozen=True)
class PodcastEpisode:
    eid: str
    pid: str
    title: str
    podcast_title: str
    author: str
    pub_date: str
    duration: int
    shownotes: str
    description: str
    podcasters: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def article_id(self) -> str:
        return f"xiaoyuzhou-{self.eid}"

    @property
    def date(self) -> str:
        return self.pub_date[:10] if self.pub_date else "日期未知"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PodcastEpisode":
        podcast = data.get("podcast") if isinstance(data.get("podcast"), dict) else {}
        raw_podcasters = podcast.get("podcasters") or data.get("podcasters") or []
        podcasters = tuple(
            str(item.get("nickname", "")).strip()
            for item in raw_podcasters
            if isinstance(item, dict) and str(item.get("nickname", "")).strip()
        )
        author = str(podcast.get("author", "")).strip()
        if not author and podcasters:
            author = " / ".join(podcasters)
        return cls(
            eid=normalize_episode_id(str(data.get("eid", ""))),
            pid=str(data.get("pid") or podcast.get("pid") or "").strip(),
            title=str(data.get("title", "")).strip() or "未命名单集",
            podcast_title=str(podcast.get("title", "")).strip() or "未知播客",
            author=author or "未知主播",
            pub_date=str(data.get("pubDate", "")).strip(),
            duration=_safe_int(data.get("duration")),
            shownotes=str(data.get("shownotes", "")),
            description=str(data.get("description", "")),
            podcasters=podcasters,
            raw=dict(data),
        )

    def to_job_dict(self) -> dict[str, Any]:
        """任务文件只保留展示和转录所需字段，不包含登录态音频 URL。"""

        return {
            "eid": self.eid,
            "pid": self.pid,
            "title": self.title,
            "podcast_title": self.podcast_title,
            "author": self.author,
            "pub_date": self.pub_date,
            "duration": self.duration,
            "shownotes": self.shownotes,
            "description": self.description,
            "podcasters": list(self.podcasters),
        }

    @classmethod
    def from_job_dict(cls, data: dict[str, Any]) -> "PodcastEpisode":
        eid = normalize_episode_id(str(data.get("eid", "")))
        podcasters = tuple(
            str(item).strip() for item in data.get("podcasters", []) if str(item).strip()
        )
        return cls(
            eid=eid,
            pid=str(data.get("pid", "")),
            title=str(data.get("title", "")) or "未命名单集",
            podcast_title=str(data.get("podcast_title", "")) or "未知播客",
            author=str(data.get("author", "")) or "未知主播",
            pub_date=str(data.get("pub_date", "")),
            duration=_safe_int(data.get("duration")),
            shownotes=str(data.get("shownotes", "")),
            description=str(data.get("description", "")),
            podcasters=podcasters,
            raw={},
        )


@dataclass(frozen=True)
class PublicEpisode:
    episode: PodcastEpisode
    audio_url: str
    extension: str
    media_id: str
    raw: dict[str, Any]


def normalize_episode_id(value: str) -> str:
    candidate = value.strip()
    if not EPISODE_ID_PATTERN.fullmatch(candidate):
        raise ValueError("小宇宙 eid 必须是 24 位十六进制字符串")
    return candidate.lower()


def credentials_path(root: Path = PROJECT_ROOT) -> Path:
    return root / "config" / "xiaoyuzhou_credentials.json"


def load_credentials(root: Path = PROJECT_ROOT) -> XiaoyuzhouCredentials | None:
    path = credentials_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    credentials = XiaoyuzhouCredentials.from_dict(payload)
    return credentials if credentials.access_token else None


def save_credentials(
    credentials: XiaoyuzhouCredentials,
    root: Path = PROJECT_ROOT,
) -> Path:
    if not credentials.access_token:
        raise ValueError("不能保存空的小宇宙 access token")
    path = credentials_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = credentials.to_dict()
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return path


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _response_message(response: requests.Response, action: str) -> str:
    message = ""
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        message = str(
            payload.get("toast")
            or payload.get("message")
            or payload.get("msg")
            or ""
        ).strip()
    suffix = f"：{message[:160]}" if message else ""
    return f"{action}失败（HTTP {response.status_code}）{suffix}"


class XiaoyuzhouAuthClient:
    """仅用于登录、状态校验和订阅更新的认证客户端。"""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.root = root.resolve()
        self.session = session or requests.Session()
        self.timeout = timeout

    @staticmethod
    def _app_headers(access_token: str = "", refresh_token: str = "") -> dict[str, str]:
        local_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
        headers = {
            "Host": "api.xiaoyuzhoufm.com",
            "User-Agent": "Xiaoyuzhou/2.57.1 (build:1576; iOS 17.4.1)",
            "Market": "AppStore",
            "App-BuildNo": "1576",
            "OS": "ios",
            "Manufacturer": "Apple",
            "BundleID": "app.podcast.cosmos",
            "Accept-Language": "zh-Hans-CN;q=1.0, zh-Hant-TW;q=0.9",
            "Model": "iPhone14,2",
            "app-permissions": "4",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "App-Version": "2.57.1",
            "WifiConnected": "true",
            "OS-Version": "17.4.1",
            "Local-Time": local_time,
            "Timezone": "Asia/Shanghai",
            # inbox/list 当前会校验这两个头是否“存在”，即使设备值为空；
            # 省略它们会返回 HTTP 400 rpc_error。本地 Go 参考实现同样显式发送空值。
            "x-jike-device-id": "",
            "x-jike-device-properties": "",
            "x-custom-xiaoyuzhou-app-dev": "",
        }
        if access_token:
            headers["x-jike-access-token"] = access_token
        if refresh_token:
            headers["x-jike-refresh-token"] = refresh_token
        return headers

    @staticmethod
    def _podcaster_headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://podcaster.xiaoyuzhoufm.com",
            "Referer": "https://podcaster.xiaoyuzhoufm.com/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/126.0 Safari/537.36"
            ),
        }

    def send_sms_code(self, mobile_phone: str, area_code: str = "+86") -> None:
        phone = mobile_phone.strip()
        area = area_code.strip() or "+86"
        if not re.fullmatch(r"\d{5,20}", phone):
            raise ValueError("手机号只能包含 5–20 位数字")
        if not re.fullmatch(r"\+\d{1,4}", area):
            raise ValueError("区号格式应类似 +86")
        try:
            response = self.session.post(
                f"{PODCASTER_API_BASE_URL}/v1/auth/send-code",
                json={"mobilePhoneNumber": phone, "areaCode": area},
                headers=self._podcaster_headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise XiaoyuzhouError(f"发送验证码网络失败：{exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise XiaoyuzhouError(_response_message(response, "发送验证码"))

    def login_with_sms(
        self,
        mobile_phone: str,
        verify_code: str,
        area_code: str = "+86",
    ) -> XiaoyuzhouLoginStatus:
        phone = mobile_phone.strip()
        code = verify_code.strip()
        area = area_code.strip() or "+86"
        if not re.fullmatch(r"\d{4,8}", code):
            raise ValueError("短信验证码应为 4–8 位数字")
        try:
            response = self.session.post(
                f"{PODCASTER_API_BASE_URL}/v1/auth/login-with-sms",
                json={
                    "mobilePhoneNumber": phone,
                    "verifyCode": code,
                    "areaCode": area,
                },
                headers=self._podcaster_headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise XiaoyuzhouError(f"小宇宙登录网络失败：{exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise XiaoyuzhouError(_response_message(response, "小宇宙登录"))

        access_token = str(response.headers.get("x-jike-access-token", "")).strip()
        refresh_token = str(response.headers.get("x-jike-refresh-token", "")).strip()
        if not access_token:
            raise XiaoyuzhouError("小宇宙登录响应没有 access token")
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            payload = {}
        container = payload.get("data", {}) if isinstance(payload, dict) else {}
        user = container.get("user") or container.get("data") or {}
        if not isinstance(user, dict):
            user = {}
        credentials = XiaoyuzhouCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            uid=str(user.get("uid", "")),
            nickname=str(user.get("nickname", "")),
            saved_at=utc_now_iso(),
        )
        save_credentials(credentials, self.root)
        return XiaoyuzhouLoginStatus(
            True,
            nickname=credentials.nickname,
            uid=credentials.uid,
            reason="登录有效",
        )

    def _refresh_credentials(
        self,
        credentials: XiaoyuzhouCredentials,
    ) -> XiaoyuzhouCredentials | None:
        if not credentials.refresh_token:
            return None
        headers = self._app_headers(
            credentials.access_token,
            credentials.refresh_token,
        )
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        try:
            response = self.session.post(
                f"{API_BASE_URL}/app_auth_tokens.refresh",
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            return None
        access_token = str(response.headers.get("x-jike-access-token", "")).strip()
        refresh_token = str(response.headers.get("x-jike-refresh-token", "")).strip()
        if not access_token:
            try:
                payload = response.json()
            except (ValueError, requests.JSONDecodeError):
                payload = {}
            container = payload.get("data", {}) if isinstance(payload, dict) else {}
            access_token = str(container.get("x-jike-access-token", "")).strip()
            refresh_token = str(container.get("x-jike-refresh-token", "")).strip()
        if not access_token:
            return None
        refreshed = XiaoyuzhouCredentials(
            access_token=access_token,
            refresh_token=refresh_token or credentials.refresh_token,
            uid=credentials.uid,
            nickname=credentials.nickname,
            saved_at=utc_now_iso(),
        )
        save_credentials(refreshed, self.root)
        return refreshed

    def _profile_response(
        self,
        credentials: XiaoyuzhouCredentials,
    ) -> requests.Response:
        return self.session.get(
            f"{API_BASE_URL}/v1/profile/get",
            headers=self._app_headers(credentials.access_token),
            timeout=self.timeout,
        )

    def check_login_status(self, *, refresh: bool = True) -> XiaoyuzhouLoginStatus:
        credentials = load_credentials(self.root)
        if credentials is None:
            return XiaoyuzhouLoginStatus(False, reason="未登录，请选择 12 登录")
        try:
            response = self._profile_response(credentials)
        except requests.RequestException:
            return XiaoyuzhouLoginStatus(False, reason="登录状态暂时无法联网校验")
        if response.status_code == 401 and refresh:
            refreshed = self._refresh_credentials(credentials)
            if refreshed is not None:
                credentials = refreshed
                try:
                    response = self._profile_response(credentials)
                except requests.RequestException:
                    return XiaoyuzhouLoginStatus(False, reason="登录状态暂时无法联网校验")
        if response.status_code < 200 or response.status_code >= 300:
            return XiaoyuzhouLoginStatus(False, reason="登录已失效，请选择 12 重新登录")
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            payload = {}
        user = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(user, dict) and isinstance(user.get("data"), dict):
            user = user["data"]
        nickname = str(user.get("nickname", "")) if isinstance(user, dict) else ""
        uid = str(user.get("uid", "")) if isinstance(user, dict) else ""
        if nickname != credentials.nickname or uid != credentials.uid:
            credentials.nickname = nickname or credentials.nickname
            credentials.uid = uid or credentials.uid
            credentials.saved_at = utc_now_iso()
            save_credentials(credentials, self.root)
        return XiaoyuzhouLoginStatus(
            True,
            nickname=nickname or credentials.nickname,
            uid=uid or credentials.uid,
            reason="登录有效",
        )

    def list_subscription_updates(self, *, max_items: int = 100) -> list[PodcastEpisode]:
        credentials = load_credentials(self.root)
        if credentials is None:
            raise XiaoyuzhouError("尚未登录小宇宙，请先执行登录")
        status = self.check_login_status(refresh=True)
        if not status.authenticated:
            raise XiaoyuzhouError(status.reason or "小宇宙登录已失效")
        credentials = load_credentials(self.root)
        if credentials is None:
            raise XiaoyuzhouError("刷新后未找到小宇宙凭据")

        episodes: list[PodcastEpisode] = []
        seen: set[str] = set()
        load_more_key: dict[str, Any] | None = None
        while len(episodes) < max_items:
            payload: dict[str, Any] = {"limit": min(20, max_items - len(episodes))}
            if load_more_key:
                payload["loadMoreKey"] = load_more_key
            try:
                response = self.session.post(
                    f"{API_BASE_URL}/v1/inbox/list",
                    json=payload,
                    headers=self._app_headers(credentials.access_token),
                    timeout=max(self.timeout, 20.0),
                )
            except requests.RequestException as exc:
                raise XiaoyuzhouError(f"获取订阅更新网络失败：{exc}") from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise XiaoyuzhouError(_response_message(response, "获取订阅更新"))
            try:
                response_payload = response.json()
            except (ValueError, requests.JSONDecodeError) as exc:
                raise XiaoyuzhouError("订阅更新接口没有返回有效 JSON") from exc
            container = response_payload.get("data", {})
            # 当前线上格式：{"data": [...], "loadMoreKey": {...}}。
            # 旧代理/文档格式：{"data": {"data": [...], "loadMoreKey": {...}}}。
            # 同时兼容两者，避免接口包装层变化导致列表不可用。
            if isinstance(container, list):
                raw_items = container
                next_key = response_payload.get("loadMoreKey")
            elif isinstance(container, dict):
                raw_items = container.get("data") or container.get("items") or []
                next_key = container.get("loadMoreKey") or response_payload.get(
                    "loadMoreKey"
                )
            else:
                raise XiaoyuzhouError("订阅更新接口数据结构无效")
            if not isinstance(raw_items, list):
                raise XiaoyuzhouError("订阅更新列表数据结构无效")
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                try:
                    episode = PodcastEpisode.from_api(raw_item)
                except ValueError:
                    continue
                if episode.eid in seen:
                    continue
                seen.add(episode.eid)
                episodes.append(episode)
                if len(episodes) >= max_items:
                    break
            if not raw_items or not isinstance(next_key, dict) or next_key == load_more_key:
                break
            load_more_key = next_key
            time.sleep(0.25)
        return episodes


class PublicEpisodeDownloader:
    """不携带任何登录状态的公开网页解析与音频下载器。"""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (20.0, 300.0),
        retries: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.clear()
        self.session.headers.update(PUBLIC_HEADERS)
        self.session.auth = None
        self.session.cookies.clear()
        self.timeout = timeout
        self.retries = max(1, retries)

    @staticmethod
    def _public_request_headers(*, audio: bool = False, resume_at: int = 0) -> dict[str, str]:
        headers = dict(PUBLIC_HEADERS)
        if audio:
            headers["Accept"] = "audio/*,application/octet-stream;q=0.9,*/*;q=0.5"
        if resume_at > 0:
            headers["Range"] = f"bytes={resume_at}-"
        for name in list(headers):
            if name.lower() in AUTH_HEADER_NAMES:
                headers.pop(name, None)
        return headers

    def fetch_public_episode(self, eid: str) -> PublicEpisode:
        episode_id = normalize_episode_id(eid)
        try:
            response = self.session.get(
                f"{PUBLIC_EPISODE_BASE_URL}{episode_id}",
                headers=self._public_request_headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise XiaoyuzhouError(f"公开单集页面访问失败：{exc}") from exc
        if response.status_code != 200:
            raise XiaoyuzhouError(
                f"公开单集页面访问失败（HTTP {response.status_code}）"
            )
        match = NEXT_DATA_PATTERN.search(response.text)
        if not match:
            raise XiaoyuzhouError("公开单集页面没有 __NEXT_DATA__")
        raw_next_data = match.group(1)
        try:
            # __NEXT_DATA__ 本身是 JSON；先直接解析，避免 shownotes 中的 &quot;
            # 被 HTML 反转义为裸引号后破坏 JSON 字符串。
            next_data = json.loads(raw_next_data)
        except json.JSONDecodeError:
            try:
                next_data = json.loads(html_module.unescape(raw_next_data))
            except json.JSONDecodeError as exc:
                raise XiaoyuzhouError("公开单集页面的 JSON 无法解析") from exc
        raw_episode = (
            next_data.get("props", {}).get("pageProps", {}).get("episode", {})
            if isinstance(next_data, dict)
            else {}
        )
        if not isinstance(raw_episode, dict) or not raw_episode:
            raise XiaoyuzhouError("公开单集页面没有 episode 数据")
        public = _public_episode_from_payload(raw_episode)
        if public.episode.eid != episode_id:
            raise XiaoyuzhouError("公开单集页面返回了不匹配的 eid")
        return public

    def download_audio(self, public: PublicEpisode, target_dir: Path) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{public.episode.article_id}{public.extension}"
        if target.is_file() and target.stat().st_size > 0:
            return target
        partial = target.with_suffix(target.suffix + ".part")
        for attempt in range(1, self.retries + 1):
            resume_at = partial.stat().st_size if partial.exists() else 0
            try:
                response = self.session.get(
                    public.audio_url,
                    headers=self._public_request_headers(audio=True, resume_at=resume_at),
                    stream=True,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise XiaoyuzhouError("公开音频地址返回的不是音频内容")
                append = resume_at > 0 and response.status_code == 206
                mode = "ab" if append else "wb"
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                if partial.stat().st_size <= 0:
                    raise XiaoyuzhouError("公开音频下载结果为空")
                partial.replace(target)
                return target
            except (OSError, requests.RequestException, XiaoyuzhouError) as exc:
                if attempt >= self.retries:
                    raise XiaoyuzhouError(
                        f"公开音频下载失败，已尝试 {self.retries} 次：{exc}"
                    ) from exc
                time.sleep(min(8, 2**attempt))
        raise XiaoyuzhouError("公开音频下载失败")


def _public_episode_from_payload(data: dict[str, Any]) -> PublicEpisode:
    if bool(data.get("isPrivateMedia")):
        raise XiaoyuzhouError("该单集是私有/付费媒体，拒绝使用登录态下载")
    media = data.get("media") if isinstance(data.get("media"), dict) else {}
    source = media.get("source") if isinstance(media.get("source"), dict) else {}
    source_mode = str(source.get("mode", "")).upper()
    enclosure = data.get("enclosure") if isinstance(data.get("enclosure"), dict) else {}
    audio_url = str(enclosure.get("url") or source.get("url") or "").strip()
    if source_mode and source_mode != "PUBLIC":
        raise XiaoyuzhouError("公开页面音频不是 PUBLIC 模式，拒绝下载")
    if not audio_url:
        raise XiaoyuzhouError("公开页面没有可下载的音频链接")
    parsed = urlparse(audio_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "xyzcdn.net" or hostname.endswith(".xyzcdn.net")
    ):
        raise XiaoyuzhouError("公开页面返回了不受信任的音频地址")
    if hostname.startswith("private-media.") or hostname.startswith("pmedia."):
        raise XiaoyuzhouError("公开页面返回了私有媒体域名，拒绝下载")
    path = parsed.path.lower()
    extension = next(
        (item for item in (".m4a", ".mp3", ".wav", ".aac", ".ogg") if path.endswith(item)),
        ".m4a",
    )
    episode = PodcastEpisode.from_api(data)
    return PublicEpisode(
        episode=episode,
        audio_url=audio_url,
        extension=extension,
        media_id=str(media.get("id", "")),
        raw=dict(data),
    )


def podcast_download_state(root: Path, episode: PodcastEpisode) -> str:
    """返回未下载、音频已下载或已完成，供订阅更新列表显示。"""

    article_id = episode.article_id
    finished = root / "finished" / f"{article_id}.json"
    if finished.is_file():
        try:
            payload = json.loads(finished.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("status", {}).get("processed"):
            return "已完成"
    for path in (root / "attachments" / "audio").glob(
        f"*/{article_id}.*"
    ):
        if path.is_file() and not path.name.endswith(".part") and path.stat().st_size > 0:
            return "音频已下载"
    return "未下载"
