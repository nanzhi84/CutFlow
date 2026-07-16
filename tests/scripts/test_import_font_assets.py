from __future__ import annotations

import hashlib
import io
import shutil
import zipfile

import pytest

from scripts import import_font_assets


def test_font_pack_sources_are_commit_pinned_with_content_hashes():
    assert import_font_assets.FONT_PACK
    assert len({spec.asset_id for spec in import_font_assets.FONT_PACK}) == len(
        import_font_assets.FONT_PACK
    )
    for spec in import_font_assets.FONT_PACK:
        source_url = spec.archive_url or spec.url
        assert source_url
        assert "/main/" not in source_url
        assert len(spec.sha256) == 64
        int(spec.sha256, 16)
        assert f"license:{spec.license}" in import_font_assets._tags(spec)
        if spec.archive_url:
            assert spec.url is None
            assert spec.archive_member
            assert spec.archive_sha256
            assert len(spec.archive_sha256) == 64
            int(spec.archive_sha256, 16)


def test_font_pack_contains_the_seven_caption_style_fonts() -> None:
    expected = {
        "asset_font_zcool_kuaile",
        "asset_font_zcool_qingke_huangyou",
        "asset_font_mashanzheng",
        "asset_font_zhimangxing",
        "asset_font_longcang",
        "asset_font_smiley_sans",
        "asset_font_lxgw_marker",
    }
    specs = {spec.asset_id: spec for spec in import_font_assets.FONT_PACK}

    assert expected <= specs.keys()
    assert all(specs[asset_id].usages == ("huazi",) for asset_id in expected)
    assert all(specs[asset_id].license == "OFL-1.1" for asset_id in expected)


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


def test_archive_font_is_extracted_and_verified(monkeypatch, tmp_path) -> None:
    font_payload = b"font-from-release-archive"
    source_archive = tmp_path / "source.zip"
    with zipfile.ZipFile(source_archive, "w") as archive:
        archive.writestr("release/font.ttf", font_payload)
    archive_sha256 = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    spec = import_font_assets.FontSpec(
        asset_id="asset_font_archive_fixture",
        title="Archive Fixture",
        filename="font.ttf",
        url=None,
        sha256=hashlib.sha256(font_payload).hexdigest(),
        content_type="font/ttf",
        family="Archive Fixture",
        weight=400,
        style="fixture",
        usages=("huazi",),
        source_repo="https://example.invalid/repo",
        license_url="https://example.invalid/license",
        license="OFL-1.1",
        archive_url="https://example.invalid/release.zip",
        archive_member="release/font.ttf",
        archive_sha256=archive_sha256,
    )

    def fake_download(url, target, *, expected_sha256, force):
        assert url == spec.archive_url
        assert expected_sha256 == archive_sha256
        assert force is False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_archive, target)

    monkeypatch.setattr(import_font_assets, "_download", fake_download)

    path = import_font_assets._materialize_font(spec, tmp_path / "downloads", force=False)

    assert path.read_bytes() == font_payload
