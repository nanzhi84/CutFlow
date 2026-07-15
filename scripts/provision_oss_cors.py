"""Provision OSS CORS and upload-cleanup lifecycle rules.

Browser uploads PUT directly to the durable bucket's ``incoming/uploads/`` staging
prefix, so that bucket needs CORS rules allowing the web origins. Lifecycle rules
expire abandoned browser staging objects and abort incomplete multipart uploads in
both durable and ephemeral S3 buckets. Existing unrelated lifecycle rules are kept.

Run (with the deployment's CUTAGENT_OBJECTSTORE_* + CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS):

    python scripts/provision_oss_cors.py
"""

from __future__ import annotations

from packages.core.config import build_object_store_settings, build_settings
from packages.core.storage.object_store_env import object_store_from_env

_STAGING_PREFIX = "incoming/uploads/"
_STAGING_RULE_ID = "expire-abandoned-upload-staging"
_MULTIPART_RULE_ID = "abort-incomplete-multipart-all"


def main() -> int:
    cfg = build_object_store_settings()
    if cfg.backend != "s3":
        print(f"Object store backend is {cfg.backend!r}; CORS provisioning only applies to s3/OSS.")
        return 0

    origins = list(build_settings().upload.cors_allowed_origins)
    if not origins:
        print(
            "Refusing to provision: CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS is empty. "
            "Set it to the comma-separated web origins that may upload directly."
        )
        return 1

    store = object_store_from_env()
    store.ensure_cors(origins)
    print(f"CORS provisioned on durable bucket {cfg.bucket!r}: AllowedOrigins={origins}")

    buckets = _ensure_upload_lifecycles(cfg)
    print(
        f"Lifecycle provisioned on {buckets}: objects under {_STAGING_PREFIX!r} expire "
        "after 1 day; incomplete multipart uploads abort after 1 day."
    )
    return 0


def _ensure_upload_lifecycles(cfg) -> list[str]:
    import boto3
    from botocore.config import Config

    def client_for(settings):
        return boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": settings.addressing_style},
            ),
        )

    abort_all = {
        "ID": _MULTIPART_RULE_ID,
        "Filter": {"Prefix": ""},
        "Status": "Enabled",
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }
    durable_client = client_for(cfg.s3)
    _upsert_lifecycle_rules(
        durable_client,
        cfg.bucket,
        [
            {
                "ID": _STAGING_RULE_ID,
                "Filter": {"Prefix": _STAGING_PREFIX},
                "Status": "Enabled",
                "Expiration": {"Days": 1},
            },
            abort_all,
        ],
    )
    buckets = [cfg.bucket]
    if cfg.tiered and cfg.ephemeral.backend == "s3":
        _upsert_lifecycle_rules(client_for(cfg.ephemeral), cfg.ephemeral.bucket, [abort_all])
        buckets.append(cfg.ephemeral.bucket)
    return buckets


def _upsert_lifecycle_rules(client, bucket: str, replacements: list[dict]) -> None:
    from botocore.exceptions import ClientError

    try:
        current = client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code"))
        if code not in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
            raise
        current = []
    replacement_ids = {rule["ID"] for rule in replacements}
    rules = [rule for rule in current if rule.get("ID") not in replacement_ids]
    rules.extend(replacements)
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": rules},
    )


if __name__ == "__main__":
    raise SystemExit(main())
