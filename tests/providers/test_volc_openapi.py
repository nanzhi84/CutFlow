"""Tests for the Volcengine speech_saas_prod management-plane client (AK/SK V4)."""

from __future__ import annotations

import json

import httpx
import pytest

from packages.ai.gateway.provider_gateway import ProviderRuntimeError
from packages.ai.providers.volc_openapi import VolcSpeechOpenAPI, _signed_headers
from packages.core.contracts import ErrorCode


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_signed_headers_shape_and_no_secret_leak() -> None:
    headers, query = _signed_headers(
        "AKLTxxx", "topsecret", "ListAPIKeys", "2025-05-20", b'{"AppID":"1"}'
    )
    assert query == "Action=ListAPIKeys&Version=2025-05-20"
    auth = headers["Authorization"]
    assert auth.startswith("HMAC-SHA256 Credential=AKLTxxx/")
    assert "speech_saas_prod/request" in auth
    # the raw secret must never appear in the signed header
    assert "topsecret" not in auth


# Mirrors the real account: 1 successful clone + an empty/unallocated slot
# (State=Unknown, the purchased-but-unused quota) + a failed one + an empty id.
_STATUSES = {
    "Result": {
        "Statuses": [
            {
                "SpeakerID": "S_UDXV2pG62",
                "Alias": "无忧快喷",
                "State": "Success",
                "DemoAudio": "https://x/demo.wav",
                "ModelTypeDetails": [
                    {
                        "ModelType": 1,
                        "IclSpeakerId": "ICL_legacy_123",
                        "ResourceID": "seed-icl-1.0",
                    },
                    {
                        "ModelType": 5,
                        "IclSpeakerId": "ICL_uranus_123",
                        "ResourceID": "seed-icl-2.0",
                        "DemoAudio": "https://x/icl2-demo.wav",
                    },
                ],
            },
            {"SpeakerID": "S_SLOT", "Alias": "", "State": "Unknown", "DemoAudio": None},
            {"SpeakerID": "S_F", "Alias": "失败", "State": "Failed"},
            {"SpeakerID": "", "Alias": "skip-empty-id", "State": "Success"},
        ]
    }
}


def _train_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["Action"] == "ListMegaTTSTrainStatus"
    assert request.url.params["Version"] == "2025-05-21"
    assert json.loads(request.content)["AppID"] == "9635790622"
    return httpx.Response(200, json=_STATUSES)


def test_list_voices_only_returns_successful() -> None:
    api = VolcSpeechOpenAPI(_client(_train_handler), "ak", "sk")
    voices = api.list_voices("9635790622")
    # The purchased S_ value is the only persisted async SpeakerID. The internal
    # ICL_uranus id is not accepted by /api/v3/tts/submit and must not leak into
    # the normalized voice-sync shape.
    assert voices == [
        {
            "voice_id": "S_UDXV2pG62",
            "display_name": "无忧快喷",
            "status": "ready",
            "preview_url": "https://x/icl2-demo.wav",
        }
    ]


def test_list_voices_falls_back_to_legacy_id_without_model_type_5() -> None:
    payload = {
        "Result": {
            "Statuses": [
                {
                    "SpeakerID": "S_LEGACY",
                    "Alias": "Legacy",
                    "State": "Success",
                    "ModelTypeDetails": [
                        {
                            "ModelType": 1,
                            "IclSpeakerId": "ICL_legacy_only",
                            "ResourceID": "seed-icl-1.0",
                        }
                    ],
                }
            ]
        }
    }
    api = VolcSpeechOpenAPI(
        _client(lambda request: httpx.Response(200, json=payload)), "ak", "sk"
    )

    assert api.list_voices("9635790622") == [
        {
            "voice_id": "S_LEGACY",
            "display_name": "Legacy",
            "status": "ready",
            "preview_url": None,
        }
    ]


def test_get_train_status_maps_per_speaker() -> None:
    api = VolcSpeechOpenAPI(_client(_train_handler), "ak", "sk")
    assert api.get_train_status("9635790622", "S_UDXV2pG62") == "ready"
    assert api.get_train_status("9635790622", "S_SLOT") == "training"  # Unknown→training
    assert api.get_train_status("9635790622", "S_F") == "failed"
    assert api.get_train_status("9635790622", "S_MISSING") is None


def test_list_free_slots_returns_only_empty_slots() -> None:
    api = VolcSpeechOpenAPI(_client(_train_handler), "ak", "sk")
    # S_SLOT (Unknown, no Alias) is claimable; Success/Failed/named are excluded
    assert api.list_free_slots("9635790622") == ["S_SLOT"]


def test_ensure_api_key_returns_existing_active() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Action"] == "ListAPIKeys"
        return httpx.Response(
            200,
            json={
                "Result": {
                    "APIKeys": [
                        {"ID": 1, "Name": "old", "APIKey": "disabled-key", "Disable": True},
                        {"ID": 2, "Name": "k", "APIKey": "f660e4fc", "Disable": False},
                    ]
                }
            },
        )

    api = VolcSpeechOpenAPI(_client(handler), "ak", "sk")
    assert api.ensure_api_key("9635790622", "cutagent-tts") == "f660e4fc"


def test_ensure_api_key_creates_when_missing() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        calls.append(action)
        if action == "ListAPIKeys":
            if "CreateAPIKey" in calls:
                return httpx.Response(
                    200,
                    json={
                        "Result": {
                            "APIKeys": [
                                {"Name": "cutagent-tts", "APIKey": "new-key", "Disable": False}
                            ]
                        }
                    },
                )
            return httpx.Response(200, json={"Result": {"APIKeys": []}})
        if action == "CreateAPIKey":
            assert json.loads(request.content) == {"AppID": "9635790622", "Name": "cutagent-tts"}
            return httpx.Response(200, json={"Result": {}})
        raise AssertionError(f"unexpected action {action}")

    api = VolcSpeechOpenAPI(_client(handler), "ak", "sk")
    assert api.ensure_api_key("9635790622", "cutagent-tts") == "new-key"
    assert "CreateAPIKey" in calls


def test_auth_error_maps_to_auth_failed_without_leaking_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {
                    "Error": {"Code": "AccessDenied", "Message": "Credential=AKLTleaked/... bad"}
                }
            },
        )

    api = VolcSpeechOpenAPI(_client(handler), "ak", "sk")
    with pytest.raises(ProviderRuntimeError) as excinfo:
        api.list_voices("x")
    assert excinfo.value.code == ErrorCode.provider_auth_failed
    assert "AKLTleaked" not in excinfo.value.message
