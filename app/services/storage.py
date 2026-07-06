"""업로드 파일 저장소 — DOCUMENT_BUCKET이 설정되면 S3, 아니면 로컬 디스크.

배포 환경(EC2 ASG)은 인스턴스 교체 시 디스크가 사라지므로 S3를 쓰고,
로컬 개발은 UPLOAD_DIR(기본 data/uploads, gitignore됨)에 저장한다.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def save(session_id: str, doc_id: str, filename: str, content: bytes) -> str:
    """저장 후 storage_path를 반환한다 (s3://... 또는 로컬 경로)."""
    key = f"documents/{session_id}/{doc_id}/{filename}"
    bucket = os.getenv("DOCUMENT_BUCKET", "").strip()
    if bucket:
        import boto3  # 로컬 개발에서는 불필요하므로 지연 import

        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=content)
        return f"s3://{bucket}/{key}"

    base = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
    path = base / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def load(storage_path: str) -> bytes:
    """save()가 반환한 storage_path에서 파일 원본을 읽는다."""
    if storage_path.startswith("s3://"):
        import boto3

        bucket, _, key = storage_path.removeprefix("s3://").partition("/")
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    return Path(storage_path).read_bytes()
