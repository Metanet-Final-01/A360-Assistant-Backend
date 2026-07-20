"""레거시 PPT(.ppt, OLE 이진) 파싱 — LibreOffice로 PPTX 변환 후 기존 파서 재사용.

python-pptx는 OOXML(.pptx)만 읽고 구버전 이진 .ppt는 못 읽는다. LibreOffice(soffice)를
헤드리스로 돌려 .ppt → .pptx 변환한 뒤 parse_pptx에 넘긴다. PDFBox 폴백과 같은
"선택적 외부 도구" 패턴: soffice가 없으면 명확한 RuntimeError를 던져(→ /parse SSE가
사용자에게 "PPTX로 저장 후 업로드" 안내) 앱 자체는 죽지 않는다.

배포 Docker에는 LibreOffice를 포함해야 실제 변환이 된다(LIBREOFFICE_PATH로 경로 지정 가능).
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services.parser.pptx import parse_pptx

logger = logging.getLogger(__name__)


def _find_soffice() -> str | None:
    explicit = os.getenv("LIBREOFFICE_PATH", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_ppt_to_pptx(content: bytes) -> bytes:
    """soffice 헤드리스로 .ppt → .pptx 변환. 미설치·실패 시 RuntimeError."""
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError(
            "레거시 PPT 변환에는 LibreOffice가 필요합니다. 파일을 PPTX로 저장해 다시 업로드해 주세요."
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.ppt")
        with open(src, "wb") as f:
            f.write(content)
        # 호출마다 별도 UserInstallation 프로필로 격리 — soffice 기본 프로필은 하나뿐이라
        # 동시 변환 시 프로필 락 충돌로 실패할 수 있다.
        profile_uri = Path(os.path.join(tmp, "profile")).as_uri()
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--convert-to",
                    "pptx",
                    "--outdir",
                    tmp,
                    src,
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as e:
            raise RuntimeError(f"PPT→PPTX 변환에 실패했습니다: {e}") from e
        out = os.path.join(tmp, "in.pptx")
        if not os.path.exists(out):
            raise RuntimeError("PPT→PPTX 변환 결과 파일을 찾지 못했습니다.")
        with open(out, "rb") as f:
            return f.read()


def parse_ppt(content: bytes) -> dict:
    result = parse_pptx(_convert_ppt_to_pptx(content))
    result["parser"] = "libreoffice+python-pptx"
    return result
