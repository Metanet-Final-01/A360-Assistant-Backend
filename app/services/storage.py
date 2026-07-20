"""업로드 파일 저장소 — DOCUMENT_BUCKET이 설정되면 S3, 아니면 로컬 디스크.

배포 환경(EC2 ASG)은 인스턴스 교체 시 디스크가 사라지므로 S3를 쓰고,
로컬 개발은 UPLOAD_DIR(기본 data/uploads, gitignore됨)에 저장한다.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_name(filename: str) -> str:
    """사용자 제어 filename을 마지막 경로 요소로 축소해 경로 순회(../)를 차단한다.

    filename은 업로드 요청의 file.filename에서 오고 validate_upload는 확장자·매직바이트만
    검사하므로, '../../x'·'..\\x' 같은 값이 그대로 넘어온다. 이를 base / key로 조합하면
    UPLOAD_DIR(또는 S3 key 프리픽스) 밖으로 탈출할 수 있다. os.path.basename만으론 POSIX에서
    '\\'를 구분자로 안 보므로, '\\'를 '/'로 먼저 바꿔 Windows 구분자까지 벗긴다. basename은
    '..'·'.'을 그대로 돌려주므로(빈 문자열이 아님) 이들까지 명시적으로 폴백해 상위 경로 탈출을 막는다.
    """
    name = os.path.basename(filename.replace("\\", "/"))
    if name in ("", ".", ".."):
        return "unnamed"
    return name


def save(session_id: str, doc_id: str, filename: str, content: bytes) -> str:
    """저장 후 storage_path를 반환한다 (s3://... 또는 로컬 경로).

    filename은 저장 경로/S3 key 구성 전에 _safe_name으로 정제한다(경로 순회 차단). 원본
    filename은 표시용 메타데이터로 documents.filename에 따로 보존되므로 여기서 축소해도 무방하다.
    """
    key = f"documents/{session_id}/{doc_id}/{_safe_name(filename)}"
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
