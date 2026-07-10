"""수집 파이프라인 CLI.

사용 예:
  python -m app.rag.pipeline crawl --contains "Google Sheets"   # 문서 크롤링 (필터)
  python -m app.rag.pipeline crawl                               # 명령 패널(패키지 문서) 전체
  python -m app.rag.pipeline parse-jars path/to/export.zip jars_dir/
  python -m app.rag.pipeline bots                                # Control Room 봇 목록+JSON 수집
  python -m app.rag.pipeline export-packages --file-ids 123 456  # BLM export → JAR 스키마 자동 추출
  python -m app.rag.pipeline build-action-tree                    # 패키지 판별+메뉴 계층 전체를 package_action_tree.json으로 저장 (JAR 유무 무관)
  python -m app.rag.pipeline export-for-agent --packages Database  # JAR 없는 패키지 문서(구조화 HTML 포함) -> 향후 파싱 Agent용 산출물 (--packages 생략 시 발견된 전체 미커버 패키지)
  python -m app.rag.pipeline export-naive-leaf-actions             # 리프=액션 필터링 없이 전부 나열 (파라미터 없음, 빠른 훑어보기용)
  python -m app.rag.pipeline build                               # 문서+스키마+봇 → rag_documents.jsonl (청킹 포함)
  python -m app.rag.pipeline build --include-naive-leaf-actions   # 위와 동일 + JAR 없는 패키지 리프를 action_candidate로 포함
  python -m app.rag.pipeline eda                                  # 문서 길이 분포 분석 (청크 크기 결정용)
  python -m app.rag.pipeline ingest [--skip-embedding]           # pgvector 적재
  python -m app.rag.pipeline search "구글시트에서 시트 활성화 어떻게 해?"
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from .build.doc_action_tree import PackageActionTree


def cmd_crawl(args: argparse.Namespace) -> None:
    from .sources import docs_crawler as dc

    m = dc.find_map(locale=args.locale, title="Automation 360")
    print(f"map: {m['title']} ({args.locale}) id={m['id']}")
    menu = dc.get_menu(m["id"])
    topics = dc.flatten_menu(menu)

    if args.url_filter:
        topics = [t for t in topics if args.url_filter in t["pretty_url"]]
    if args.contains:
        needle = args.contains.lower()
        topics = [
            t
            for t in topics
            if needle in t["title"].lower()
            or any(needle in b.lower() for b in t["breadcrumbs"])
        ]
    print(f"대상 토픽: {len(topics)}개")

    def progress(i, total, title):
        print(f"  [{i}/{total}] {title}")

    out_path = config.docs_jsonl_for_locale(args.locale)
    written = dc.crawl_topics(m["id"], topics, out_path, on_progress=progress, locale=args.locale)
    print(f"저장: {written}개 신규 → {out_path}")


def _merge_into_packages_json(new_packages: list[dict]) -> dict[str, dict]:
    """새로 파싱한 패키지들을 기존 packages.json과 합친다. 같은 package_name이 이미
    있으면 버전을 비교해 더 높은 쪽만 채택한다(select_better_version) — 방금 파싱한
    쪽을 무조건 최신으로 보고 덮어쓰면, 이미 더 높은 버전이 packages.json에 있었는데
    이번 실행이 낮은 버전만 발견한 경우 오히려 퇴보한다.
    """
    from .sources.jar_parser import select_better_version

    existing: dict[str, dict] = {}
    if config.PACKAGES_JSON.exists():
        for pkg in json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8")):
            existing[pkg["package_name"]] = pkg
    for pkg in new_packages:
        name = pkg["package_name"]
        existing[name] = pkg if name not in existing else select_better_version(existing[name], pkg)
    return existing


def cmd_parse_jars(args: argparse.Namespace) -> None:
    from .sources.jar_parser import parse_packages

    packages = parse_packages([Path(p) for p in args.paths], preferred_locale=args.jar_locale)
    config.PACKAGES_JSON.parent.mkdir(parents=True, exist_ok=True)

    existing = _merge_into_packages_json(packages)
    config.PACKAGES_JSON.write_text(
        json.dumps(list(existing.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for pkg in packages:
        print(f"  {pkg['package_name']} v{pkg['package_version']}: 액션 {len(pkg['actions'])}개")
    print(f"저장: 패키지 총 {len(existing)}개 → {config.PACKAGES_JSON}")


def cmd_harvest_github(args: argparse.Namespace) -> None:
    import os

    from .sources.github_harvest import harvest
    from .sources.jar_parser import parse_packages

    token = os.getenv("GITHUB_TOKEN") or None
    stats = harvest(token=token, max_repos=args.max_repos)
    print(
        f"수집 완료: 저장소 {stats['repos']}개, 패키지 JAR {stats['jars']}개, "
        f"봇 {stats['bots']}개 (zip {stats['zips']}개)"
    )

    # 받은 JAR을 즉시 파싱해 packages.json에 병합
    jar_dir = Path(stats["jar_dir"])
    if any(jar_dir.glob("*.jar")):
        packages = parse_packages([jar_dir], preferred_locale=args.jar_locale)
        existing = _merge_into_packages_json(packages)
        config.PACKAGES_JSON.write_text(
            json.dumps(list(existing.values()), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"패키지 스키마: 총 {len(existing)}개 → {config.PACKAGES_JSON}")


def cmd_bots(args: argparse.Namespace) -> None:
    from .sources.control_room import ControlRoomClient

    client = ControlRoomClient()
    try:
        bots = client.list_bots(workspace=args.workspace)
        print(f"Task Bot {len(bots)}개 발견")
        config.BOTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(config.BOTS_JSONL, "w", encoding="utf-8") as f:
            for i, bot in enumerate(bots):
                record = {
                    "file_id": bot.get("id"),
                    "name": bot.get("name"),
                    "path": bot.get("path"),
                    "workspace": args.workspace,
                }
                try:
                    record["json"] = client.get_bot_json(bot["id"])
                except Exception as e:  # 권한 없는 봇 등은 건너뜀
                    print(f"  [skip] {bot.get('name')}: {e}")
                    continue
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"  [{i+1}/{len(bots)}] {bot.get('name')}")
        print(f"저장 → {config.BOTS_JSONL}")
    finally:
        client.close()


def cmd_export_packages(args: argparse.Namespace) -> None:
    from .sources.control_room import ControlRoomClient
    from .sources.jar_parser import parse_packages

    client = ControlRoomClient()
    try:
        print(f"BLM export 요청 (fileIds={args.file_ids}, 패키지 포함)...")
        zip_bytes = client.export_with_packages([int(x) for x in args.file_ids])
    finally:
        client.close()

    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = config.EXPORTS_DIR / "package-export.zip"
    zip_path.write_bytes(zip_bytes)
    print(f"다운로드 완료 ({len(zip_bytes)} bytes) → {zip_path}")

    args.paths = [str(zip_path)]
    args.jar_locale = getattr(args, "jar_locale", "ko_KR")
    cmd_parse_jars(args)


def _load_source_inputs(source: str) -> tuple[list[dict], list[dict], list[dict]]:
    """--source 선택에 따라 (packages, docs, bots) 중 해당 소스만 채워서 반환한다.

    "docs"(공식문서, Fluid Topics)와 "github"(패키지 JAR + 공개 봇)는 서로 독립적으로
    build/ingest할 수 있다 — 같은 rag_documents 테이블에 upsert되므로 나중에 합쳐도
    검색은 항상 통합된 하나의 인덱스로 유지된다.
    """
    from .build.merge import load_bots, load_docs

    docs = load_docs(config.DOCS_JSONL) if source in ("all", "docs") else []
    bots = load_bots(config.BOTS_JSONL) if source in ("all", "github") else []
    packages = (
        json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8"))
        if source in ("all", "github") and config.PACKAGES_JSON.exists()
        else []
    )
    return packages, docs, bots


def _discover_packages(docs: list[dict], en_docs: list[dict]) -> dict[str, "PackageActionTree"]:
    """루트 패키지("~패키지" 제목 + 메뉴 자식)마다 doc_action_tree로 전체 트리(사이트 메뉴의
    `parent_menu_id` 기반, 어느 깊이에 있든 모든 리프 문서 + 카테고리 경유 문서)를 만들고,
    진짜 영어 package_name으로 키를 바꾼다.

    영어 package_name은 en-US 크롤 결과에서 pretty_url로 페어링한 개요 페이지 제목
    ("Database package")에서 뽑는다 — 한국어 제목("데이터베이스 패키지")에서 뽑으면
    완전히 번역되는 패키지명이 bots.jsonl의 실제 표기와 안 맞는 문제가 있었다(실측
    확인된 버그). en 페어링에 실패하면(en 크롤이 없거나 누락) 한국어 제목에서라도
    접미어("패키지")를 떼어 폴백한다 — 부정확할 수 있지만 완전히 못 찾는 것보단 낫다.

    반환: {canonical_package_name: PackageActionTree}.
    """
    from .build.doc_action_match import canonical_package_name, pair_by_pretty_url
    from .build.doc_action_tree import build_all_trees

    trees = build_all_trees(docs)
    # 사이트 메뉴는 진짜 트리(노드마다 부모가 정확히 하나)라 이전 버전(본문 링크 기반)과 달리
    # 같은 리프를 두 루트가 동시에 주장하는 경우가 구조적으로 있을 수 없다 — 그래서
    # 여기엔 그런 충돌을 정리하는 단계가 없다.

    root_docs = [t.root_doc for t in trees]
    ko_to_en = pair_by_pretty_url(root_docs, en_docs)

    result: dict[str, PackageActionTree] = {}
    for tree in trees:
        en_doc = ko_to_en.get(tree.root_doc["content_id"])
        pkg_name = canonical_package_name(en_doc["title"] if en_doc else tree.root_doc["title"])
        if pkg_name:
            result[pkg_name] = tree
    return result


def _fuzzy_find(name: str, candidates) -> str | None:
    """공백 무시 + 접두 포함으로 느슨하게 name과 일치하는 candidates 중 하나를 찾는다.
    정확히 1개로 안 좁혀지면(모호하거나 전혀 없으면) None — 애매하면 추측으로 메우지
    않는다.

    문서 사이트 개요 페이지 제목에서 뽑은 이름과 bots.jsonl의 실제 packageName 표기가
    다른 실측 사례들을 잡기 위한 것 — 대소문자 차이가 아니라 아예 다른 문자열이라
    단순 소문자 비교로는 못 잡는다:
    - "Python"(실제 이름) <-> "Python Script"(개요 페이지 제목에서 뽑은 이름)
    - "DataTable"(실제 이름, 공백 없음) <-> "Data Table"(개요 페이지 제목, 공백 있음)
    양방향(실제 이름 -> 발견된 이름 교정, 발견된 이름 -> 실제 이름 교정)에 똑같이 쓴다.
    """
    if name in candidates:
        return name
    target_norm = name.lower().replace(" ", "")
    matches = [
        c for c in candidates
        if (c_norm := c.lower().replace(" ", "")).startswith(target_norm) or target_norm.startswith(c_norm)
    ]
    return matches[0] if len(matches) == 1 else None


def _find_package_tree(pkg_name: str, discovered: dict[str, "PackageActionTree"]) -> "PackageActionTree | None":
    match = _fuzzy_find(pkg_name, discovered)
    return discovered[match] if match else None


def _write_tree_report(trees_by_package: dict[str, "PackageActionTree"]) -> None:
    """실행마다 트리 규모를 사이드카 파일로 남긴다 — 조용히 누락되는 게 없도록, 매번
    눈에 보이게 한다."""
    report = {
        pkg_name: {"leaf_count": len(tree.leaves), "category_count": len(tree.category_docs)}
        for pkg_name, tree in trees_by_package.items()
    }
    config.DOC_ACTION_TREE_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.DOC_ACTION_TREE_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_export_for_agent(args: argparse.Namespace) -> None:
    """JAR 스키마가 없는 패키지들을, 확정적으로 풀리는 부분(패키지 판별 + 메뉴 계층)까지만
    정리해서 향후 LLM 기반 파싱 Agent(팀원이 별도 개발)가 바로 쓸 수 있는 형태로 내보낸다.

    "이 리프가 진짜 액션인지 참고자료/사용예시일 뿐인지"는 규칙 기반으로 안 풀린다고
    확인됐다(app/rag/_investigation_notes/HTML_STRUCTURE_INSIGHTS.md) — 그 판단은 여기서 안 하고, 각 리프의
    `structured_html`(CSS/JS/이미지 데이터 제거된 압축 구조, docs_crawler.py가 크롤링
    시점에 이미 계산해둠)을 그대로 실어서 Agent에게 넘긴다.
    """
    from .build.merge import load_docs

    docs = load_docs(config.DOCS_JSONL)
    en_docs = load_docs(config.docs_jsonl_for_locale("en-US"))
    packages_covered: set[str] = set()
    if config.PACKAGES_JSON.exists():
        packages_covered = {
            p["package_name"] for p in json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8"))
        }

    discovered = _discover_packages(docs, en_docs)
    _write_tree_report(discovered)

    targets = list(args.packages) if args.packages else sorted(set(discovered) - packages_covered)

    config.AGENT_HANDOFF_JSONL.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(config.AGENT_HANDOFF_JSONL, "w", encoding="utf-8") as f:
        for pkg_name in targets:
            if pkg_name in packages_covered:
                print(f"  [skip] {pkg_name}: packages.json에 이미 JAR 스키마 있음")
                continue
            tree = _find_package_tree(pkg_name, discovered)
            if tree is None:
                print(f"  [skip] {pkg_name}: 트리를 못 찾음 (먼저 crawl 필요)")
                continue
            for leaf in tree.leaves:
                record = {
                    "package_name": pkg_name,
                    "depth": leaf.depth,
                    "path_titles": leaf.path_titles,
                    "title": leaf.doc.get("title"),
                    "url": leaf.doc.get("url"),
                    "menu_id": leaf.doc.get("menu_id"),
                    "structured_html": leaf.doc.get("structured_html"),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
            print(f"  [{pkg_name}] 리프 {len(tree.leaves)}개 (카테고리 경유 {len(tree.category_docs)}개)")

    print(f"저장 → {config.AGENT_HANDOFF_JSONL} ({written}개 리프)")


def cmd_build_action_tree(args: argparse.Namespace) -> None:
    """패키지 판별 + 메뉴 계층(루트/카테고리/리프)을 JAR 유무와 무관하게 전체 패키지에
    대해 정리해 `package_action_tree.json`으로 남긴다. crawl 직후 한 번 실행해 두면
    이후 export-for-agent/export-naive-leaf-actions/build가 다시 계산할 필요 없이
    이 결과를 참고할 수 있다.
    """
    from .build.doc_action_tree import tree_to_dict
    from .build.merge import load_docs

    docs = load_docs(config.DOCS_JSONL)
    en_docs = load_docs(config.docs_jsonl_for_locale("en-US"))
    discovered = _discover_packages(docs, en_docs)
    _write_tree_report(discovered)

    tree_json = {pkg_name: tree_to_dict(tree) for pkg_name, tree in discovered.items()}
    config.PACKAGE_ACTION_TREE_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.PACKAGE_ACTION_TREE_JSON.write_text(
        json.dumps(tree_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"패키지 {len(tree_json)}개 → {config.PACKAGE_ACTION_TREE_JSON}")


def cmd_export_naive_leaf_actions(args: argparse.Namespace) -> None:
    """리프=진짜 액션 여부를 필터링하지 않고, 모든 리프를 액션 후보로 그대로 나열한다
    (파라미터 스키마 없음 — action_schema로 안 씀). Agent가 준비되기 전 빠른 훑어보기용.
    """
    from .build.merge import load_docs
    from .build.naive_leaf_actions import leaves_as_naive_actions

    docs = load_docs(config.DOCS_JSONL)
    en_docs = load_docs(config.docs_jsonl_for_locale("en-US"))
    packages_covered: set[str] = set()
    if config.PACKAGES_JSON.exists():
        packages_covered = {
            p["package_name"] for p in json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8"))
        }

    discovered = _discover_packages(docs, en_docs)
    targets = list(args.packages) if args.packages else sorted(set(discovered) - packages_covered)

    config.NAIVE_LEAF_ACTIONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(config.NAIVE_LEAF_ACTIONS_JSONL, "w", encoding="utf-8") as f:
        for pkg_name in targets:
            if pkg_name in packages_covered:
                print(f"  [skip] {pkg_name}: packages.json에 이미 JAR 스키마 있음")
                continue
            tree = _find_package_tree(pkg_name, discovered)
            if tree is None:
                print(f"  [skip] {pkg_name}: 트리를 못 찾음 (먼저 crawl 필요)")
                continue
            for record in leaves_as_naive_actions(pkg_name, tree):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
            print(f"  [{pkg_name}] {len(tree.leaves)}개")

    print(f"저장 → {config.NAIVE_LEAF_ACTIONS_JSONL} ({written}개)")


def cmd_build(args: argparse.Namespace) -> None:
    from .build.merge import build_rag_documents

    packages, docs, bots = _load_source_inputs(args.source)

    naive_leaf_actions = None
    if args.include_naive_leaf_actions:
        if not config.NAIVE_LEAF_ACTIONS_JSONL.exists():
            sys.exit(
                f"{config.NAIVE_LEAF_ACTIONS_JSONL}이 없습니다. "
                "먼저 export-naive-leaf-actions를 실행하세요."
            )
        with open(config.NAIVE_LEAF_ACTIONS_JSONL, encoding="utf-8") as f:
            naive_leaf_actions = [json.loads(line) for line in f]

    rag_docs = build_rag_documents(
        packages,
        docs,
        locale=args.locale,
        bots=bots,
        naive_leaf_actions=naive_leaf_actions,
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
    )

    config.RAG_DOCUMENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(config.RAG_DOCUMENTS_JSONL, "w", encoding="utf-8") as f:
        for doc in rag_docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    by_type: dict[str, int] = {}
    for doc in rag_docs:
        by_type[doc["source_type"]] = by_type.get(doc["source_type"], 0) + 1
    print(f"RAG 문서 {len(rag_docs)}개 → {config.RAG_DOCUMENTS_JSONL}")
    for source_type, count in sorted(by_type.items()):
        print(f"  {source_type}: {count}")


def cmd_ingest(args: argparse.Namespace) -> None:
    from .store import db

    if not config.RAG_DOCUMENTS_JSONL.exists():
        sys.exit("rag_documents.jsonl이 없습니다. 먼저 build를 실행하세요.")
    documents = [
        json.loads(line) for line in open(config.RAG_DOCUMENTS_JSONL, encoding="utf-8")
    ]

    embeddings = None
    if not args.skip_embedding:
        from .retrieval.embed import embed_texts

        print(f"임베딩 생성 중 ({config.EMBEDDING_PROVIDER}/{config.EMBEDDING_MODEL}, {len(documents)}개)...")
        embeddings = embed_texts(
            [d["content"] for d in documents],
            on_progress=lambda done, total: print(f"  {done}/{total}"),
        )

    conn = db.connect()
    try:
        db.ensure_schema(conn)
        if args.clean:
            print("--clean: 기존 rag_documents 전체 삭제")
            db.clear_all(conn)
        count = db.upsert_documents(conn, documents, embeddings)
        print(f"pgvector 적재 완료: {count}개")
    finally:
        conn.close()

    if not args.skip_opensearch:
        from .store import opensearch_client

        os_client = opensearch_client.connect()
        if args.clean:
            print("--clean: 기존 OpenSearch 색인 삭제")
            opensearch_client.delete_index(os_client)
        opensearch_client.ensure_index(os_client)
        os_count = opensearch_client.bulk_index(os_client, documents)
        print(f"OpenSearch 색인 완료: {os_count}개")


def cmd_eda(args: argparse.Namespace) -> None:
    from .build.eda import compute_length_stats, print_report
    from .build.merge import build_rag_documents

    packages, docs, bots = _load_source_inputs(args.source)
    # chunk_size=None: 청킹 전 원본 길이를 분석해야 청크 크기를 순환 오류 없이 정할 수 있다
    rag_docs = build_rag_documents(packages, docs, locale=args.locale, bots=bots, chunk_size=None)

    stats = compute_length_stats(rag_docs)
    print_report(stats)

    config.EDA_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.EDA_REPORT_JSON.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n리포트 저장: {config.EDA_REPORT_JSON}")


def cmd_search(args: argparse.Namespace) -> None:
    from .observability import new_request_id
    from .store import db, opensearch_client
    from .retrieval.hybrid_search import search as hybrid_search

    new_request_id()
    conn = db.connect()
    try:
        os_client = opensearch_client.connect()
        results = hybrid_search(conn, os_client, args.query, limit=args.limit, mode=args.mode)
        for r in results:
            score = r.get("rerank_score", r.get("rrf_score", r.get("score", 0.0)))
            print(f"[{score:.3f}] ({r['source_type']}, {r.get('retrieval_source', 'vector')}) {r['title']}")
            print(f"    {r['content'][:150]}...")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.rag.pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="Fluid Topics API로 문서 크롤링")
    p_crawl.add_argument("--locale", default="ko-KR")
    p_crawl.add_argument("--url-filter", default="cloud-commands-panel", help="prettyUrl 부분 일치 필터")
    p_crawl.add_argument("--contains", default=None, help="제목/breadcrumb 부분 일치 필터")
    p_crawl.set_defaults(func=cmd_crawl)

    p_jars = sub.add_parser("parse-jars", help="패키지 JAR/BLM export zip에서 액션 스키마 추출")
    p_jars.add_argument("paths", nargs="+", help=".jar, .zip, 또는 jar 디렉터리")
    p_jars.add_argument("--jar-locale", default="ko_KR", help="라벨 로케일 (기본 ko_KR, 없으면 en_US)")
    p_jars.set_defaults(func=cmd_parse_jars)

    p_gh = sub.add_parser("harvest-github", help="AA 공개 GitHub에서 실제 봇+패키지 JAR 수집 (계정 불필요)")
    p_gh.add_argument("--max-repos", type=int, default=None, help="테스트용 저장소 수 제한")
    p_gh.add_argument("--jar-locale", default="ko_KR")
    p_gh.set_defaults(func=cmd_harvest_github)

    p_bots = sub.add_parser("bots", help="Control Room에서 봇 목록+JSON 수집 (CR_URL/CR_USERNAME/CR_API_KEY 필요)")
    p_bots.add_argument("--workspace", default="public", choices=["public", "private"])
    p_bots.set_defaults(func=cmd_bots)

    p_export = sub.add_parser("export-packages", help="BLM export(패키지 포함) 후 JAR 스키마 자동 추출")
    p_export.add_argument("--file-ids", nargs="+", required=True, help="export할 봇 file id")
    p_export.add_argument("--jar-locale", default="ko_KR")
    p_export.set_defaults(func=cmd_export_packages)

    p_export_agent = sub.add_parser(
        "export-for-agent",
        help="JAR 스키마 없는 패키지의 리프 문서(구조화 HTML 포함)를 향후 파싱 Agent용으로 내보내기",
    )
    p_export_agent.add_argument(
        "--packages", nargs="+", default=None,
        help="대상 패키지명 (기본: 메뉴로 발견된 JAR 미보유 패키지 전체)",
    )
    p_export_agent.set_defaults(func=cmd_export_for_agent)

    sub.add_parser(
        "build-action-tree",
        help="패키지 판별+메뉴 계층을 JAR 유무와 무관하게 전체 정리해 package_action_tree.json으로 저장",
    ).set_defaults(func=cmd_build_action_tree)

    p_naive = sub.add_parser(
        "export-naive-leaf-actions",
        help="리프=진짜 액션 여부 필터링 없이 전부 액션 후보로 나열 (파라미터 없음, 빠른 훑어보기용)",
    )
    p_naive.add_argument("--packages", nargs="+", default=None, help="대상 패키지명 (기본: JAR 미보유 패키지 전체)")
    p_naive.set_defaults(func=cmd_export_naive_leaf_actions)

    p_build = sub.add_parser("build", help="문서+스키마+봇을 RAG 문서로 병합 (청킹 포함)")
    p_build.add_argument("--locale", default="ko-KR")
    p_build.add_argument(
        "--source",
        default="all",
        choices=["all", "docs", "github"],
        help="all(기본)/docs(공식문서만)/github(패키지+봇만) — docs와 github은 독립적으로 build+ingest 가능 (같은 테이블에 upsert됨)",
    )
    p_build.add_argument(
        "--include-naive-leaf-actions", action="store_true",
        help="export-naive-leaf-actions 산출물(리프=액션 필터링 없는 후보)을 action_candidate로 같이 포함",
    )
    p_build.set_defaults(func=cmd_build)

    p_eda = sub.add_parser("eda", help="청킹 전 원본 문서의 source_type별 길이 분포 분석 (청크 크기 결정용)")
    p_eda.add_argument("--locale", default="ko-KR")
    p_eda.add_argument("--source", default="all", choices=["all", "docs", "github"])
    p_eda.set_defaults(func=cmd_eda)

    p_ingest = sub.add_parser("ingest", help="임베딩 생성 후 pgvector + OpenSearch 적재")
    p_ingest.add_argument("--skip-embedding", action="store_true")
    p_ingest.add_argument("--skip-opensearch", action="store_true")
    p_ingest.add_argument(
        "--clean", action="store_true",
        help="적재 전 기존 rag_documents 테이블/OpenSearch 색인을 전부 비운다 "
             "(upsert는 새 build에 없는 옛 row를 안 지우므로, RAG 구조를 크게 바꾼 뒤 재적재할 때 필요)",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", help="적재된 문서 하이브리드(RRF+Reranker) 검색 테스트")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument(
        "--mode", default="hybrid_rerank", choices=["vector", "hybrid", "hybrid_rerank"]
    )
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
