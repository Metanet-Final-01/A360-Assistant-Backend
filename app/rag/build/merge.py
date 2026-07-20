"""수집한 문서(docs.jsonl)·패키지 스키마(packages.json)를 RAG 문서로 병합한다.

RAG 문서 단위:
- action_schema: 액션 하나당 하나. JAR의 공식 스키마(metadata.schema_source="jar").
  추천 메뉴(BackendCatalog)가 조회하는 유일한 source_type이다.
- action_candidate: JAR이 없는 패키지의 리프 문서를 액션 후보로만 나열한 것
  (`naive_leaf_actions.py` 산출, metadata.schema_source="naive_leaf_action"). 파라미터
  스키마를 모르는 미검증 후보라 추천 메뉴엔 안 뜨고 search_kb 일반 질의응답에만 쓰인다.
- package_overview: 패키지 하나당 하나. 액션 목록 요약.
- doc_page: 크롤링한 문서 페이지 하나당 하나.
- bot_example: Control Room에서 수집한 실제 봇 하나당 하나 (액션 조합 예시).

JAR이 없는 패키지의 리프가 "진짜 액션인지 참고/예제 문서인지"와 그 파라미터 스키마
추출은 팀 결정(2026-07-10)으로 규칙 기반 파싱이 아니라 별도의 LLM 기반 파싱 Agent가
담당한다 — `app/rag/pipeline.py`의 `export-for-agent` 명령이 그 Agent가 쓸 입력을
준비한다(`app/rag/_investigation_notes/HTML_STRUCTURE_INSIGHTS.md`,
`_archive_pre_agent_handoff/README.md` 참고). 그 Agent가 나오기 전 임시로 쓸 수 있는
가장 단순한 대안이 `action_candidate`(모든 리프를 필터링 없이 액션 후보로 취급)다.

chunk_size를 넘는 문서는 여러 row로 쪼개지며 `parent_id`(원 문서 id)와 `chunk_index`(0부터)를 갖는다.
안 쪼개진 문서도 스키마 일관성을 위해 `parent_id=id`, `chunk_index=0`을 갖는다.

패키지 안에 같은 액션 이름이 두 번 있으면(실측: 커뮤니티 WebAutomation JAR 자체의
데이터 문제, 파라미터 개수가 다른 두 버전이 섞여 있었음) id가 겹쳐 여기서 바로
에러가 난다 — 그건 의도된 동작이다. 정본 판단(어느 버전을 쓸지)은 원본 JAR을 다시
읽는 `jar_parser.py::_dedupe_actions_by_name`이 파싱 시점에 처리하므로, 여기 도달할
때는 이미 패키지당 액션 이름이 유일해야 한다. 그런데도 겹친다면 새로운 데이터
문제이니 조용히 넘기지 않고 바로 터뜨린다.
"""

import hashlib
import json
from pathlib import Path

from .chunk import chunk_text
from .doc_action_match import match_action_doc, match_package_docs


def _doc_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def load_docs(docs_path: Path) -> list[dict]:
    docs = []
    if docs_path.exists():
        with open(docs_path, encoding="utf-8") as f:
            for line in f:
                docs.append(json.loads(line))
    return docs


def _format_parameters(parameters: list[dict]) -> str:
    lines = []
    for p in parameters:
        required = "필수" if p.get("required") else "선택"
        line = f"- {p.get('name')} ({p.get('type')}, {required})"
        desc = p.get("label") or p.get("description")
        if desc:
            line += f": {desc}"
        if "default" in p:
            line += f" (기본값: {json.dumps(p['default'], ensure_ascii=False)})"
        if "options" in p:
            opts = ", ".join(str(o.get("value") if isinstance(o, dict) else o) for o in p["options"])
            line += f" (선택지: {opts})"
        lines.append(line)
    return "\n".join(lines) if lines else "- 없음"


def load_bots(bots_path: Path) -> list[dict]:
    bots = []
    if bots_path.exists():
        with open(bots_path, encoding="utf-8") as f:
            for line in f:
                bots.append(json.loads(line))
    return bots


