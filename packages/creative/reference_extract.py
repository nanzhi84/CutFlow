from __future__ import annotations

import asyncio
import html
import inspect
import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.core import contracts as c
from packages.core.storage.object_store import ObjectStore, ObjectRef
from packages.core.storage.secret_store import SecretStore
from packages.creative.reference_cookies import stored_cookie_header

AsrInvoke = Callable[[str, str], str | dict[str, Any] | Awaitable[str | dict[str, Any]]]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

class ReferenceExtractError(Exception):
    def __init__(
        self,
        code: c.ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

async def extract_reference(
    url: str,
    language: str = "zh",
    *,
    asr_invoke: AsrInvoke,
    object_store: ObjectStore,
    secret_store: SecretStore,
) -> c.ReferenceExtractResult:
    parsed = _supported_url(url)
    platform = _platform_from_host(parsed.netloc)
    headers = {"User-Agent": USER_AGENT}
    cookie_header = stored_cookie_header(secret_store)
    if cookie_header:
        headers["Cookie"] = cookie_header
    douyin_title: str | None = None
    douyin_duration: float | None = None

    if platform == "douyin":
        douyin = await _extract_douyin_share(url, secret_store)
        headers.update(douyin.headers)
        douyin_title = douyin.title
        douyin_duration = douyin.duration_sec
        url = douyin.resolved_url or url

    info = await _extract_info(url, headers=headers)
    platform = _platform_from_info(info, fallback=platform)
    title = _clean_optional_text(info.get("title")) or douyin_title
    duration = _duration_from_value(info.get("duration")) or douyin_duration
    resolved_url = _clean_optional_text(info.get("webpage_url")) or url

    subtitle = await _subtitle_from_info(info, language=language, headers=headers)
    if subtitle:
        return c.ReferenceExtractResult(
            reference_script=subtitle,
            source="subtitle",
            title=title,
            platform=platform,
            duration_sec=duration,
            resolved_url=resolved_url,
        )

    transcript = await _download_upload_and_asr(url, language, asr_invoke, object_store, headers)
    return c.ReferenceExtractResult(
        reference_script=transcript,
        source="asr",
        title=title,
        platform=platform,
        duration_sec=duration,
        resolved_url=resolved_url,
    )

def _supported_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReferenceExtractError(
            c.ErrorCode.reference_unsupported_platform,
            "Reference URL must be an http(s) URL.",
            details={"url": url},
        )
    return parsed

def _platform_from_host(host: str) -> str:
    return _platform_from_key(host.lower(), "generic")

def _platform_from_info(info: dict[str, Any], *, fallback: str) -> str:
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    return _platform_from_key(key, fallback)


def _platform_from_key(value: str, fallback: str) -> str:
    for token, platform in (
        ("douyin", "douyin"),
        ("youtube", "youtube"),
        ("youtu.be", "youtube"),
        ("bilibili", "bilibili"),
        ("kuaishou", "kuaishou"),
    ):
        if token in value:
            return platform
    return fallback


@dataclass(frozen=True)
class _DouyinExtract:
    headers: dict[str, str]
    title: str | None
    duration_sec: float | None
    resolved_url: str | None


async def _extract_douyin_share(url: str, secret_store: SecretStore) -> _DouyinExtract:
    headers = {"User-Agent": USER_AGENT}
    cookie = stored_cookie_header(secret_store)
    if cookie:
        headers["Cookie"] = cookie
    try:
        page = await _http_get_text(url, headers=headers)
        router_data = _parse_router_data(page)
        item = _find_douyin_item(router_data) if router_data else {}
    except ReferenceExtractError:
        raise
    except Exception as exc:
        raise ReferenceExtractError(
            c.ErrorCode.reference_unreachable,
            "Douyin share page is unreachable.",
            details={"reason": str(exc)},
        ) from exc
    desc = _clean_optional_text(item.get("desc"))
    title = _clean_optional_text(item.get("title")) or desc
    resolved = next(
        (_clean_optional_text(item.get(key)) for key in ("share_url", "url", "video_url") if _clean_optional_text(item.get(key))),
        None,
    ) or url
    return _DouyinExtract(
        headers=headers,
        title=title,
        duration_sec=_duration_from_value(item.get("duration")),
        resolved_url=resolved,
    )


def _parse_router_data(page: str) -> dict[str, Any] | None:
    start = page.find("window._ROUTER_DATA")
    if start < 0:
        return None
    eq = page.find("=", start)
    if eq < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(page[eq + 1 :].lstrip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _find_douyin_item(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "desc" in value and ("duration" in value or "share_url" in value or "video" in value):
            return value
        for nested in value.values():
            found = _find_douyin_item(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_douyin_item(nested)
            if found:
                return found
    return {}


async def _extract_info(url: str, *, headers: dict[str, str]) -> dict[str, Any]:
    ydl_cls = _load_youtube_dl()
    opts = _ydl_options(headers, skip_download=True)
    try:
        def run() -> dict[str, Any]:
            with ydl_cls(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(run)
    except Exception as exc:
        raise _map_ytdlp_error(exc) from exc
    if not isinstance(info, dict):
        raise ReferenceExtractError(c.ErrorCode.reference_unreachable, "yt-dlp returned no video info.")
    return info


async def fetch_metadata(url: str, *, cookie_header: str | None = None) -> dict[str, Any]:
    """Run a single yt-dlp metadata fetch (no media download) for cookie tests.

    Validates the URL, attaches the cookie header if provided, and returns a
    compact metadata dict ({title, platform, resolved_url, duration_sec}).
    Raises :class:`ReferenceExtractError` on any reachability/extractor error.
    """
    parsed = _supported_url(url)
    headers = {"User-Agent": USER_AGENT}
    if cookie_header:
        headers["Cookie"] = cookie_header
    info = await _extract_info(url, headers=headers)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first:
            info = first
    return {
        "title": _clean_optional_text(info.get("title")),
        "platform": _platform_from_info(info, fallback=_platform_from_host(parsed.netloc)),
        "resolved_url": _clean_optional_text(info.get("webpage_url")) or url,
        "duration_sec": _duration_from_value(info.get("duration")),
    }


async def _subtitle_from_info(info: dict[str, Any], *, language: str, headers: dict[str, str]) -> str | None:
    for track in _subtitle_tracks(info, language):
        raw = str(track.get("data") or "")
        if not raw and track.get("url"):
            raw = await _http_get_text(str(track["url"]), headers=headers)
        parsed = _parse_subtitle_text(raw, str(track.get("ext") or ""))
        if parsed:
            return parsed
    return None


def _subtitle_tracks(info: dict[str, Any], language: str) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    for bucket in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not isinstance(bucket, dict):
            continue
        for lang in _language_candidates(language):
            values = bucket.get(lang)
            if isinstance(values, list):
                tracks.extend(item for item in values if isinstance(item, dict))
    priorities = {"vtt": 0, "srt": 1, "json3": 2, "srv3": 3}
    return sorted(tracks, key=lambda item: priorities.get(str(item.get("ext") or "").lower(), 9))


def _language_candidates(language: str) -> list[str]:
    base = (language or "zh").strip()
    return list(dict.fromkeys(item for item in [base, base.lower(), base.split("-")[0], "zh-Hans", "zh-CN", "zh", "en"] if item))


def _parse_subtitle_text(raw: str, ext: str) -> str | None:
    if not raw.strip():
        return None
    if ext.lower() == "json3":
        return _parse_json3_subtitle(raw)
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = html.unescape(re.sub(r"<[^>]+>", "", raw_line)).strip()
        if not line or line == "WEBVTT" or line.isdigit():
            continue
        if "-->" in line or line.startswith(("NOTE", "STYLE")):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines).strip() or None


def _parse_json3_subtitle(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    lines: list[str] = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        text = "".join(str(seg.get("utf8") or "") for seg in event.get("segs", []) if isinstance(seg, dict)).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines).strip() or None


async def _download_upload_and_asr(
    url: str,
    language: str,
    asr_invoke: AsrInvoke,
    object_store: ObjectStore,
    headers: dict[str, str],
) -> str:
    with tempfile.TemporaryDirectory(prefix="cutagent-reference-") as tmp:
        audio_path = await _download_audio(url, headers=headers, directory=Path(tmp))
        ref: ObjectRef | None = None
        try:
            ref = object_store.prepare_upload(audio_path.name, "reference-audio", tier="ephemeral")
            object_store.put_bytes(ref, audio_path.read_bytes())
            signed_url = object_store.signed_url(ref.uri).url
            transcript = await _invoke_asr(asr_invoke, signed_url, language)
        except ReferenceExtractError:
            raise
        except Exception as exc:
            raise ReferenceExtractError(
                c.ErrorCode.reference_asr_failed,
                "ASR transcription failed.",
                details={"reason": str(exc)},
            ) from exc
        finally:
            if ref is not None:
                object_store.delete(ref.uri)
    return transcript


async def _download_audio(url: str, *, headers: dict[str, str], directory: Path) -> Path:
    ydl_cls = _load_youtube_dl()
    opts = {**_ydl_options(headers, skip_download=False), "format": "bestaudio/best", "outtmpl": str(directory / "reference.%(ext)s"), "noplaylist": True}
    try:
        def run() -> Path:
            with ydl_cls(opts) as ydl:
                ydl.download([url])
            files = [path for path in directory.iterdir() if path.is_file()]
            if not files:
                raise FileNotFoundError("yt-dlp did not create an audio file.")
            return max(files, key=lambda path: path.stat().st_size)

        return await asyncio.to_thread(run)
    except Exception as exc:
        raise _map_ytdlp_error(exc) from exc


async def _invoke_asr(asr_invoke: AsrInvoke, audio_url: str, language: str) -> str:
    result = asr_invoke(audio_url, language)
    if inspect.isawaitable(result):
        result = await result
    text = _asr_text(result)
    if not text:
        raise ReferenceExtractError(c.ErrorCode.reference_asr_failed, "ASR response did not include text.")
    return text


def _asr_text(result: Any) -> str | None:
    if isinstance(result, str):
        return result.strip() or None
    output = getattr(result, "output", None)
    if isinstance(output, dict):
        result = output
    if isinstance(result, dict):
        text = result.get("text")
        if not text and isinstance(result.get("output"), dict):
            text = result["output"].get("text")
        return str(text).strip() if text else None
    return None


async def _http_get_text(url: str, headers: dict[str, str] | None = None) -> str:
    import httpx

    def run() -> str:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=20)
        response.raise_for_status()
        return response.text

    return await asyncio.to_thread(run)


def _ydl_options(headers: dict[str, str], *, skip_download: bool) -> dict[str, Any]:
    return {"quiet": True, "no_warnings": True, "skip_download": skip_download, "http_headers": headers}


def _load_youtube_dl():
    try:
        from yt_dlp import YoutubeDL
    except ModuleNotFoundError as exc:
        raise ReferenceExtractError(
            c.ErrorCode.reference_unsupported_platform,
            "yt-dlp is not installed in this environment.",
        ) from exc
    return YoutubeDL


def _map_ytdlp_error(exc: Exception) -> ReferenceExtractError:
    message = str(exc)
    lowered = message.lower()
    if "unsupported url" in lowered or "no suitable extractor" in lowered:
        return ReferenceExtractError(c.ErrorCode.reference_unsupported_platform, "Reference platform is unsupported.", details={"reason": message})
    return ReferenceExtractError(c.ErrorCode.reference_unreachable, "Reference URL is unreachable.", details={"reason": message})


def _duration_from_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration > 10_000:
        duration = duration / 1000
    return duration


def _clean_optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
