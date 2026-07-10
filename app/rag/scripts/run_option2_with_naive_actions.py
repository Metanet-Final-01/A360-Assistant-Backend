"""옵션 2: 옵션 1 + JAR 없는 패키지의 리프 문서도 action_reference로 같이 적재.

리프가 진짜 액션인지 필터링은 안 한다(그건 미래 파싱 Agent 몫 — app/rag/README.md
"JAR 없는 패키지" 절 참고). 파라미터 스키마도 없다 — action_schema가 아니라
action_reference로 들어가 추천 메뉴에는 안 뜨고 search_kb 검색에만 쓰인다.
"""

from _run_steps import run_steps

if __name__ == "__main__":
    run_steps([
        ["crawl"],
        ["build-action-tree"],
        ["export-naive-leaf-actions"],
        ["build", "--include-naive-leaf-actions"],
        ["ingest"],
    ])
