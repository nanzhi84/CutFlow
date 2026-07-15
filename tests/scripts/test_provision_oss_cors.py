from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from scripts import provision_oss_cors


class FakeLifecycleClient:
    def __init__(self, rules=None, error: Exception | None = None) -> None:
        self.rules = list(rules or [])
        self.error = error
        self.put_calls: list[dict] = []

    def get_bucket_lifecycle_configuration(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return {"Rules": self.rules}

    def put_bucket_lifecycle_configuration(self, **kwargs):
        self.put_calls.append(kwargs)


class NoLifecycleError(ClientError):
    def __init__(self, code: str) -> None:
        super().__init__({"Error": {"Code": code}}, "GetBucketLifecycleConfiguration")


def _config():
    return SimpleNamespace(
        bucket="cutagent-prod",
        s3=SimpleNamespace(
            endpoint_url="https://oss.example.test",
            access_key="access",
            secret_key="secret",
            region_name="us-east-1",
            addressing_style="path",
        ),
    )


def _install_client(monkeypatch, client: FakeLifecycleClient) -> None:
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)


def test_lifecycle_preserves_unmanaged_rules_and_replaces_managed_rule(monkeypatch) -> None:
    keep = {"ID": "archive-cold-output", "Status": "Enabled", "Expiration": {"Days": 30}}
    stale_managed = {
        "ID": provision_oss_cors._STAGING_RULE_ID,
        "Status": "Enabled",
        "Expiration": {"Days": 7},
    }
    client = FakeLifecycleClient([keep, stale_managed])
    _install_client(monkeypatch, client)

    provision_oss_cors._ensure_staging_lifecycle(_config())

    rules = client.put_calls[0]["LifecycleConfiguration"]["Rules"]
    assert rules[0] == keep
    assert rules[1] == {
        "ID": provision_oss_cors._STAGING_RULE_ID,
        "Filter": {"Prefix": "incoming/uploads/"},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }


def test_lifecycle_handles_bucket_without_existing_configuration(monkeypatch) -> None:
    client = FakeLifecycleClient(error=NoLifecycleError("NoSuchLifecycleConfiguration"))
    _install_client(monkeypatch, client)

    provision_oss_cors._ensure_staging_lifecycle(_config())

    assert len(client.put_calls[0]["LifecycleConfiguration"]["Rules"]) == 1


def test_lifecycle_does_not_hide_auth_or_network_errors(monkeypatch) -> None:
    client = FakeLifecycleClient(error=NoLifecycleError("AccessDenied"))
    _install_client(monkeypatch, client)

    with pytest.raises(NoLifecycleError):
        provision_oss_cors._ensure_staging_lifecycle(_config())
