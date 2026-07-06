"""수집 파이프라인 CLI.

사용 예:
  python -m app.rag.pipeline crawl --contains "Google Sheets"   # 문서 크롤링 (필터)
  python -m app.rag.pipeline crawl                               # 명령 패널(패키지 문서) 전체
  python -m app.rag.pipeline parse-jars path/to/export.zip jars_dir/
  python -m app.rag.pipeline bots                                # Control Room 봇 목록+JSON 수집
  python -m app.rag.pipeline export-packages --file-ids 123 456  # BLM export → JAR 스키마 자동 추출
  python -m app.rag.pipeline build                               # 문서+스키마+봇 → rag_documents.jsonl (청킹 포함)
  python -m app.rag.pipeline eda                                  # 문서 길이 분포 분석 (청크 크기 결정용)
  python -m app.rag.pipeline ingest [--skip-embedding]           # pgvector 적재
  python -m app.rag.pipeline search "구글시트에서 시트 활성화 어떻게 해?"
"""

import argparse
import json
import sys
from pathlib import Path

from . import config


def cmd_crawl(args: argparse.Namespace) -> None:
    from .sources import fluid_topics as ft

    m = ft.find_map(locale=args.locale, title="Automation 360")
    print(f"map: {m['title']} ({args.locale}) id={m['id']}")
    toc = ft.get_toc(m["id"])
    topics = ft.flatten_toc(toc)

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

    written = ft.crawl_topics(m["id"], topics, config.DOCS_JSONL, on_progress=progress)
    print(f"저장: {written}개 신규 → {config.DOCS_JSONL}")


def cmd_parse_jars(args: argparse.Namespace) -> None:
    from .sources.jar_parser import parse_packages

    packages = parse_packages([Path(p) for p in args.paths], preferred_locale=args.jar_locale)
    config.PACKAGES_JSON.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if config.PACKAGES_JSON.exists():
        for pkg in json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8")):
            existing[pkg["package_name"]] = pkg
    for pkg in packages:
        existing[pkg["package_name"]] = pkg

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
        existing: dict[str, dict] = {}
        if config.PACKAGES_JSON.exists():
            for pkg in json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8")):
                existing[pkg["package_name"]] = pkg
        for pkg in packages:
            existing[pkg["package_name"]] = pkg
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
    from .build.normalize import load_bots, load_docs

    docs = load_docs(config.DOCS_JSONL) if source in ("all", "docs") else []
    bots = load_bots(config.BOTS_JSONL) if source in ("all", "github") else []
    packages = (
        json.loads(config.PACKAGES_JSON.read_text(encoding="utf-8"))
        if source in ("all", "github") and config.PACKAGES_JSON.exists()
        else []
    )
    return packages, docs, bots


def cmd_build(args: argparse.Namespace) -> None:
    from .build.normalize import build_rag_documents

    packages, docs, bots = _load_source_inputs(args.source)
    rag_docs = build_rag_documents(
        packages,
        docs,
        locale=args.locale,
        bots=bots,
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
        count = db.upsert_documents(conn, documents, embeddings)
        print(f"pgvector 적재 완료: {count}개")
    finally:
        conn.close()


def cmd_eda(args: argparse.Namespace) -> None:
    from .build.eda import compute_length_stats, print_report
    from .build.normalize import build_rag_documents

    packages, docs, bots = _load_source_inputs(args.source)
    # chunk_size=None: 청킹 전 원본 길이를 분석해야 청크 크기를 순환 오류 없이 정할 수 있다
    rag_docs = build_rag_documents(packages, docs, locale=args.locale, bots=bots, chunk_size=None)

    stats = compute_length_stats(rag_docs)
    print_report(stats)

    config.EDA_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.EDA_REPORT_JSON.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n리포트 저장: {config.EDA_REPORT_JSON}")


def cmd_search(args: argparse.Namespace) -> None:
    from .store import db
    from .retrieval.embed import embed_query

    conn = db.connect()
    try:
        results = db.search(conn, embed_query(args.query), limit=args.limit)
        for r in results:
            print(f"[{r['score']:.3f}] ({r['source_type']}) {r['title']}")
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

    p_build = sub.add_parser("build", help="문서+스키마+봇을 RAG 문서로 병합 (청킹 포함)")
    p_build.add_argument("--locale", default="ko-KR")
    p_build.add_argument(
        "--source",
        default="all",
        choices=["all", "docs", "github"],
        help="all(기본)/docs(공식문서만)/github(패키지+봇만) — docs와 github은 독립적으로 build+ingest 가능 (같은 테이블에 upsert됨)",
    )
    p_build.set_defaults(func=cmd_build)

    p_eda = sub.add_parser("eda", help="청킹 전 원본 문서의 source_type별 길이 분포 분석 (청크 크기 결정용)")
    p_eda.add_argument("--locale", default="ko-KR")
    p_eda.add_argument("--source", default="all", choices=["all", "docs", "github"])
    p_eda.set_defaults(func=cmd_eda)

    p_ingest = sub.add_parser("ingest", help="임베딩 생성 후 pgvector 적재")
    p_ingest.add_argument("--skip-embedding", action="store_true")
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", help="적재된 문서 벡터 검색 테스트")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
