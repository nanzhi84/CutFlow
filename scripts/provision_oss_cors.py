"""Provision OSS bucket CORS + a staging-prefix lifecycle rule for browser-direct uploads.

Browser uploads PUT directly to the durable bucket's ``incoming/uploads/`` staging
prefix, so that bucket needs CORS rules allowing the web origins. This script also
adds a lifecycle rule that auto-expires abandoned staging objects (a browser PUT that
never reached ``complete``). It is idempotent — safe to re-run — and must be run once
per environment (dev / prod) before browser uploads will work.

Run (with the deployment's CUTAGENT_OBJECTSTORE_* + CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS):

    python scripts/provision_oss_cors.py
"""

from __future__ import annotations

from packages.core.config import build_object_store_settings, build_settings
from packages.core.storage.object_store_env import object_store_from_env

_STAGING_PREFIX = "incoming/uploads/"
_STAGING_RULE_ID = "expire-abandoned-upload-staging"


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

    _ensure_staging_lifecycle(cfg)
    print(
        f"Lifecycle provisioned on {cfg.bucket!r}: objects under {_STAGING_PREFIX!r} "
        "expire and incomplete multipart uploads abort after 1 day."
    )
    return 0


def _ensure_staging_lifecycle(cfg) -> None:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.config import Config

    s3 = cfg.s3
    client = boto3.client(
        "s3",
        endpoint_url=s3.endpoint_url,
        aws_access_key_id=s3.access_key,
        aws_secret_access_key=s3.secret_key,
        region_name=s3.region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": s3.addressing_style}),
    )
    try:
        current = client.get_bucket_lifecycle_configuration(Bucket=cfg.bucket)
        existing_rules = list(current.get("Rules", []))
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
            raise
        existing_rules = []
    managed_rule = {
        "ID": _STAGING_RULE_ID,
        "Filter": {"Prefix": _STAGING_PREFIX},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }
    rules = [rule for rule in existing_rules if rule.get("ID") != _STAGING_RULE_ID]
    rules.append(managed_rule)
    client.put_bucket_lifecycle_configuration(
        Bucket=cfg.bucket,
        LifecycleConfiguration={"Rules": rules},
    )


if __name__ == "__main__":
    raise SystemExit(main())