def _walk_bot_nodes(obj) -> list[dict]:
    """봇 JSON에서 (packageName, commandName)을 가진 노드를 재귀적으로 수집."""
    found = []
    if isinstance(obj, dict):
        if obj.get("commandName") and obj.get("packageName"):
            found.append(obj)
        for value in obj.values():
            found.extend(_walk_bot_nodes(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_bot_nodes(item))
    return found


def _summarize_bot(bot: dict) -> tuple[str, dict]:
    nodes = _walk_bot_nodes(bot.get("json", {}))
    steps = []
    packages_used: dict[str, set] = {}
    for node in nodes:
        pkg, cmd = node["packageName"], node["commandName"]
        packages_used.setdefault(pkg, set()).add(cmd)
        attrs = [a.get("name") for a in node.get("attributes", []) if isinstance(a, dict)]
        step = f"{pkg}.{cmd}"
        if attrs:
            step += f" (파라미터: {', '.join(str(a) for a in attrs)})"
        steps.append(step)

    content = (
        f"봇 이름: {bot.get('name')}\n"
        f"경로: {bot.get('path')}\n"
        f"사용 패키지: {', '.join(sorted(packages_used)) or '없음'}\n"
        f"액션 순서 ({len(steps)}단계):\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
    )
    metadata = {
        "file_id": bot.get("file_id"),
        "used_packages": {k: sorted(v) for k, v in packages_used.items()},
        "node_count": len(nodes),
    }
    return content, metadata


# doc_page는 산문(문단/문장 위주), 나머지는 merge.py가 조립한 "라벨: 값" 정형 텍스트
_CHUNK_STRATEGY_BY_SOURCE_TYPE = {"doc_page": "prose"}
_DEFAULT_CHUNK_STRATEGY = "structured"

# 이보다 짧은 선두 조각(대개 breadcrumb/헤더 줄만 남은 경우)은 다음 청크에 합쳐
# 저품질 단독 청크가 생기지 않게 한다.
_MIN_LEADING_CHUNK_CHARS = 150


def _split_document(doc: dict, chunk_size: int | None, chunk_overlap: int) -> list[dict]:
    if chunk_size is None:
        # chunk_size=None: 청킹 없이 원본 길이 그대로 (EDA가 청킹 전 분포를 보기 위해 사용)
        return [{**doc, "parent_id": doc["id"], "chunk_index": 0}]

    strategy = _CHUNK_STRATEGY_BY_SOURCE_TYPE.get(doc["source_type"], _DEFAULT_CHUNK_STRATEGY)
    parts = chunk_text(doc["content"], chunk_size, chunk_overlap, strategy=strategy)

    if len(parts) > 1 and len(parts[0]) < _MIN_LEADING_CHUNK_CHARS:
        parts = [parts[0] + "\n\n" + parts[1]] + parts[2:]

    # 어떤 설정으로 쪼개졌는지 metadata에 남겨서, 나중에 다른 chunk_size/전략을 실험할 때
    # "이 row가 어떤 실행에서 나온 건지" 추적할 수 있게 한다.
    def _with_chunk_meta(base_doc: dict, chunk_count: int) -> dict:
        return {
            **base_doc,
            "metadata": {
                **base_doc.get("metadata", {}),
                "chunk_strategy": strategy,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "chunk_count": chunk_count,
            },
        }

    if len(parts) <= 1:
        merged = _with_chunk_meta(doc, chunk_count=1)
        return [{**merged, "parent_id": doc["id"], "chunk_index": 0, "content": parts[0] if parts else doc["content"]}]

    # 청크가 여러 개면 각 청크 맨 앞에 문서 제목을 붙여 문맥을 유지한다 —
    # 그러지 않으면 뒤쪽 청크들은 어느 문서 소속인지 알 길이 없는 본문 조각만 남는다.
    return [
        {
            **_with_chunk_meta(doc, chunk_count=len(parts)),
            "id": _doc_id(doc["id"], str(index)),
            "parent_id": doc["id"],
            "chunk_index": index,
            "content": f"{doc['title']}\n\n{part}",
        }
        for index, part in enumerate(parts)
    ]


def build_rag_documents(
    packages: list[dict],
    docs: list[dict],
    locale: str,
    bots: list[dict] | None = None,
    naive_leaf_actions: list[dict] | None = None,
    chunk_size: int | None = 1200,
    chunk_overlap: int = 200,
) -> list[dict]:
    rag_docs: list[dict] = []
    matched_doc_ids: set[str] = set()
    existing_package_names = {p["package_name"] for p in packages}

    for package in packages:
        package_docs = match_package_docs(package, docs)
        action_names = [a.get("label") or a.get("name") for a in package["actions"]]

        rag_docs.append(
            {
                "id": _doc_id("package", package["package_name"], package.get("package_version") or ""),
                "source_type": "package_overview",
                "package_name": package["package_name"],
                "action_name": None,
                "locale": locale,
                "title": f"{package['package_label'] or package['package_name']} 패키지",
                "url": package_docs[0]["url"] if package_docs else "",
                "content": (
                    f"패키지: {package['package_label'] or package['package_name']}\n"
                    f"버전: {package.get('package_version')}\n"
                    f"설명: {package.get('package_description')}\n"
                    f"포함된 액션 ({len(action_names)}개): {', '.join(str(n) for n in action_names)}"
                ),
                "metadata": {"package_version": package.get("package_version")},
            }
        )

        for action in package["actions"]:
            action_doc = match_action_doc(action, package_docs)
            if action_doc:
                matched_doc_ids.add(action_doc["content_id"])

            content = (
                f"패키지: {package['package_label'] or package['package_name']}\n"
                f"액션: {action.get('label') or action.get('name')} ({action.get('name')})\n"
                f"설명: {action.get('description')}\n"
                f"파라미터:\n{_format_parameters(action['parameters'])}\n"
                f"리턴: {action.get('return_type')}"
                + (f" ({action.get('return_label')})" if action.get("return_label") else "")
            )
            if action_doc:
                content += f"\n\n공식 문서 설명:\n{action_doc['text']}"

            rag_docs.append(
                {
                    "id": _doc_id("action", package["package_name"], str(action.get("name"))),
                    "source_type": "action_schema",
                    "package_name": package["package_name"],
                    "action_name": action.get("name"),
                    "locale": locale,
                    "title": f"{package['package_label'] or package['package_name']} - {action.get('label') or action.get('name')}",
                    "url": action_doc["url"] if action_doc else "",
                    "content": content,
                    "metadata": {
                        "package_version": package.get("package_version"),
                        "schema": action,
                        "schema_source": "jar",
                    },
                }
            )

    # 리프=액션 베이스라인(app/rag/build/naive_leaf_actions.py 산출) — 순수 추가.
    # packages.json에 이미 있는 패키지는 JAR 쪽이 항상 우선이라 절대 안 건드린다.
    # 파라미터 스키마를 모르는 미검증 후보라 action_schema가 아니라 action_candidate로
    # 남긴다(추천 메뉴엔 안 뜨고 search_kb 일반 질의응답에만 쓰임).
    for naive in naive_leaf_actions or []:
        pkg_name = naive["package_name"]
        if pkg_name in existing_package_names:
            continue
        rag_docs.append(
            {
                "id": _doc_id("naive_action", pkg_name, naive.get("url") or naive["title"]),
                "source_type": "action_candidate",
                "package_name": pkg_name,
                "action_name": None,
                "locale": locale,
                "title": f"{pkg_name} - {naive['title']}",
                "url": naive.get("url") or "",
                "content": (
                    f"패키지: {pkg_name}\n"
                    f"경로: {' > '.join(naive.get('path_titles', []))}\n"
                    f"(참고: 파라미터 스키마 미확인 — 리프 문서를 액션 후보로만 나열한 것)"
                ),
                "metadata": {
                    "schema_source": "naive_leaf_action",
                    "path_titles": naive.get("path_titles", []),
                },
            }
        )

    for bot in bots or []:
        content, metadata = _summarize_bot(bot)
        rag_docs.append(
            {
                "id": _doc_id("bot", str(bot.get("file_id"))),
                "source_type": "bot_example",
                "package_name": None,
                "action_name": None,
                "locale": locale,
                "title": f"봇 예시: {bot.get('name')}",
                "url": "",
                "content": content,
                "metadata": metadata,
            }
        )

    for doc in docs:
        rag_docs.append(
            {
                "id": _doc_id("doc", doc["content_id"]),
                "source_type": "doc_page",
                "package_name": None,
                "action_name": None,
                "locale": locale,
                "title": doc["title"],
                "url": doc.get("url", ""),
                "content": " > ".join(doc["breadcrumbs"]) + "\n\n" + doc["text"],
                "metadata": {
                    "breadcrumbs": doc["breadcrumbs"],
                    "matched_to_action": doc["content_id"] in matched_doc_ids,
                },
            }
        )

    _assert_no_duplicate_ids(rag_docs)

    return [
        chunk
        for doc in rag_docs
        for chunk in _split_document(doc, chunk_size, chunk_overlap)
    ]


def _assert_no_duplicate_ids(rag_docs: list[dict]) -> None:
    """id는 DB의 primary key라 겹치면 ON CONFLICT(id) upsert가 하나를 조용히 덮어써
    데이터가 유실된다. 청킹 전 최상위 문서 id 기준으로 검사한다 — 청크 id는 항상
    고유한 parent id에서 파생되므로 여기서 겹치지 않으면 청킹 후에도 겹치지 않는다.
    """
    seen: dict[str, dict] = {}
    duplicates = []
    for doc in rag_docs:
        prior = seen.get(doc["id"])
        if prior is not None:
            duplicates.append(
                f"id={doc['id']} package={doc.get('package_name')!r} action={doc.get('action_name')!r} "
                f"title={doc['title']!r} (기존 title={prior['title']!r})"
            )
        else:
            seen[doc["id"]] = doc
    if duplicates:
        raise ValueError("중복 id 발견:\n" + "\n".join(duplicates))
