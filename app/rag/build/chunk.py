"""문서 텍스트 청킹. LangChain의 RecursiveCharacterTextSplitter를 사용한다.

산문(prose)과 정형 텍스트(structured)는 구분자 우선순위를 다르게 준다 —
doc_page(크롤링한 문서 페이지)는 문단/문장 위주 산문이고,
action_schema/package_overview/bot_example(normalize.py가 조립한 텍스트)은
"라벨: 값" 줄이 반복되는 정형 텍스트라 필드 줄 경계를 우선한다.
"""

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

_SEPARATORS_BY_STRATEGY = {
    "prose": ["\n\n", "\n", ". ", "! ", "? ", "。", " ", ""],
    "structured": ["\n\n", "\n", ": ", ", ", " ", ""],
}


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\x0b\x0c ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, chunk_size: int, overlap: int, strategy: str = "prose") -> list[str]:
    """text를 chunk_size(글자 수) 근처에서 strategy에 맞는 구분자 우선순위로 분할한다.

    strategy: "prose"(문단/문장 우선, 기본) 또는 "structured"(필드/줄 우선).
    chunk_size 이하인 텍스트는 그대로 1개짜리 리스트로 반환한다 (분할 없음).
    """
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size/overlap 설정이 올바르지 않습니다")

    normalized = _normalize(text)
    if not normalized:
        return []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=_SEPARATORS_BY_STRATEGY[strategy],
        length_function=len,  # 글자 수 기준 — embed.py의 _MAX_CHARS와 동일한 단위로 통일
    )
    return splitter.split_text(normalized)
