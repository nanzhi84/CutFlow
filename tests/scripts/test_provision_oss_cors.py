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

    def put_bucket_lifecycle_configuration(self, **kwargs) -> None:
        self.put_calls.append(kwargs)


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}},
        "GetBucketLifecycleConfiguration",
    )


def _s3_settings(bucket: str):
    return SimpleNamespace(
        backend="s3",
        bucket=bucket,
        endpoint_url="https://oss.example.test",
        access_key="access",
        secret_key="secret",
        region_name="us-east-1",
        addressing_style="path",
    )


def test_lifecycle_upsert_preserves_unrelated_rules_and_replaces_managed() -> None:
    keep = {"ID": "archive-cold-output", "Status": "Enabled"}
    stale = {"ID": provision_oss_cors._MULTIPART_RULE_ID, "Status": "Disabled"}
    client = FakeLifecycleClient([keep, stale])
    replacement = {
        "ID": provision_oss_cors._MULTIPART_RULE_ID,
        "Filter": {"Prefix": ""},
        "Status": "Enabled",
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }

    provision_oss_cors._upsert_lifecycle_rules(client, "outputs", [replacement])

    assert client.put_calls == [
        {
            "Bucket": "outputs",
            "LifecycleConfiguration": {"Rules": [keep, replacement]},
        }
    ]


@pytest.mark.parametrize("code", ["NoSuchLifecycleConfiguration", "NoSuchLifecycle"])
def test_lifecycle_upsert_handles_missing_configuration(code: str) -> None:
    client = FakeLifecycleClient(error=_client_error(code))
    replacement = {"ID": "managed", "Status": "Enabled"}

    provision_oss_cors._upsert_lifecycle_rules(client, "outputs", [replacement])

    assert client.put_calls[0]["LifecycleConfiguration"]["Rules"] == [replacement]


def test_lifecycle_upsert_does_not_hide_auth_errors() -> None:
    client = FakeLifecycleClient(error=_client_error("AccessDenied"))

    with pytest.raises(ClientError):
        provision_oss_cors._upsert_lifecycle_rules(
            client,
            "outputs",
            [{"ID": "managed", "Status": "Enabled"}],
        )


def test_upload_lifecycles_cover_durable_and_ephemeral_s3(monkeypatch) -> None:
    durable = FakeLifecycleClient()
    ephemeral = FakeLifecycleClient()
    clients = iter([durable, ephemeral])
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: next(clients))
    cfg = SimpleNamespace(
        bucket="durable",
        s3=_s3_settings("durable"),
        tiered=True,
        ephemeral=_s3_settings("ephemeral"),
    )

    buckets = provision_oss_cors._ensure_upload_lifecycles(cfg)

    assert buckets == ["durable", "ephemeral"]
    durable_rules = durable.put_calls[0]["LifecycleConfiguration"]["Rules"]
    ephemeral_rules = ephemeral.put_calls[0]["LifecycleConfiguration"]["Rules"]
    assert {rule["ID"] for rule in durable_rules} == {
        provision_oss_cors._STAGING_RULE_ID,
        provision_oss_cors._MULTIPART_RULE_ID,
    }
    assert [rule["ID"] for rule in ephemeral_rules] == [
        provision_oss_cors._MULTIPART_RULE_ID
    ]
