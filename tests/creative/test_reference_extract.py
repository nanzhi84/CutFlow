from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path

import pytest

from packages.core import contracts as c
from packages.core.storage.object_store import ObjectRef, StoredObject


def _module():
    try:
        return importlib.import_module("packages.creative.reference_extract")
    except ModuleNotFoundError as exc:
        pytest.fail(f"reference_extract module is missing: {exc}")


def _patch_to_thread(module, monkeypatch: pytest.MonkeyPatch) -> None:
    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", inline_to_thread)
    # Skip the real DNS lookup in the SSRF guard so unit tests stay offline; the
    # guard itself is covered directly in test_reference_security.py.
    monkeypatch.setattr(module, "_assert_public_url", lambda url, **kwargs: module._supported_url(url))


def _run(value):
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


class FakeSecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get(self, secret_ref: str) -> str | None:
        return self.values.get(secret_ref)


class FakeObjectStore:
    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, str]] = []
        self.put_calls: list[tuple[ObjectRef, bytes]] = []
        self.deleted: list[str] = []

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        _ = content_key
        self.prepare_calls.append({"filename": filename, "purpose": purpose, "tier": tier})
        return ObjectRef(bucket="cutagent-ephemeral", key=f"{purpose}/{filename}", uri=f"local://cutagent-ephemeral/{purpose}/{filename}")

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        self.put_calls.append((ref, content))
        return StoredObject(ref=ref, size_bytes=len(content), sha256="sha")

    def signed_url(self, uri: str, **_: object) -> c.SignedUrlResponse:
        return c.SignedUrlResponse(url=f"https://signed.example/{uri.rsplit('/', 1)[-1]}", expires_at=c.utcnow(), request_id="req_test")

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


class FakeYDL:
    info: dict = {}
    created_paths: list[Path] = []
    download_calls = 0

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict:
        assert download is False
        return dict(self.info)

    def download(self, urls: list[str]) -> int:
        _ = urls
        FakeYDL.download_calls += 1
        target = Path(str(self.opts["outtmpl"]).replace("%(ext)s", "m4a"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio-bytes")
        FakeYDL.created_paths.append(target)
        return 0


def test_subtitle_track_returns_script_without_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    _patch_to_thread(module, monkeypatch)
    FakeYDL.info = {
        "title": "字幕视频",
        "duration": 12,
        "webpage_url": "https://youtu.be/resolved",
        "extractor_key": "Youtube",
        "subtitles": {"zh": [{"ext": "vtt", "url": "https://subtitle.example/caption.vtt"}]},
    }
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)

    async def fake_get_text(url: str, headers: dict[str, str] | None = None) -> str:
        assert url == "https://subtitle.example/caption.vtt"
        return "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n第一句\n\n00:00:01.000 --> 00:00:02.000\n第二句"

    monkeypatch.setattr(module, "_http_get_text", fake_get_text)
    asr_calls: list[str] = []

    result = _run(
        module.extract_reference(
            "https://youtu.be/demo",
            asr_invoke=lambda audio_url, language: asr_calls.append(audio_url),
            object_store=FakeObjectStore(),
            secret_store=FakeSecretStore(),
        )
    )

    assert result.reference_script == "第一句\n第二句"
    assert result.source == "subtitle"
    assert result.title == "字幕视频"
    assert result.platform == "youtube"
    assert result.duration_sec == 12
    assert result.resolved_url == "https://youtu.be/resolved"
    assert asr_calls == []


def test_subtitle_strips_youtube_vtt_metadata_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # YouTube VTT payloads prepend "Kind: captions" / "Language: <lang>" metadata
    # lines after WEBVTT; these must NOT leak into the extracted reference script.
    module = _module()
    _patch_to_thread(module, monkeypatch)
    FakeYDL.info = {
        "title": "TED",
        "duration": 844,
        "webpage_url": "https://youtu.be/resolved",
        "extractor_key": "Youtube",
        "subtitles": {"en": [{"ext": "vtt", "url": "https://subtitle.example/en.vtt"}]},
    }
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)

    async def fake_get_text(url: str, headers: dict[str, str] | None = None) -> str:
        return (
            "WEBVTT\nKind: captions\nLanguage: en\n\n"
            "00:00:00.000 --> 00:00:02.000\nSo in college,\n\n"
            "00:00:02.000 --> 00:00:04.000\nI was a government major.\n"
        )

    monkeypatch.setattr(module, "_http_get_text", fake_get_text)

    result = _run(
        module.extract_reference(
            "https://youtu.be/demo",
            "en",
            asr_invoke=lambda audio_url, language: None,
            object_store=FakeObjectStore(),
            secret_store=FakeSecretStore(),
        )
    )

    assert result.reference_script == "So in college,\nI was a government major."
    assert "Kind:" not in result.reference_script
    assert "Language:" not in result.reference_script


