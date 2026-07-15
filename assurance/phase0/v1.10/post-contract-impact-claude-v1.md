# Post-Phase-0 Latest-Change Impact Audit — Claude v1

Read-only audit answering `CODEX-TO-CLAUDE-20260715-013`. **This is not a contract rewrite and not an
implementation.** No v1.11 is proposed. Phase 0 v1.10 stands as approved.

## 1. Run metadata

| Item | Value |
|---|---|
| Artifact | `artifacts/post-phase0-latest-change-impact-claude-v1.md` (append-only, new) |
| Run at | 2026-07-15, local |
| Runtime | Python 3.13.2, Windows-11-10.0.26200-SP0 |
| **Frozen contract baseline** | `d66fce1840c1dcf907c9d303bbc64654c7c41857` — unchanged, see §3 |
| **Implementation baseline observed** | `origin/dev` = `7d2561819eba107a6a5ffcc32f5420a955dd7028` |
| Span audited | `d66fce1..7d25618` — **3 merges, 16 files, +1700 / −21** |
| Merges | #239 (RPA-171), #242 (RPA-172), #243 (RPA-173). No others exist; the set Codex listed is complete |
| Open PR | **#249** (RPA-176 agent v3), head `af2d554fdc4f10aaf7ab4e75b5b6970d3bbc054d`, +6865/−9, 60 files, `MERGEABLE`, not draft, **not merged** |
| Jira read | RPA-176/177 (진행 중), RPA-178..184 (Backlog) |
| Working tree | branch `docs/RPA-178-phase0-assurance-contract` — **not changed, no checkout, no commit** |
| Not performed | product/test/`app/agent/**` edit, tracked docs or wiki edit, Jira create/edit, branch/commit/push/PR, DB, LLM |

## 2. Verdict up front

- **The three-harness direction and the D-002 ownership boundary are unchanged and remain valid.**
- **No Phase 0 invariant is broken.** Everything below is absorbed by the *implementation* baseline.
- **No v1.11.** New product features are not a reason to reopen a converged contract.
- `RPA-178`, `RPA-179`, `RPA-180` may proceed now. **`RPA-181` may start but must not freeze its
  strict schema before #249 merges.** `RPA-182`/`RPA-183` need acceptance updates (§6).
- One **Decision-needed** is surfaced, not resolved and not numbered by me (§7).

## 3. Why the contract baseline does not move

```text
git diff --name-only d66fce1..origin/dev -- app/agent | wc -l   ->  0
```

**Zero files under `app/agent/**` changed.** Therefore every Current-Confirmed Agent fact v1.10 froze
at `d66fce1` — the 5 public names, `registry._discover()`/`default_version()` behaviour, and above all
**`_done_data` carrying no version field** — still holds at `7d25618`. D-21 is neither closed nor
weakened by these merges. This is the reason the contract baseline and the implementation baseline can
be recorded separately, exactly as `RPA-178`'s DoD already requires.

## 4. Current-Confirmed — what actually merged (`7d25618`, read via `git show`, not executed)

