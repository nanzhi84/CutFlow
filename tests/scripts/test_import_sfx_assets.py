from __future__ import annotations

from types import SimpleNamespace

from packages.production.pipeline.nodes.subtitle_and_bgm_mix import (
    _select_caption_sfx_asset_ids,
)
from scripts import import_sfx_assets


def test_caption_sfx_pack_contains_ten_unique_cc0_assets() -> None:
    specs = import_sfx_assets.SFX_PACK

    interface_archive = (
        "https://kenney.nl/media/pages/assets/interface-sounds/"
        "fa43c1dd4d-1677589452/kenney_interface-sounds.zip"
    )
    impact_archive = (
        "https://kenney.nl/media/pages/assets/impact-sounds/"
        "87b4ddecda-1677589768/kenney_impact-sounds.zip"
    )
    expected_sources = {
        interface_archive: "https://kenney.nl/assets/interface-sounds",
        impact_archive: "https://kenney.nl/assets/impact-sounds",
    }
    expected_pack = {
        "asset_sfx_click": (
            "Caption Click",
            "click",
            interface_archive,
            "Audio/click_001.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_ding": (
            "Caption Ding",
            "ding",
            interface_archive,
            "Audio/confirmation_001.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_whoosh": (
            "Caption Whoosh",
            "whoosh",
            interface_archive,
            "Audio/scroll_001.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_impact": (
            "Caption Impact",
            "impact",
            impact_archive,
            "Audio/impactPunch_heavy_002.ogg",
            expected_sources[impact_archive],
        ),
        "asset_sfx_pop_soft": (
            "Caption Pop Soft",
            "pop",
            interface_archive,
            "Audio/select_001.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_pop_bright": (
            "Caption Pop Bright",
            "pop",
            interface_archive,
            "Audio/click_002.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_ding_soft": (
            "Caption Ding Soft",
            "ding",
            interface_archive,
            "Audio/confirmation_002.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_whoosh_fast": (
            "Caption Whoosh Fast",
            "whoosh",
            interface_archive,
            "Audio/scroll_003.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_rise": (
            "Caption Rise",
            "rise",
            interface_archive,
            "Audio/maximize_003.ogg",
            expected_sources[interface_archive],
        ),
        "asset_sfx_sparkle": (
            "Caption Sparkle",
            "sparkle",
            impact_archive,
            "Audio/impactGlass_light_002.ogg",
            expected_sources[impact_archive],
        ),
    }

    assert len(specs) == 10
    assert len({spec.asset_id for spec in specs}) == len(specs)
    assert {spec.sfx_class for spec in specs} == {
        "click",
        "ding",
        "impact",
        "pop",
        "rise",
        "sparkle",
        "whoosh",
    }
    assert {
        spec.asset_id: (
            spec.title,
            spec.sfx_class,
            spec.archive_url,
            spec.archive_member,
            spec.source_page,
        )
        for spec in specs
    } == expected_pack
    assert len({(spec.archive_url, spec.archive_member) for spec in specs}) == len(specs)
    assert all(spec.source_page == expected_sources[spec.archive_url] for spec in specs)
    assert all("license:CC0-1.0" in import_sfx_assets._tags(spec) for spec in specs)


def test_caption_sfx_assets_are_visible_to_the_class_selector() -> None:
    for spec in import_sfx_assets.SFX_PACK:
        tags = import_sfx_assets._tags(spec)
        asset = SimpleNamespace(
            id=spec.asset_id,
            kind="sfx",
            usable=True,
            tags=tags,
        )

        assert "caption_emphasis" in tags
        assert f"sfx_class:{spec.sfx_class}" in tags
        assert _select_caption_sfx_asset_ids([asset]) == {
            spec.sfx_class: spec.asset_id
        }