def test_no_subtitles_downloads_ephemeral_audio_then_invokes_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    _patch_to_thread(module, monkeypatch)
    FakeYDL.info = {
        "title": "无字幕视频",
        "duration": 9.5,
        "webpage_url": "https://youtu.be/audio",
        "extractor_key": "Youtube",
        "subtitles": {},
        "automatic_captions": {},
    }
    FakeYDL.created_paths = []
    FakeYDL.download_calls = 0
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)
    store = FakeObjectStore()
    asr_calls: list[tuple[str, str]] = []

    def fake_asr(audio_url: str, language: str) -> str:
        asr_calls.append((audio_url, language))
        return "ASR 识别文案"

    result = _run(
        module.extract_reference(
            "https://youtu.be/audio",
            "zh",
            asr_invoke=fake_asr,
            object_store=store,
            secret_store=FakeSecretStore(),
        )
    )

    assert result.reference_script == "ASR 识别文案"
    assert result.source == "asr"
    assert FakeYDL.download_calls == 1
    assert store.prepare_calls == [{"filename": "reference.m4a", "purpose": "reference-audio", "tier": "durable"}]
    assert store.put_calls[0][1] == b"audio-bytes"
    assert asr_calls == [("https://signed.example/reference.m4a", "zh")]
    assert store.deleted == ["local://cutagent-ephemeral/reference-audio/reference.m4a"]
    assert FakeYDL.created_paths and all(not path.exists() for path in FakeYDL.created_paths)


def test_douyin_no_subtitle_falls_back_to_guest_browser_sniff_then_asr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cookie-free Douyin: the HTTP share-page parse is blocked, so the flow must
    # NOT abort — it falls back to the guest headless-browser sniff, downloads the
    # sniffed video stream with the browser's cookies, and ASRs it.
    module = _module()
    _patch_to_thread(module, monkeypatch)
    FakeYDL.info = {"extractor_key": "Douyin", "subtitles": {}, "automatic_captions": {}}
    FakeYDL.created_paths = []
    FakeYDL.download_calls = 0
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)

    async def blocked_get_text(url: str, headers: dict[str, str] | None = None) -> str:
        raise RuntimeError("douyin share page blocked without cookie")

    monkeypatch.setattr(module, "_http_get_text", blocked_get_text)

    async def raising_extract_info(url: str, *, headers: dict[str, str]) -> dict:
        # yt-dlp is also blocked without a cookie -> info stays empty -> sniff fallback.
        raise module.ReferenceExtractError(
            c.ErrorCode.reference_unreachable, "yt-dlp blocked without cookie"
        )

    monkeypatch.setattr(module, "_extract_info", raising_extract_info)

    from packages.creative.reference_browser import BrowserMediaResult

    sniff_calls: list[tuple[str, str | None]] = []

    async def fake_sniffer(url: str, *, cookie_header: str | None = None) -> BrowserMediaResult:
        sniff_calls.append((url, cookie_header))
        return BrowserMediaResult(
            media_url="https://cdn.douyin.example/stream.mp4",
            cookie_header="ttwid=guest",
            title="抖音对标视频",
            duration_sec=30.0,
            resolved_url="https://www.douyin.com/video/9",
        )

    download_calls: list[tuple[str, str | None]] = []

    async def fake_download_audio(url: str, *, headers: dict[str, str], directory: Path) -> Path:
        download_calls.append((url, headers.get("Cookie")))
        target = directory / "reference.m4a"
        target.write_bytes(b"audio-bytes")
        return target

    monkeypatch.setattr(module, "_download_audio", fake_download_audio)

    store = FakeObjectStore()
    asr_calls: list[tuple[str, str]] = []

    def fake_asr(audio_url: str, language: str) -> str:
        asr_calls.append((audio_url, language))
        return "抖音游客 ASR 文案"

    result = _run(
        module.extract_reference(
            "https://v.douyin.com/abc/",
            "zh",
            asr_invoke=fake_asr,
            object_store=store,
            secret_store=FakeSecretStore(),
            sniffer=fake_sniffer,
        )
    )

    assert result.source == "asr"
    assert result.reference_script == "抖音游客 ASR 文案"
    assert result.platform == "douyin"
    # sniffed the original pasted url (share parse was blocked, so url not resolved)
    assert sniff_calls and sniff_calls[0][0] == "https://v.douyin.com/abc/"
    # downloaded the sniffed stream URL, carrying the browser's guest cookie
    assert download_calls == [("https://cdn.douyin.example/stream.mp4", "ttwid=guest")]
    assert asr_calls == [("https://signed.example/reference.m4a", "zh")]
    assert result.title == "抖音对标视频"
    assert result.duration_sec == 30.0
    assert result.resolved_url == "https://www.douyin.com/video/9"


