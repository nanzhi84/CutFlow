"""Regression guards for the multi-vendor voice fields (vendor + status)."""

from __future__ import annotations

from packages.core.contracts import VoiceProfile
from packages.media.sqlalchemy_repository import _vendor_from_profile_id


def test_vendor_derivation_from_profile_id() -> None:
    assert _vendor_from_profile_id("minimax.tts.prod") == "minimax"
    assert _vendor_from_profile_id("volcengine.tts.prod") == "volcengine"
    # sandbox is a pseudo-vendor → empty so the UI buckets it under '未指定厂商'
    assert _vendor_from_profile_id("sandbox.tts.default") == ""
    assert _vendor_from_profile_id(None) == ""
    assert _vendor_from_profile_id("") == ""


def test_voice_profile_defaults_to_ready_no_vendor() -> None:
    vp = VoiceProfile(id="v1", display_name="x", source="cloned")
    assert vp.vendor == ""
    assert vp.status == "ready"


def test_voice_profile_accepts_training_status() -> None:
    vp = VoiceProfile(
        id="v2", display_name="y", source="cloned", vendor="volcengine", status="training"
    )
    assert vp.vendor == "volcengine"
    assert vp.status == "training"
