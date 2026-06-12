# Aliyun OSS ObjectStore

Use the S3-compatible ObjectStore backend when generated media must be reachable
by cloud providers such as DashScope ASR. For Aliyun OSS, use virtual-hosted
style addressing and the OSS region endpoint:

```bash
export CUTAGENT_OBJECTSTORE_BACKEND=s3
export CUTAGENT_OBJECTSTORE_ENDPOINT=https://oss-cn-<region>.aliyuncs.com
export CUTAGENT_OBJECTSTORE_BUCKET=<bucket>
export CUTAGENT_OBJECTSTORE_ACCESS_KEY=<access-key-id>
export CUTAGENT_OBJECTSTORE_SECRET_KEY=<access-key-secret>
export CUTAGENT_OBJECTSTORE_REGION=oss-cn-<region>
export CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE=virtual
export CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB=8
export CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB=8
export CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY=4
export CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT=10
export CUTAGENT_OBJECTSTORE_READ_TIMEOUT=120
export CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS=5
```

Example for Shanghai:

```bash
export CUTAGENT_OBJECTSTORE_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com
export CUTAGENT_OBJECTSTORE_REGION=oss-cn-shanghai
export CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE=virtual
```

The bucket can remain private. Genesis writes artifacts to OSS and passes
presigned HTTPS URLs to ASR, so DashScope can download the TTS audio and return
real word or sentence timestamps. With this configuration, `strict_timestamps`
can use true ASR alignment for subtitles instead of estimated local timings.

MinIO remains the default local S3-compatible target. Leave
`CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE` unset, or set it to `path`, for MinIO.

## Shared ephemeral tier for workers

The tiered ObjectStore keeps finished video, cover, subtitle, ASR audio, and
other durable artifacts in the durable backend above. Transitional render
artifacts such as portrait tracks, lipsync video, and rendered timeline clips use
the ephemeral tier and are garbage-collected after a successful run.

The ephemeral tier must be reachable by every worker that may pick up the next
activity or resume the run. A per-worker local `/tmp` directory is safe only for
single-host worker deployments. In multi-host Temporal worker deployments, the
next activity can run on another worker and fail to read intermediate artifacts
written to the previous worker's local disk. Use a shared local volume/NFS mount
or a local-network MinIO/S3 bucket for ephemeral storage.

Local single-host default:

```bash
export CUTAGENT_OBJECTSTORE_TIERED=1
export CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=local
export CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH=/tmp/cutagent-ephemeral
```

Shared MinIO/S3 ephemeral tier example:

```bash
export CUTAGENT_OBJECTSTORE_TIERED=1
export CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=s3
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT=http://127.0.0.1:9000
export CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET=cutagent-ephemeral
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY=<minio-access-key>
export CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY=<minio-secret-key>
export CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION=us-east-1
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE=path
```

Keep the ephemeral bucket name different from the durable bucket name. The
tiered store routes `put`, `get`, `exists`, `signed_url`, and `delete` by the
bucket embedded in the object URI, so durable OSS and ephemeral MinIO can both
use the S3 backend as long as their buckets are distinct.

## Multipart transfer tuning

Remote OSS is practical for rendered media only when uploads use multipart
transfer. Portrait tracks, rendered clips, and final videos are commonly larger
than a single fast request over a distant OSS endpoint. Keep
`CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB` at or below the expected video
artifact size so boto3 switches to managed multipart upload automatically.

The default transfer settings are:

```bash
export CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB=8
export CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB=8
export CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY=4
```

For slower links, tune request timeouts and retry attempts instead of relying
on Temporal retries around a single long object request:

```bash
export CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT=10
export CUTAGENT_OBJECTSTORE_READ_TIMEOUT=120
export CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS=5
```
