from __future__ import annotations

import os
from io import BytesIO
from urllib.parse import urlparse

from minio import Minio
from minio.credentials.providers import ChainedProvider, EnvAWSProvider, IamAwsProvider
from minio.error import S3Error
from urllib3 import PoolManager

from app.core.pipeline_control import task_run_id


class ImmutableObjectConflict(RuntimeError):
    """A deterministic publication key already contains different bytes."""


def _endpoint() -> str:
    endpoint = os.environ.get("S3_ENDPOINT") or "http://localhost:9000"
    parsed = urlparse(endpoint)
    return parsed.netloc or parsed.path


def _secure() -> bool:
    endpoint = os.environ.get("S3_ENDPOINT") or "http://localhost:9000"
    return endpoint.startswith("https://")


def bucket_name() -> str:
    return os.environ.get("S3_BUCKET") or "fruition-storage"


def client(http_client: PoolManager | None = None) -> Minio:
    mode = os.environ.get("S3_CREDENTIALS_MODE", "local")
    if mode == "aws":
        region = os.environ.get("AWS_REGION")
        if not region:
            raise ValueError("AWS S3 region이 필요합니다.")
        return Minio(
            _endpoint(),
            secure=_secure(),
            region=region,
            credentials=ChainedProvider(
                [EnvAWSProvider(), IamAwsProvider(http_client=http_client)]
            ),
            http_client=http_client,
        )
    if mode != "local":
        raise ValueError("지원하지 않는 S3 credentials mode입니다.")
    return Minio(
        _endpoint(),
        access_key=os.environ.get("S3_ACCESS_KEY") or "fruition",
        secret_key=os.environ.get("S3_SECRET_KEY") or "fruition_dev_secret",
        secure=_secure(),
        http_client=http_client,
    )


def split_storage_uri(uri: str) -> tuple[str, str]:
    if uri.startswith("s3://"):
        parsed = urlparse(uri)
        return parsed.netloc, parsed.path.lstrip("/")
    return bucket_name(), uri.lstrip("/")


def storage_uri(object_name: str, bucket: str | None = None) -> str:
    return f"s3://{bucket or bucket_name()}/{object_name.lstrip('/')}"


def read_text_object(uri: str) -> str:
    bucket, object_name = split_storage_uri(uri)
    response = client().get_object(bucket, object_name)
    try:
        return response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()


def write_text_object(
    object_name: str,
    text: str,
    content_type: str = "text/markdown; charset=utf-8",
) -> str:
    bucket, key = split_storage_uri(object_name)
    data = text.encode("utf-8")
    minio_client = client()
    if os.environ.get("S3_CREDENTIALS_MODE", "local") == "local" and not minio_client.bucket_exists(bucket):
        minio_client.make_bucket(bucket)
    if task_run_id.get() is not None:
        from app.modules.task_cancellation.infrastructure.object_change_journal import change_object
        change_object(task_run_id.get(), minio_client, bucket, key, {"text": text, "content_type": content_type})
        return storage_uri(key, bucket)
    from app.modules.task_cancellation.infrastructure.object_change_journal import write_fence
    with write_fence(key, text):
        minio_client.put_object(
            bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
    return storage_uri(key, bucket)


def create_or_verify_text_object(
    object_name: str,
    text: str,
    content_type: str = "text/markdown; charset=utf-8",
) -> str:
    """Create an immutable UTF-8 object, or verify an identical retry."""
    from app.modules.task_cancellation.infrastructure import postgres_task_journal as journal
    from app.modules.task_cancellation.infrastructure.object_change_journal import snapshot, write_fence

    bucket, key = split_storage_uri(object_name)
    data = text.encode("utf-8")
    minio_client = client()
    if os.environ.get("S3_CREDENTIALS_MODE", "local") == "local" and not minio_client.bucket_exists(bucket):
        minio_client.make_bucket(bucket)
    with write_fence(key, text), journal.connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"ai-object:{bucket}/{key}",),
        )
        current = snapshot(minio_client, bucket, key)
        if current is not None:
            if current["text"] != text:
                raise ImmutableObjectConflict(f"immutable object differs: {key}")
            return storage_uri(key, bucket)
        minio_client.put_object(
            bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
    return storage_uri(key, bucket)


def delete_object(object_name: str) -> None:
    bucket, key = split_storage_uri(object_name)
    if task_run_id.get() is not None:
        from app.modules.task_cancellation.infrastructure.object_change_journal import change_object
        change_object(task_run_id.get(), client(), bucket, key, None)
        return
    client().remove_object(bucket, key)


def is_missing_object_error(exc: Exception) -> bool:
    return isinstance(exc, S3Error) and exc.code in {"NoSuchKey", "NoSuchObject"}


def pipeline_log_uri(run_id: str) -> str:
    return storage_uri(f"pipeline-runs/{run_id}/pipeline.log")


def write_pipeline_log(uri: str, text: str) -> None:
    """실행 진단 로그는 업무 변경 rollback 대상에 포함하지 않는다."""
    bucket, key = split_storage_uri(uri)
    data = text.encode("utf-8")
    client().put_object(
        bucket,
        key,
        BytesIO(data),
        length=len(data),
        content_type="text/plain; charset=utf-8",
    )