def test_douyin_share_page_uses_cookie_and_router_data(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    _patch_to_thread(module, monkeypatch)
    seen_headers: list[dict[str, str] | None] = []
    FakeYDL.info = {
        "extractor_key": "Douyin",
        "subtitles": {"zh": [{"ext": "vtt", "url": "https://subtitle.example/douyin.vtt"}]},
    }
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)

    async def fake_get_text(url: str, headers: dict[str, str] | None = None) -> str:
        seen_headers.append(headers)
        if url == "https://subtitle.example/douyin.vtt":
            return "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n抖音口播文案"
        return (
            "<html><script>window._ROUTER_DATA = "
            '{"loaderData":{"video/page":{"videoInfoRes":{"item_list":[{"desc":"抖音口播标题","duration":83000,"share_url":"https://www.douyin.com/video/123"}]}}}}'
            "</script></html>"
        )

    monkeypatch.setattr(module, "_http_get_text", fake_get_text)

    result = _run(
        module.extract_reference(
            "https://v.douyin.com/abc/",
            asr_invoke=lambda audio_url, language: "unused",
            object_store=FakeObjectStore(),
            secret_store=FakeSecretStore({"douyin_cookie": "sessionid=manual"}),
        )
    )

    assert seen_headers and seen_headers[0]["Cookie"] == "sessionid=manual"
    assert result.reference_script == "抖音口播文案"
    assert result.source == "subtitle"
    assert result.title == "抖音口播标题"
    assert result.platform == "douyin"
    assert result.duration_sec == 83
    assert result.resolved_url == "https://www.douyin.com/video/123"


@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("ftp://example.com/video.mp4", "reference.unsupported_platform"),
        ("not-a-url", "reference.unsupported_platform"),
    ],
)
def test_invalid_or_unsupported_url_maps_to_clear_error(url: str, expected_code: str) -> None:
    module = _module()

    with pytest.raises(module.ReferenceExtractError) as exc:
        _run(
            module.extract_reference(
                url,
                asr_invoke=lambda audio_url, language: "unused",
                object_store=FakeObjectStore(),
                secret_store=FakeSecretStore(),
            )
        )

    assert exc.value.code == c.ErrorCode(expected_code)


def test_ytdlp_unreachable_and_asr_failure_have_distinct_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    _patch_to_thread(module, monkeypatch)

    class UnreachableYDL(FakeYDL):
        def extract_info(self, url: str, download: bool = False) -> dict:
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(module, "_load_youtube_dl", lambda: UnreachableYDL)
    with pytest.raises(module.ReferenceExtractError) as unreachable:
        _run(
            module.extract_reference(
                "https://youtu.be/unreachable",
                asr_invoke=lambda audio_url, language: "unused",
                object_store=FakeObjectStore(),
                secret_store=FakeSecretStore(),
            )
        )
    assert unreachable.value.code == c.ErrorCode.reference_unreachable

    FakeYDL.info = {"title": "needs asr", "duration": 1, "webpage_url": "https://youtu.be/asr"}
    monkeypatch.setattr(module, "_load_youtube_dl", lambda: FakeYDL)

    def failing_asr(audio_url: str, language: str) -> str:
        raise RuntimeError("asr failed")

    with pytest.raises(module.ReferenceExtractError) as asr_failed:
        _run(
            module.extract_reference(
                "https://youtu.be/asr",
                asr_invoke=failing_asr,
                object_store=FakeObjectStore(),
                secret_store=FakeSecretStore(),
            )
        )
    assert asr_failed.value.code == c.ErrorCode.reference_asr_failed