| # | Fact | Evidence |
|---|---|---|
| C1 | **The product now has its first live blocking control.** `/turn` raises **429 before the SSE stream opens** when a budget limit is exceeded. | `app/api/sessions.py` (#239): `verdict = await run_in_threadpool(budget.check_budget, ...)`; `if verdict.exceeded: raise HTTPException(429, ...)`, placed deliberately before the stream because "SSE는 이미 200으로 열린 뒤라 프론트가 HTTP 에러로 잡지 못한다" |
| C2 | **Budget limits are a runtime-mutable policy**, changed without redeploy via `PUT /admin/budget-limits`, append-only rows in `budget_limits` with `updated_by` + `created_at`, cache busted for immediate effect. `updated_by` is the admin email, or the literal `"service"` for `X-API-Key` ops callers. | `app/api/admin.py`, `app/models.py::BudgetLimitOverride` (#243) |
| C3 | The same override pattern already existed for retrieval params (RPA-149); budget explicitly copies it. Neither lives in a protected policy tree, carries a digest, nor has an `approved` state. | `models.RetrievalParamOverride`, `models.BudgetLimitOverride` docstring: "RPA-149(retrieval_params)와 **같은 패턴**" |
| C4 | **The budget decision reads the observability DB**, not the primary DB. | `budget.check_budget` → `from app.core.observability_db import observability_sessionmaker`; `_spent()` sums `models.LlmUsage.cost_usd` |
| C5 | Gauge thresholds recalibrated from measurements: `TURN_GAUGE_LIMIT_TOKENS` 100000 → **6000**, warn ratio 0.7 → **0.87**; both still env-overridable with fallback-on-invalid. | `app/api/sessions.py` (#242) |
| C6 | Observability indexes added for the budget query path, and schema prep still swallows failures ("관측 DB 장애가 앱 기동을 막으면 안 된다"). | `app/core/observability_db.py` (#239) |

## 5. Planned — PR #249 (RPA-176 agent v3), **not merged**, do not treat as fact

54 of its 60 files are under `app/agent/**` and are the Agent owner's. Only these touch surfaces the
Backend boundary owns or consumes:

| # | Planned change | Why the harness cares |
|---|---|---|
| P1 | **Backend-owned public schema grows, additively**: `VarRef`, `RecommendedAction.produces/consumes`, `CardTarget`, `QuestionCard`, `FlowSpec`/`SpecRequirement`/`SpecUnknown`, `Recommendation.needs_input`. | `app/schemas/recommendation.py` is **Backend-owned**. A strict boundary schema frozen against today's shape would deny valid v3 payloads (§6, RPA-181) |
| P2 | **`operation` enum extended**: `^(chat\|compact\|fill_cards)$`, plus `card_values: dict[str, Any]`. `fill_cards` is described as an **LLM-free deterministic path** that still yields a new recommendation version. | Phase 0's operation model splits `origin` / `mutation_kind` / `llm_invoked`. `fill_cards` is a mutation with `llm_invoked=False` — a combination no current fixture covers |
| P3 | **`QuestionCard.input_type` includes `credential_ref`** (also `file_path`). Card answers arrive as `card_values` and are echoed into `agent_context["card_values"]`. | Evidence persistence must never store a credential value. Direct P1 for RPA-182's masking/retention scope |
| P4 | New SSE frames (spec / candidates / verdict / scorecard) and `flow_confidence`. | Per-operation completeness cardinality becomes **version-dependent** (§6, RPA-182) |
| P5 | v3 is selected **per turn**; `AGENT_VERSION` default stays v2, v2 code unmodified. | Confirms the D-24 correction in Codex's message. Nothing here contradicts it |

## 6. Per-Jira judgement

| Jira | Judgement | Basis |
|---|---|---|
| **RPA-178** contract/policy repo adoption | **unchanged** | Depends only on the frozen v1.10 artifacts. §3 shows the contract baseline is intact; its DoD already demands contract vs implementation baselines be recorded separately, which §4 supplies |
| **RPA-179** determinism + single policy registry | **unchanged** | Both debts are internal to `phase0-v1.10-src/`. Nothing merged touches them. (Both are mine: the fixed `.out` path and the duplicated policy tuple — the latter is the drift I found by accident in v1.10 §4, and RPA-179 is what makes it structurally impossible) |
| **RPA-180** Change Assurance Observe | **unchanged** | Scoped to repo diffs/PRs, independent of Agent output. It may proceed before #249 — and #249 (60 files, +6865) is exactly the kind of large AI-assisted PR it exists to observe |
| **RPA-181** Output Boundary Observe | **dependency_or_order_change + acceptance_update** | See below |
| **RPA-182** receipt/evidence + Backoffice | **acceptance_update** | See below |
| **RPA-183** protected writer/policy + promotion | **acceptance_update** | See below |
| **RPA-184** public `resolved_agent_version` | **unchanged, and now doubly blocking** | Still the D-21 blocker for any assured Output; §6 also makes it a prerequisite for RPA-182's version-aware completeness |

### RPA-181 — start yes, freeze no

The DoD says "strict payload schema". v3 adds fields **additively** (P1). A strict Backend-owned schema
written now against the v2 shape is, by construction, a schema that **denies valid v3 output** the day
#249 merges — a false deny manufactured by our own harness, in Observe, on the Agent owner's correct
work. Two acceptance updates:

1. The boundary schema must be **keyed on the resolved Agent version**, or explicitly additive-tolerant
   for owner-approved public fields. Freezing it must wait for #249's public schema to land.
2. `fill_cards` (P2) must be modelled in the operation contract before RPA-181 claims coverage:
   `llm_invoked=False`, `origin` = card submission, `mutation_kind` = revise, and it **does** produce a
   recommendation version. A model that assumes "mutation ⇒ LLM" is wrong at `af2d554`.

Order: RPA-181 validator + Observe wiring may begin now against v2; its strict-schema freeze and its
Enforce promotion both sit behind #249 and RPA-184.

### RPA-182 — three acceptance updates

1. **Completeness must key on `resolved_agent_version`.** v3 emits frames v2 never emits (P4), so
   expected stage cardinality differs per version. A single expected-set would either under-check v3 or
   report v2 turns as incomplete. This makes RPA-182 depend on **RPA-184**, which is the only way to
   know which version actually ran.
2. **A budget-429 turn produces zero LLM/turn evidence (C1).** That is a legitimate zero, not missing
   evidence. If the completeness policy cannot express it, the harness will report `refused` for turns
   the product correctly blocked — and Phase 0's own rule ("검사 오류·증거 누락을 PASS로 바꾸지 않는다")
   cuts both ways: it must not manufacture a failure either.
3. **`credential_ref` / `file_path` card values must never be persisted** (P3). RPA-182's masking scope
   currently names PII; card values are a new secret-bearing surface.

### RPA-183 — the runtime-override gap

RPA-183's DoD says "정책과 검사 코드를 한 명이 동시에 무검토 변경할 수 없음" and scopes two-person review to
CODEOWNERS/branch protection — i.e. **policies that live in the repo**. But C2/C3 show the product
already governs live behaviour through policies that are **not in the repo at all**: `budget_limits` and
`retrieval_params` are mutated at runtime through an admin API, by a human admin or by an `X-API-Key`
service caller recorded only as the string `"service"`, with no digest, no `approved` state, and no
second reviewer. One of those values decides whether a user gets a 429.

This is not a Phase 0 violation — the harness does not govern budget, and D-16/D-17 as accepted are
about *assurance* receipts and policies. But RPA-183 cannot claim it has operationalised "the protected
policy boundary" while the product's only live blocking policy sits outside it. Acceptance must state
explicitly which of the two is true (see §7).

## 7. Decision-needed — surfaced, not resolved, not numbered by me

> **Are product runtime-override policies (`budget_limits`, `retrieval_params`) inside the harness
> policy-authority model, or explicitly outside it?**

Both answers are defensible and the choice is not mine:

- **Outside** — they are product operational tuning, not assurance authority. Then RPA-183 must say so
  in one line, so nobody later reads "protected policy boundary" as covering them.
- **Inside** — then they need digest/approval binding and an audit trail the harness resolves, and
  `updated_by="service"` is not sufficient provenance for a value that blocks users.

I did not create a Jira issue, did not add a D-number, and did not modify `DECISIONS.md`, `STATUS.md`
or `CONTEXT.md` — this round is read-only by instruction and Codex is implementing RPA-178 in parallel.

## 8. Answers to the required questions

- **Is the three-harness direction and D-002 still valid?** Yes. Zero `app/agent/**` changes merged
  (§3); #249's 54 Agent files are the owner's and I read them only to bound the public surface.
- **How do D-21/D-22/D-23 apply while keeping per-turn v3 selection?** Unchanged, and the merges do not
  disturb them. D-24's canonical form is per-turn selection (RPA-176/177), and #249 confirms it: default
  stays v2, `agent_version` is per request. D-21 stays blocking because `_done_data` still has no version
  (§3); D-22 stays blocking because `approved_versions` is empty; D-23's silent-fallback ban is exactly
  what RPA-184 must implement for v3's unsupported-version path.
- **May RPA-178/179/180 proceed before v3 merges?** Yes, all three. Only RPA-181's schema freeze and any
  Enforce promotion must wait.
- **Missing public-schema/provenance/lineage/SSE evidence in RPA-181/182/184?** Yes — P1–P4 above, plus
  the card→`fill_cards`→new-version lineage that RPA-184 already names.
- **Do the merges require new P0/P1 blockers or new Jira?** No new P0. One P1-class Decision-needed (§7),
  which I recommend routing into RPA-183's acceptance rather than a new issue.

## 9. Honest limits of this audit

- **Nothing was executed.** I read merged code with `git show`/`git diff` and PR #249 through a
  read-only fetch ref, which I deleted. I did not run the product, its tests, the harness, or a live
  429. Every §4/§5 row is a **code-read fact**, not a runtime observation.
- **`Not-tested`:** what `check_budget` does when the observability DB is unreachable. `_spent()` would
  raise inside `observability_sessionmaker()()`, but whether that surfaces as a 500 or is caught upstream
  I did not verify, and I will not guess at a fail-open/fail-closed claim for a blocking control.
- **`Planned` is not `Current-Confirmed`.** §5 is PR #249 at `af2d554`; it may change or not merge. No
  acceptance criterion should be written as if v3 exists today.
- I did not read the GitHub Wiki pages directly (private repo, no authenticated fetch in this run); I
  used `DECISIONS.md` and `STATUS.md`, which record the same accepted decisions and cite wiki commit
  `e271915`. If the wiki and `DECISIONS.md` disagree, this audit followed `DECISIONS.md`.
- **I am the harness's author auditing changes that affect my own harness's roadmap.** Independent review
  has caught an overclaim of mine in all eight Phase 0 rounds. Treat §6's judgements as `advisory
  evidence`, not a PASS.

---

# 한국어 버전

`CODEX-TO-CLAUDE-20260715-013`에 답하는 **read-only 감사**입니다. 계약 재작성도 구현도 아니며 **v1.11을
제안하지 않습니다.** Phase 0 v1.10은 승인된 상태 그대로입니다.

## K1. 실행 메타데이터

| 항목 | 값 |
|---|---|
| 산출물 | `artifacts/post-phase0-latest-change-impact-claude-v1.md` (신규, append-only) |
| 실행 | 2026-07-15 / Python 3.13.2 / Windows-11-10.0.26200-SP0 |
| **고정 계약 baseline** | `d66fce1840c1dcf907c9d303bbc64654c7c41857` — 이동 없음(§K3) |
| **관측된 구현 baseline** | `origin/dev` = `7d2561819eba107a6a5ffcc32f5420a955dd7028` |
| 감사 범위 | `d66fce1..7d25618` — **3 merge, 16파일, +1700 / −21** |
| 병합 | #239(RPA-171), #242(RPA-172), #243(RPA-173). **그 외 없음** — Codex가 적은 목록이 완전합니다 |
| 열린 PR | **#249**(RPA-176 v3), head `af2d554…`, +6865/−9, 60파일, `MERGEABLE`, draft 아님, **미병합** |
| Jira | RPA-176/177(진행 중), RPA-178~184(Backlog) |
| 워킹트리 | `docs/RPA-178-phase0-assurance-contract` — **변경·checkout·commit 없음** |
| 수행 안 함 | 제품·테스트·`app/agent/**` 수정, 추적 문서·위키 수정, Jira 생성·수정, branch/commit/push/PR, DB, LLM |

## K2. 결론 먼저

- **3중 하네스 방향과 D-002 소유권 경계는 그대로 유효합니다.**
- **Phase 0 불변식을 깨는 변경은 없습니다.** 아래는 전부 *구현* baseline에서 흡수합니다.
- **v1.11 없음.** 새 기능이 생겼다는 이유로 수렴된 계약을 다시 열지 않습니다.
- `RPA-178`·`RPA-179`·`RPA-180`은 지금 진행 가능합니다. **`RPA-181`은 착수는 되지만 #249 병합 전에
  strict schema를 고정하면 안 됩니다.** `RPA-182`·`RPA-183`은 인수 기준 보정이 필요합니다(§K6).
- **Decision-needed 1건**을 제기하되 해결하지 않았고 번호도 붙이지 않았습니다(§K7).

## K3. 계약 baseline이 이동하지 않는 이유

`git diff --name-only d66fce1..origin/dev -- app/agent | wc -l` → **0**.

**`app/agent/**`가 한 파일도 바뀌지 않았습니다.** 따라서 v1.10이 `d66fce1`에서 고정한 Agent 사실 —
공개 심볼 5개, `registry._discover()`/`default_version()` 동작, 그리고 무엇보다 **`_done_data`에 버전
필드가 없다**는 사실 — 이 `7d25618`에서도 그대로입니다. 이 병합들로 D-21이 닫히지도, 약해지지도
않았습니다. 계약 baseline과 구현 baseline을 분리 기록할 수 있는 근거이며, 이는 `RPA-178`의 DoD가 이미
요구하는 바입니다.

## K4. Current-Confirmed — 실제로 병합된 것 (`7d25618`, `git show`로 읽음, 실행 안 함)

| # | 사실 | 근거 |
|---|---|---|
| C1 | **제품에 첫 실제 차단 통제가 생겼습니다.** 예산 초과 시 `/turn`이 **SSE 스트림을 열기 전에 429**로 끊습니다. | `app/api/sessions.py`(#239). 스트림 앞에 둔 이유를 코드가 직접 적어둠 — "SSE는 이미 200으로 열린 뒤라 프론트가 HTTP 에러로 잡지 못한다" |
| C2 | **예산 상한은 런타임 가변 정책**입니다. 재배포 없이 `PUT /admin/budget-limits`로 바뀌고, `budget_limits`에 append-only 행 + `updated_by`/`created_at`, 캐시 무효화로 즉시 반영. `updated_by`는 관리자 이메일이거나 `X-API-Key` ops 호출이면 문자열 `"service"`. | `app/api/admin.py`, `models.BudgetLimitOverride`(#243) |
| C3 | 같은 오버라이드 패턴이 retrieval params(RPA-149)에 이미 있었고 예산이 명시적으로 복제했습니다. 둘 다 보호된 정책 트리에 없고, digest도 `approved` 상태도 없습니다. | `models.BudgetLimitOverride` docstring: "RPA-149와 **같은 패턴**" |
| C4 | **예산 판정은 primary DB가 아니라 관측 DB를 읽습니다.** | `budget.check_budget` → `observability_sessionmaker`; `_spent()`가 `LlmUsage.cost_usd` 합산 |
| C5 | 게이지 임계 실측 재보정: `TURN_GAUGE_LIMIT_TOKENS` 100000 → **6000**, warn 0.7 → **0.87**. 둘 다 여전히 env 오버라이드 + 비정상값 폴백. | `app/api/sessions.py`(#242) |
| C6 | 예산 조회 경로용 관측 DB 인덱스 추가. 스키마 준비는 여전히 예외를 삼킵니다("관측 DB 장애가 앱 기동을 막으면 안 된다"). | `app/core/observability_db.py`(#239) |

## K5. Planned — PR #249(v3), **미병합**. 사실로 취급 금지

60파일 중 54개가 `app/agent/**`이며 Agent 담당자 소유입니다. Backend가 소유·소비하는 표면은 아래뿐입니다.

| # | 계획된 변경 | 하네스가 신경 쓰는 이유 |
|---|---|---|
| P1 | **Backend 소유 공개 스키마가 하위호환으로 확장**: `VarRef`, `RecommendedAction.produces/consumes`, `CardTarget`, `QuestionCard`, `FlowSpec`/`SpecRequirement`/`SpecUnknown`, `Recommendation.needs_input`. | `app/schemas/recommendation.py`는 **Backend 소유**입니다. 지금 모양으로 strict schema를 고정하면 v3의 정상 산출물을 거부하게 됩니다(§K6) |
| P2 | **`operation` 확장**: `^(chat\|compact\|fill_cards)$` + `card_values`. `fill_cards`는 **LLM 무개입 결정론 경로**인데도 새 추천 버전을 만듭니다. | Phase 0 운영 모델은 `origin`/`mutation_kind`/`llm_invoked`를 분리합니다. `fill_cards`는 `llm_invoked=False`인 **변경**이며 현재 fixture에 없는 조합입니다 |
| P3 | **`QuestionCard.input_type`에 `credential_ref`**(및 `file_path`)가 있습니다. 카드 응답은 `card_values`로 들어와 `agent_context["card_values"]`로 전달됩니다. | 증거 저장이 자격증명 값을 담으면 안 됩니다. RPA-182 마스킹 범위의 직접적인 P1 |
| P4 | 새 SSE 프레임(spec/candidates/verdict/scorecard)과 `flow_confidence`. | operation별 completeness 기수가 **버전 의존**이 됩니다(§K6) |
| P5 | v3는 **턴 단위** 선택, 기본은 v2 유지, v2 코드 무수정. | Codex 메시지의 D-24 보정과 일치합니다. 배치되는 내용 없음 |

## K6. Jira별 판정

| Jira | 판정 | 근거 |
|---|---|---|
| **RPA-178** 계약·정책 편입 | **unchanged** | 고정된 v1.10 산출물에만 의존. §K3이 계약 baseline 무결을 보이며, DoD가 이미 요구하는 "구현 baseline 별도 기록"은 §K4가 제공 |
| **RPA-179** 결정론·정책 registry 단일화 | **unchanged** | 두 부채 모두 `phase0-v1.10-src/` 내부. 병합된 것 중 건드리는 게 없음. (둘 다 제 부채입니다 — 특히 정책 목록 중복은 v1.10 §4에서 제가 **우연히** 발견한 표류이고, RPA-179가 그걸 구조적으로 불가능하게 만드는 작업입니다) |
| **RPA-180** Change Assurance Observe | **unchanged** | 저장소 diff/PR 범위라 Agent 출력과 무관. #249 전에 진행 가능하며, 오히려 #249(60파일 +6865)야말로 이 통제가 관측하려는 종류의 대형 AI 보조 PR입니다 |
| **RPA-181** Output Boundary Observe | **dependency_or_order_change + acceptance_update** | 아래 |
| **RPA-182** receipt·증거·백오피스 | **acceptance_update** | 아래 |
| **RPA-183** 보호 writer·정책 경계 | **acceptance_update** | 아래 |
| **RPA-184** 공개 `resolved_agent_version` | **unchanged, 그리고 이중으로 차단 요인** | 여전히 assured Output의 D-21 차단 요인이고, 이제 RPA-182의 버전 인식 completeness의 선행 조건이기도 합니다 |

### RPA-181 — 착수는 예, 고정은 아니오

DoD가 "strict payload schema"라고 합니다. v3는 필드를 **하위호환으로 추가**합니다(P1). 지금 v2 모양으로
strict schema를 쓰면, #249가 병합되는 날 **정상 v3 산출물을 거부하는 스키마**가 됩니다 — Observe에서,
Agent 담당자의 올바른 작업에 대해, 우리 하네스가 만들어낸 오탐입니다. 보정 두 가지:

1. 경계 스키마는 **해석된 Agent 버전으로 키잉**하거나, 소유자 승인 공개 필드에 대해 명시적으로
   additive-tolerant여야 합니다. 고정은 #249의 공개 스키마 확정 이후입니다.
2. `fill_cards`(P2)를 운영 계약에 모델링한 뒤에 커버리지를 주장해야 합니다 — `llm_invoked=False`인데
   추천 버전을 만듭니다. "변경 ⇒ LLM"을 가정하는 모델은 `af2d554`에서 틀립니다.

순서: RPA-181의 validator·Observe 배선은 지금 v2 기준으로 시작 가능. strict schema 고정과 Enforce 승격은
#249와 RPA-184 뒤.

### RPA-182 — 인수 기준 보정 3건

1. **completeness를 `resolved_agent_version`으로 키잉해야 합니다.** v3는 v2에 없는 프레임을 냅니다(P4).
   단일 expected 집합이면 v3를 덜 검사하거나 v2를 incomplete로 보고합니다. 이 때문에 RPA-182가
   **RPA-184에 의존**합니다 — 실제 실행 버전을 아는 유일한 방법이라서요.
2. **예산 429 턴은 LLM/턴 증거가 0건입니다(C1).** 이건 정당한 0이지 증거 누락이 아닙니다. 정책이 이를
   표현하지 못하면, 제품이 올바르게 막은 턴을 하네스가 `refused`로 보고합니다. Phase 0의 원칙("검사
   오류·증거 누락을 PASS로 바꾸지 않는다")은 양방향입니다 — **없는 실패를 만들어내서도 안 됩니다.**
3. **`credential_ref`/`file_path` 카드 값은 절대 저장되면 안 됩니다**(P3). 현재 마스킹 범위는 PII만
   명시하는데, 카드 값은 새로 생긴 비밀 보유 표면입니다.

### RPA-183 — 런타임 오버라이드 격차

RPA-183 DoD는 "정책과 검사 코드를 한 명이 동시에 무검토 변경할 수 없음"이라 하고, 2인 리뷰 범위를
CODEOWNERS·브랜치 보호 — 즉 **저장소에 있는 정책** — 로 잡습니다. 그런데 C2/C3는 제품이 이미 **저장소에
아예 없는 정책**으로 실제 동작을 통제하고 있음을 보여줍니다. `budget_limits`와 `retrieval_params`는
admin API로 런타임에 바뀌고, 주체는 사람 관리자이거나 `"service"`라는 문자열로만 기록되는 `X-API-Key`
호출이며, digest도 `approved` 상태도 2차 리뷰어도 없습니다. **그 값 하나가 사용자에게 429를 줄지
말지를 결정합니다.**

Phase 0 위반은 아닙니다 — 하네스는 예산을 통제하지 않고, 승인된 D-16/D-17은 *보증* receipt와 정책에
관한 것입니다. 다만 제품의 유일한 실제 차단 정책이 그 밖에 있는 채로 RPA-183이 "보호된 정책 경계를
운영화했다"고 주장할 수는 없습니다. 인수 기준이 둘 중 무엇인지 명시해야 합니다(§K7).

## K7. Decision-needed — 제기만, 해결·번호 부여 안 함

> **제품 런타임 오버라이드 정책(`budget_limits`, `retrieval_params`)은 하네스 정책 권위 모델 안인가,
> 명시적으로 밖인가?**

둘 다 방어 가능하고 제 선택이 아닙니다.

- **밖** — 보증 권위가 아니라 제품 운영 튜닝이다. 그러면 RPA-183이 한 줄로 그렇게 적어야 나중에 누구도
  "보호된 정책 경계"가 이것까지 덮는다고 읽지 않습니다.
- **안** — 그러면 하네스가 해석하는 digest/승인 결속과 감사 이력이 필요하고, **사용자를 막는 값**의
  provenance로 `updated_by="service"`는 불충분합니다.

Jira를 만들지 않았고, D 번호를 추가하지 않았으며, `DECISIONS.md`·`STATUS.md`·`CONTEXT.md`를 수정하지
않았습니다 — 이번 라운드는 지시대로 read-only이고 Codex가 RPA-178을 병렬로 구현 중입니다.

## K8. 필수 질문에 대한 답

- **3중 하네스 방향과 D-002는 유효한가?** 예. `app/agent/**` 병합 변경 0건(§K3). #249의 Agent 파일 54개는
  담당자 소유이며 공개 표면 범위를 확정하려고 읽기만 했습니다.
- **v3 턴 선택을 유지하며 D-21/22/23을 어떻게 적용하나?** 그대로이며 이번 병합이 흔들지 않습니다. D-24의
  정본은 턴 단위 선택이고 #249가 이를 확인합니다(기본 v2, `agent_version` 요청 단위). `_done_data`에
  버전이 없으므로 D-21은 계속 차단 요인, `approved_versions`가 비어 D-22도 차단 요인, D-23의 조용한
  fallback 금지는 v3 미지원 버전 경로에서 RPA-184가 구현해야 할 바로 그것입니다.
- **v3 병합 전에 RPA-178/179/180 진행 가능한가?** 셋 다 가능합니다. RPA-181의 스키마 고정과 모든 Enforce
  승격만 뒤로 미룹니다.
- **RPA-181/182/184에 빠진 공개 스키마·provenance·lineage·SSE 증거가 있나?** 예 — 위 P1~P4, 그리고
  카드→`fill_cards`→새 버전 계보(RPA-184가 이미 언급).
- **새 P0/P1 차단이나 Jira가 필요한가?** 새 P0는 없습니다. P1급 Decision-needed 1건(§K7)이며, 새 이슈보다
  **RPA-183 인수 기준으로 넣는 것**을 권고합니다.

## K9. 이 감사의 정직한 한계

- **아무것도 실행하지 않았습니다.** 병합된 코드는 `git show`/`git diff`로, PR #249는 읽기 전용 fetch
  ref로 읽고 삭제했습니다. 제품·테스트·하네스·실제 429를 돌리지 않았습니다. §K4/§K5의 모든 행은
  **코드 판독 사실**이지 런타임 관측이 아닙니다.
- **`Not-tested`:** 관측 DB가 불통일 때 `check_budget`이 어떻게 되는지. `_spent()`가
  `observability_sessionmaker()()` 안에서 예외를 낼 텐데, 그게 500으로 표면화되는지 상위에서 잡히는지
  검증하지 않았습니다. **차단 통제의 fail-open/fail-closed를 추측으로 주장하지 않겠습니다.**
- **`Planned`은 `Current-Confirmed`가 아닙니다.** §K5는 `af2d554` 시점의 PR #249이고 바뀌거나 병합되지
  않을 수 있습니다. 어떤 인수 기준도 v3가 오늘 존재하는 것처럼 쓰면 안 됩니다.
- GitHub Wiki 페이지를 직접 읽지 않았습니다(비공개 저장소, 이번 실행에 인증 fetch 없음). 같은 확정
  결정을 기록하고 위키 커밋 `e271915`를 인용하는 `DECISIONS.md`·`STATUS.md`를 사용했습니다. 위키와
  `DECISIONS.md`가 다르면 이 감사는 `DECISIONS.md`를 따랐습니다.
- **저는 하네스의 작성자이면서 그 하네스의 로드맵에 영향을 주는 변경을 감사했습니다.** 여덟 라운드의
  Phase 0에서 독립 검토가 매번 제 과대주장을 잡았습니다. §K6의 판정은 `advisory evidence`이지 PASS가
  아닙니다.
