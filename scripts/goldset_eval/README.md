# goldset_eval — 골드셋 평가 하네스 (P0)

업무정의서 PDF를 에이전트(v3)에 넣어 흐름도를 생성하고, 같은 케이스의 **정답 봇 JSON**
(A360 원본 표기)과 비교해 액션 수준 지표를 산출한다. 로드맵의 "P0 평가 하네스(골드셋)"
구현체다.

```
업무정의서 PDF ─ parse_document ─→ analyze ─→ v3 recommend ─→ Recommendation
                                                                  │ pre-order (package, action)
정답 봇 JSON ─ gold.load_case ─→ (packageName, commandName) ──────┤
                                                                  ▼
                                     notation.CanonAction 정규화 → metrics.score_case
```

## 실행

```bash
cd A360-Assistant-Backend
PYTHONUTF8=1 \
RAG_DATABASE_URL="postgresql://a360_admin:a360_local_password@localhost:5433/a360" \
OPENSEARCH_HOST="http://localhost:9201" OPENSEARCH_USERNAME="" OPENSEARCH_PASSWORD="" \
.venv/Scripts/python.exe -m scripts.goldset_eval.run_eval \
  --goldset "C:\...\final-etc-files\골드셋" \
  --out     "C:\...\final-etc-files\골드셋\평가결과" \
  [--cases 1,6,8] [--tag baseline-v3] [--timeout 900] [--parallel 3]

# 런 비교 (A=기준, B=개선 후)
python -m scripts.goldset_eval.compare_runs <run_dir_A> <run_dir_B>

# 골드셋을 손댔으면 누출 검사 (업무정의서에 구현 어휘가 새지 않았는지)
python -m scripts.goldset_eval.leak_check
```

> ⚠️ **`RAG_DATABASE_URL`과 `OPENSEARCH_HOST`를 반드시 덮어쓸 것.** 안 덮으면 `.env` 값이
> 그대로 먹혀 **운영 Neon DB와 운영 Bonsai OpenSearch에 붙는다.** 평가는 LLM 호출이 많고
> 카탈로그를 전량 읽으므로 운영에 부하를 준다.
>
> **`DATABASE_PORT`로는 안 된다** — RAG 카탈로그는 `app/rag/config.py::database_dsn()`이
> `RAG_DATABASE_URL`만 읽는다(서비스 DB와의 격리를 보장하려는 설계). `DATABASE_PORT`는
> 앱 DB용이라 카탈로그 경로에 아무 효과가 없다.

전제: docker compose 기동 + `.env`의 `OPENAI_API_KEY`.

**로컬 컨테이너 짝을 맞출 것** — postgres와 OpenSearch는 같은 세대끼리 써야 한다.

| postgres | OpenSearch | 카탈로그 세대 | 액션 수 |
|---|---|---|---|
| `a360-postgres-new` :5433 | :9201 | `llm_agent` (현행 서비스) | 1,200 |
| `a360-postgres` :5432 | :9200 | `docs_rule` (구세대) | 1,272 |

엇갈려 쓰면 pgvector와 BM25가 서로 다른 코퍼스를 봐서 RRF 융합이 조용히 망가진다.

## 표기 문제 — 왜 정규화 매처가 필요한가

| 소스 | 표기 예 |
|---|---|
| 골드셋(A360 원본 봇 JSON) | `Excel_MS/GetMultipleCells`, `Rest/restPost`, `Loop/loop.commands.start` |
| 현재 KB(공식문서 적재) | `Excel advanced/excelAdvancedPackageGetMultipleCellsAction`, `Loop/cloudUsingLoopAction` |

에이전트는 KB 표기로 출력하므로 직접 문자열 매칭이 불가능하다. `notation.py`가
패키지 별칭 + camelCase 토큰화 + 노이즈/패키지반향 토큰 제거 + 경량 어간 처리로
양쪽을 의미 토큰 집합으로 접고, 소수의 불규칙 표기(loop.commands.start 등)는 수동
별칭으로 처리한다. 유사도 ≥ 0.55(MATCH_THRESHOLD)면 동치.

## 지표 (케이스별 score.json)

- **action P/R/F1** — 탐욕 1:1 매칭 기준. Step 구획·Comment는 골드/예측 양쪽에서 제외.
- **recall_achv** — 현 KB에 동치 액션이 없는 골드 액션(예: `Twilio/*`, `Email/sendMail`)을
  분모에서 제외한 재현율. **에이전트 결함과 KB 결손을 분리**하는 핵심 지표.
  컨테이너(Loop/If/Step/Error handler)는 검수기가 KB 없이 허용하므로 항상 달성 가능 취급.
- **order_score** — 매칭쌍을 골드 순서로 놓았을 때 예측 위치 LIS 비율 (순서 보존).
- **package P/R/F1** — 패키지 정준 키 멀티셋.
- **kb_gaps / gold_only / pred_only** — KB 결손 목록·놓친 정답·에이전트만 낸 액션.
  pred_only에는 *의미 동등 대체*(예: 골드 `Rest/restPost` 대신 `Jira/jiraCreateProject`)가
  섞이므로 수치만으로 단정하지 말고 케이스 대조 필요.

## 알려진 해석 주의

1. 골드 봇은 A2019 시절 실봇이라 구식 구현(REST 수동 호출, 전용 DLL)이 있다 — 에이전트가
   전용 패키지(Jira 등)로 대체하면 지표는 miss지만 실무상 더 나은 답일 수 있다.
2. `달성가능 재현율`에서 잡히는 KB 결손(2026-07-15 기준 37건/528): Email connect·send,
   REST POST, If 본체, Screen 캡처, WebAutomation(구), Twilio, MSWord Bookmark 등 —
   RAG 적재 담당과 공유할 목록은 run 리포트의 "KB 결손" 섹션.
3. 러너는 케이스 실패를 격리한다 — summary.json의 error 행만 재실행하면 된다.
