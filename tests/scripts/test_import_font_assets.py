from __future__ import annotations

import hashlib
import io

import pytest

from scripts import import_font_assets


def test_font_pack_sources_are_commit_pinned_with_content_hashes():
    assert import_font_assets.FONT_PACK
    for spec in import_font_assets.FONT_PACK:
        assert "/main/" not in spec.url
        assert len(spec.sha256) == 64
        int(spec.sha256, 16)


def test_download_reuses_only_a_hash_verified_cached_font(monkeypatch, tmp_path):
    payload = b"verified-font"
    target = tmp_path / "font.otf"
    target.write_bytes(payload)

    monkeypatch.setattr(
        import_font_assets,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network not expected")),
    )

    import_font_assets._download(
        "https://example.invalid/font.otf",
        target,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        force=False,
    )

    assert target.read_bytes() == payload


def test_download_rejects_bytes_that_do_not_match_the_pinned_hash(monkeypatch, tmp_path):
    target = tmp_path / "font.otf"
    monkeypatch.setattr(
        import_font_assets,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"tampered-font"),
    )

    with pytest.raises(RuntimeError, match="integrity check failed"):
        import_font_assets._download(
            "https://example.invalid/font.otf",
            target,
            expected_sha256=hashlib.sha256(b"expected-font").hexdigest(),
            force=False,
        )

    assert not target.exists()
    assert not target.with_suffix(".otf.part").exists()
