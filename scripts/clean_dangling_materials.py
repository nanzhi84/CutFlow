#!/usr/bin/env python3
"""Delete media_assets whose source object does NOT exist in object storage
(dangling references — e.g. broll/bgm bytes that were never synced to OSS).
Removes the asset, its annotations (FK cascade) and its selection-ledger rows.
Orphan source-artifact rows are LEFT in place (cheap, and deleting them risks
FK violations from other reference columns). Dry-run by default; --apply to execute.

Safety: only a genuine not-found (404 / NoSuchKey) counts as dangling — any other
OSS error aborts the run; and --apply refuses to proceed if an implausibly large
fraction of assets is flagged (a likely credential/region/endpoint misconfig).

  python scripts/clean_dangling_materials.py --apply   # CUTAGENT_OBJECTSTORE_* sourced
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError

NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
# Refuse to mass-delete if more than this fraction is flagged (config/outage guard).
MAX_DANGLING_FRACTION = 0.5


def log(m: str) -> None:
    print(m, flush=True)


def normalize_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://")


def oss_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CUTAGENT_OBJECTSTORE_ENDPOINT"],
        aws_access_key_id=os.environ["CUTAGENT_OBJECTSTORE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["CUTAGENT_OBJECTSTORE_SECRET_KEY"],
        region_name=os.environ.get("CUTAGENT_OBJECTSTORE_REGION", "oss-cn-shanghai"),
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": os.environ.get("CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE", "virtual")},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def object_missing(cli, uri: str) -> bool:
    """True only if the object is genuinely absent. Any other error re-raises so a
    transient/auth/region failure can never be mistaken for 'dangling'."""
    bucket, _, key = uri[len("s3://"):].partition("/")
    try:
        cli.head_object(Bucket=bucket, Key=key)
        return False
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code"))
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in NOT_FOUND_CODES or status == 404:
            return True
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get(
        "CUTAGENT_DATABASE_URL", "postgresql://cutagent:cutagent@127.0.0.1:55432/cutagent"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn = normalize_dsn(args.dsn)
    cli = oss_client()
    endpoint = os.environ.get("CUTAGENT_OBJECTSTORE_ENDPOINT")
    log(f"[{'APPLY' if args.apply else 'DRY-RUN'}] db={dsn.split('@')[-1]}  oss={endpoint}")

    conn = psycopg.connect(dsn)
    cur = conn.cursor()
    cur.execute("""
        SELECT m.id, m.title, m.kind, a.id, a.uri, a.oss_uri
        FROM media_assets m JOIN artifacts a ON a.id = m.source_artifact_id
    """)
    rows = cur.fetchall()
    checked = 0
    dangling = []  # asset_ids
    for asset_id, title, kind, art_id, uri, oss_uri in rows:
        ref = uri if (uri or "").startswith("s3://") else (oss_uri if (oss_uri or "").startswith("s3://") else None)
        if not ref:
            continue
        checked += 1
        if object_missing(cli, ref):
            dangling.append(asset_id)
            log(f"  DANGLING {kind:14} {str(title)[:32]:32} -> {ref.split('//',1)[-1][:56]}")
    log(f"checked {checked} OSS-backed assets; dangling={len(dangling)}")
    if not dangling:
        log("nothing to clean.")
        return 0

    frac = len(dangling) / max(checked, 1)
    if args.apply and frac > MAX_DANGLING_FRACTION:
        log(f"ABORT: {frac:.0%} of assets flagged dangling (> {MAX_DANGLING_FRACTION:.0%}) — "
            f"refusing to mass-delete; check OSS credentials/region/endpoint.")
        return 1

    if args.apply:
        cur.execute("DELETE FROM annotations WHERE asset_id = ANY(%s)", (dangling,))
        ann = cur.rowcount
        cur.execute("DELETE FROM selection_ledger WHERE asset_id = ANY(%s)", (dangling,))
        sl = cur.rowcount
        cur.execute("DELETE FROM selection_reservations WHERE asset_id = ANY(%s)", (dangling,))
        sr = cur.rowcount
        cur.execute("DELETE FROM media_assets WHERE id = ANY(%s)", (dangling,))
        ma = cur.rowcount
        conn.commit()
        log(f"deleted: media_assets={ma} annotations={ann} selection_ledger={sl} "
            f"selection_reservations={sr} (orphan source artifacts left in place)")
    else:
        log(f"DRY-RUN: would delete {len(dangling)} assets + their annotations/selection rows")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
