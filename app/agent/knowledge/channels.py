"""단계별 검색 채널 — 파이프라인 단계마다 **다른 소스 타입**에 격리해 검색한다 (설계 §4 Phase 1).

## 왜 하나의 검색으로는 안 되나

지금까지 흐름 생성의 검색은 하나였다: `["action_schema", "bot_example"]`로 좁힌 질의를
기능 단위마다 던지고 그 결과가 곧 액션 메뉴였다. 지식을 늘리려면 소스 타입을 더해야 하는데,
`doc_page`가 16,164행으로 코퍼스의 91%라 한 검색에 섞는 순간 랭킹이 문서로 덮인다
(실측: 필터 없는 검색 상위 5건 중 doc_page가 절반). 그래서 **섞지 않고 나눈다** —
단계마다 필요한 채널만 본다.

| 단계 | 채널 | 소스 타입 |
|---|---|---|
| 업무 분해 | `DECOMPOSE` | `package_overview` (용례는 별도 결정론 자산) |
| 액션 선택 | `ACTION` | `action_schema` |
| 파라미터 | `PARAM_DOC` | `doc_page` (해당 액션 표기로 질의) |
| 구조 | (검색 없음) | `derive_structural_actions` — 카탈로그 직조회 |

## 채널 정의가 여기 하나뿐이어야 하는 이유

v1~v3는 같은 목록을 `recommend/graph.py`(`SEARCH_SOURCE_TYPES`)와
`recommend/research.py`(`ACTION_SOURCE_TYPES`) **두 곳에** 따로 들고 있었다. 한쪽만 고치면
dossier(선행 조사)와 compose 툴(escape hatch)이 서로 다른 채널을 보게 되는데, 그 어긋남은
에러가 아니라 "왜 메뉴에 없던 액션을 골랐지" 같은 **조용한 품질 저하**로만 나타난다.
v4는 이 모듈 하나를 본다. v1~v3의 상수는 비교 셀렉터 보존을 위해 그대로 둔다.

## 채널이 성립하려면 push-down이 있어야 한다

`search_actions`는 원래 소스 타입을 검색에 내려보내지 않고 **결과 단계에서** 걸렀다.
하이브리드가 코퍼스 전체에서 후보를 뽑고(`candidate_pool_size=50` → RRF 융합 →
`rerank_candidates=20`) 그 20건에서 필터를 적용하므로, 필터가 보는 창은
`min(k*3, 20)`이었다. 코퍼스의 91%인 `doc_page`가 그 창을 독식해 좁은 채널은 `limit`을
올려도 굶었다 — 즉 **채널을 나누는 것 자체가 무의미했다.**

그래서 §8에서 소스 타입을 검색 SQL·BM25 질의에 내려보냈다(`search_actions(pushdown=True)`,
v4 검색기만 켠다). 2026-07-26 로컬 실측(5433/9201, 12질의, 캐시 OFF):

| 채널 | 적재 행수 | 0건 질의 전→후 | 누적 히트 전→후 |
|---|---:|---|---:|
| `package_overview` | 136 | 10/12 → **0/12** | 2 → **48** |
| `action_schema` | 1,375 | 2/12 → **0/12** | 31 → **96** |
| `doc_page` | 16,164 | 0/12 → 0/12 | 24 → 24 |
| `trigger_schema` | 31 | **12/12 → 0/12** | **0 → 48** |
| `bot_example` | **0행** | 12/12 (변화 없음) | 0 → 0 |

굶주림이 사라졌다 — 모든 채널이 요청한 `limit`을 정확히 채운다. `doc_page`만 그대로인
것도 예측대로다(원래 굶은 적이 없다). `bot_example`만은 push-down으로도 안 살아난다:
**DB에 행이 0건**이라서다. 후단 필터라 에러 없이 조용히 안 잡혀 v1~v3 내내 아무도
몰랐다 — 채널 정의에서 빼고, `__post_init__`이 선언 시점에 다시 못 들어오게 막는다.

## 굶주림 우회책은 push-down과 함께 제거했다

푸시다운 전에는 좁은 채널에 질의 정형 접미사를 붙여 버텼다(`package_overview`에
"…어떤 패키지 액션을 쓰는가"를 붙이면 8질의에서 0 → 18히트). push-down 이후 그 근거가
사라졌고, 재보니 **접미사가 오히려 해로웠다** — 2026-07-26 실측(8질의, 기대 패키지가
상위 4개 안에 드는가): plain 7/8 vs 정형 6/8. "구글 시트의 셀을 읽는다"는 plain이
`Google Sheets`를 1위로 올리는데 정형은 `Clipboard·Box·OCR·Screen`으로 밀어냈다.
접미사는 개요 페이지의 **일반적인 문투**에 맞춰 굶주림을 뚫는 장치였으니, 굶지 않게
된 뒤에는 일반 패키지를 끌어오기만 한다. 제거했다.

`rescue_suffix`는 남긴다 — 0건일 때만 발화하므로 굶지 않으면 비용이 0이고, 색인
이상·이례적 질의에 대한 안전망으로는 여전히 유효하다.
"""

import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass

from app.core import config

logger = logging.getLogger(__name__)

# 실제 적재된 소스 타입 (2026-07-25 로컬 실측, 총 17,838행). 이 집합에 없는 이름을 채널에
# 쓰면 후단 필터라 **조용히** 0건이 된다 — `SearchChannel.__post_init__`이 즉시 막는다.
# 새 소스 타입이 적재되면 여기부터 늘린다.
LOADED_SOURCE_TYPES = frozenset({
    "action_schema",      # 1,375 — 액션 스펙(패키지·액션·파라미터)
    "doc_page",           # 16,164 — 공식 문서 페이지 (package_name/action_name이 비어 있다)
    "package_overview",   # 136 — 패키지 개요
    "package_release",    # 132 — 릴리스 노트 (어느 채널에도 넣지 않는다 — 설계 §4)
    "trigger_schema",     # 31 — 트리거 (Phase 3에서 채널 편입 예정)
})

# 채널이 **연속** 이 횟수만큼 0건이면 WARN한다. 채널마다 `warn_after`로 덮어쓴다.
#
# 왜 연속인가(누적이 아니라): 누적 0건만 보면 생애 최초 1히트가 들어온 순간 감지기가
# 영구히 침묵한다 — 색인 재구축이나 소스 타입 개명으로 채널이 **나중에** 죽는 경우가
# 정확히 못 잡히는 시나리오다. 연속 카운터는 죽은 시점부터 다시 센다.
# 왜 1회가 아닌가: "그 질의에 맞는 게 없었다"는 정상 상황에도 울려 경고가 소음이 된다.
_DEFAULT_WARN_AFTER = 6


@dataclass(frozen=True)
class SearchChannel:
    """한 단계가 보는 검색 채널.

    - `source_types` — 이 채널이 허용하는 소스 타입. 적재 0건 이름은 생성 시 거부한다.
    - `limit` — 질의 1건당 요청할 히트 수. 순위 병합의 깊이 상한이기도 하다.
    - `quota` — 채널 전체에서 취할 상한. 질의별 보장분은 따로 두지 않는다 —
      `merge_by_rank`가 순위 라운드로빈이라 각 질의의 1등이 다른 질의의 2등보다 항상
      먼저 들어간다(보장이 병합 방식 자체에서 나온다).
    - `rescue_suffix` — 0건일 때 **한 번만** 다시 던지는 구제 질의. 잘 되던 질의는 건드리지
      않고(정형이 오히려 해칠 수 있다) 굶은 질의만 재시도하므로 비용이 굶은 비율에 비례한다.
    - `warn_after` — 연속 0건 몇 회에 굶주림을 WARN할지. **채널마다 한 턴의 질의 수가
      달라 공통값을 쓸 수 없다**: 이 값이 한 턴 질의 수보다 크면 그 채널은 한 턴 안에서
      절대 임계에 못 닿아 감지기가 사실상 꺼진다.
    """

    name: str
    source_types: tuple[str, ...]
    limit: int
    quota: int
    rescue_suffix: str = ""
    warn_after: int = _DEFAULT_WARN_AFTER
    doc: str = ""

    def __post_init__(self) -> None:
        unknown = set(self.source_types) - LOADED_SOURCE_TYPES
        if unknown:
            # 조용한 0건이 v1~v3에서 `bot_example`로 실제 일어났다 — 선언 시점에 막는다.
            raise ValueError(
                f"채널 '{self.name}'이 적재되지 않은 소스 타입을 참조한다: {sorted(unknown)}. "
                f"적재된 것: {sorted(LOADED_SOURCE_TYPES)}"
            )

    def types(self) -> list[str]:
        """retriever 계약(`source_types: list[str] | None`)에 넘길 형태로."""
        return list(self.source_types)

    def rescue(self, query: str) -> str | None:
        """0건일 때 한 번 더 던질 구제 질의 (없으면 None)."""
        if not self.rescue_suffix:
            return None
        return f"{(query or '').strip()} {self.rescue_suffix}".strip()


# ── 채널 정의 (단일 진실 공급원) ─────────────────────────────────────────────

DECOMPOSE = SearchChannel(
    name="decompose",
    source_types=("package_overview",),
    # push-down 이후 136행 전체가 후보 풀이라 limit을 그대로 채운다(실측 0건 질의 0/12).
    limit=4, quota=6,
    # 한 턴 질의 수 = 목표 1 + 요구 3 = **최대 4건**. 공통값 6을 쓰면 한 턴 안에서 임계에
    # 못 닿아 감지기가 꺼진다 — 이 채널은 3으로 내린다.
    warn_after=3,
    doc="업무 분해 — 어떤 패키지들의 지형인지만 본다(액션 어휘는 아직 보지 않는다)",
)

ACTION = SearchChannel(
    name="action",
    source_types=("action_schema",),
    # push-down 이후 후보 20건이 전부 action_schema라 limit이 곧 유효 깊이다.
    # ⚠️ 프로덕션 팬아웃(한/영 이중 질의 최대 16건)에서는 질의 수가 quota를 넘어 라운드
    # 로빈이 1~2라운드에서 끝난다 — 그때 8은 다 안 쓰인다. 짧은 업무(질의 4~6건)에서만
    # 깊은 순위가 소비된다. 리랭크 비용은 후보 20건에 붙지 limit에 붙지 않아 8이 손해는 아니다.
    limit=8, quota=14,
    # 정형을 항상 붙이지 않는 이유: plain으로 이미 대부분 잡힌다(실측 0건 질의 0/12).
    # 굶은 질의에만 붙이므로 굶지 않으면 비용이 0인 안전망이다.
    rescue_suffix="액션",
    warn_after=8,
    doc="액션 선택 — 폐쇄 어휘. 문서·개요를 섞지 않아 랭킹이 액션 스펙으로만 채워진다",
)

PARAM_DOC = SearchChannel(
    name="param_doc",
    source_types=("doc_page",),
    # 코퍼스의 91%라 원래 굶지 않았다(push-down 전후 모두 0건 질의 0/12). 토큰이 비싸 조인다.
    limit=2, quota=10,
    # 한 턴 질의 수 = 목표 1 + 액션 6 = 최대 7건.
    warn_after=5,
    doc="파라미터 — 이미 고른 액션의 표기로 그 액션 문서 본문만 집는다",
)

CHANNELS: tuple[SearchChannel, ...] = (DECOMPOSE, ACTION, PARAM_DOC)
_BY_NAME = {c.name: c for c in CHANNELS}

# v1~v3가 쓰던 레거시 목록 — 여기 있는 이름이 넘어오면 ACTION 채널로 접는다.
_LEGACY_ACTION_TYPES = frozenset({"action_schema", "bot_example"})


def channel_for_source_types(source_types: list[str] | None) -> SearchChannel | None:
    """레거시 `source_types` 목록을 채널로 접는다 (v4 내부 호출부 호환).

    `recommend/graph.py`가 아직 자기 `SEARCH_SOURCE_TYPES`를 들고 있어(그 파일은 2상 구조
    작업이 잡고 있다) compose escape hatch가 `["action_schema","bot_example"]`을 넘긴다.
    그대로 쓰면 dossier와 채널이 어긋나므로 여기서 흡수한다 — **정의는 하나**라는 계약을
    호출부 수정 없이 유지하는 장치다. graph.py가 채널을 직접 넘기게 되면 이 경로는 죽는다.

    None(전체 검색 — qa 경로)은 그대로 None을 돌려준다: 문서까지 봐야 하는 단계다.
    """
    if not source_types:
        return None
    given = set(source_types)
    if given <= _LEGACY_ACTION_TYPES:
        return ACTION
    if given <= set(PARAM_DOC.source_types):
        return PARAM_DOC
    if given <= set(DECOMPOSE.source_types):
        return DECOMPOSE
    return None


# ── 적재 0건 감지 ────────────────────────────────────────────────────────────

_stats_lock = threading.Lock()
# 채널명 → {searches, hits, zero_streak, warned}
_stats: dict[str, dict] = {}


def _new_row() -> dict:
    return {"searches": 0, "hits": 0, "zero_streak": 0, "warned": False}


def record_hits(channel: "SearchChannel | str", hit_count: int) -> None:
    """채널 검색 1회의 결과를 집계하고, 굶은 채널을 WARN한다.

    왜 필요한가: `bot_example`은 DB에 0행인데 예외도 빈 에러도 나지 않고 그냥 안 잡혔다.
    v1~v3 내내 아무도 몰랐던 이유가 그 침묵이다. 같은 함정(적재 누락, 소스 타입 개명,
    색인 재구축 실패)이 다시 생기면 로그로 드러나게 한다.

    **연속** 0건을 센다. 누적 0건으로 잡으면 생애 최초 1히트가 들어오는 순간 감지기가
    영구히 침묵해, 채널이 **나중에** 죽는 경우(재적재·개명)를 정확히 못 잡는다.
    히트가 하나라도 오면 streak과 warned를 함께 푼다 — 회복 후 다시 죽으면 다시 울려야
    한다. 굶주림 구간(episode)당 1회 경고이지 프로세스당 1회가 아니다.

    ⚠️ "0행"과 "질의에 안 걸림"을 구분하지 않는다 — 소비자 입장에서 증상이 같고
    (그 채널의 지식이 프롬프트에 하나도 안 실린다), 구분하려면 DB를 직접 봐야 하는데
    이 계층은 검색 서비스만 안다(INTERFACES 계약).
    """
    resolved = channel if isinstance(channel, SearchChannel) else _BY_NAME.get(channel)
    channel_name = resolved.name if resolved is not None else str(channel)
    threshold = resolved.warn_after if resolved is not None else _DEFAULT_WARN_AFTER
    with _stats_lock:
        row = _stats.setdefault(channel_name, _new_row())
        row["searches"] += 1
        row["hits"] += hit_count
        if hit_count > 0:
            row["zero_streak"] = 0
            row["warned"] = False        # 회복 — 다음 굶주림 구간에 다시 울린다
            return
        row["zero_streak"] += 1
        should_warn = row["zero_streak"] >= threshold and not row["warned"]
        if not should_warn:
            return
        row["warned"] = True
        streak = row["zero_streak"]
    types = ", ".join(resolved.source_types) if resolved is not None else channel_name
    logger.warning(
        "검색 채널 '%s'(%s)가 연속 %d회 히트 0건 — 해당 소스 타입이 적재되지 않았거나"
        "(bot_example 전례), 색인이 비었거나, 소스 타입 이름이 바뀌었다. "
        "이 채널의 지식은 프롬프트에 하나도 실리지 않는다.",
        channel_name, types, streak,
    )


def channel_stats() -> dict[str, dict]:
    """채널별 {searches, hits, zero_streak, warned} 스냅샷 — 테스트·관측용."""
    with _stats_lock:
        return {name: dict(row) for name, row in _stats.items()}


def reset_channel_stats() -> None:
    """집계 초기화 (테스트 전용 — 굶주림 구간 계약을 테스트마다 되살린다)."""
    with _stats_lock:
        _stats.clear()


# ── 검색 팬아웃 상한 ─────────────────────────────────────────────────────────

# 이벤트 루프별 세마포어. 루프 하나짜리 서버(uvicorn)에서는 사실상 프로세스 전역이고,
# 여러 루프를 만드는 테스트·평가 스크립트에서는 루프별로 갈린다 — asyncio.Semaphore는
# 첫 await에서 루프에 묶여 다른 루프에서 쓰면 터지기 때문에 전역 하나로는 못 둔다.
_gates: "weakref.WeakKeyDictionary[object, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_gates_lock = threading.Lock()


def search_gate() -> asyncio.Semaphore:
    """검색 전용 동시 실행 상한 (`MAX_SEARCH_CONCURRENCY`).

    왜 LLM 상한과 따로 두나: `MAX_LLM_CONCURRENCY`는 LLM 호출만 막는다. 검색은
    `asyncio.gather`로 최대 16질의가 한꺼번에 나가는데, 채널이 셋으로 늘면 그게 곱해진다.
    그 앞에서 먼저 터지는 것은 LLM 쿼터가 아니라 **DB 풀과 리랭커 레이트 리밋**이다:

    - `app/rag/store/db.py`의 동기 풀 `max_size=20` — 검색 1건이 커넥션 1개를 잡는다.
    - 검색 1건 = Voyage 임베딩 1회 + Voyage rerank 1회 (실측 회당 1.4초).

    기본 8은 그 둘 아래에 머무는 값이다(커넥션 8/20, 리랭커 동시 8). 팬아웃 26질의
    기준으로도 4파도면 끝나 체감 지연은 유지된다.
    """
    loop = asyncio.get_running_loop()
    with _gates_lock:
        gate = _gates.get(loop)
        if gate is None:
            # 상한은 게이트 생성 시점에 굳는다 — 살아 있는 게이트를 리사이즈하면 대기 중인
            # 획득자의 공정성이 깨진다. 프로세스 수명 동안 바꿀 값이 아니다.
            gate = _gates[loop] = asyncio.Semaphore(max(1, int(config.MAX_SEARCH_CONCURRENCY)))
        return gate


def reset_search_gates() -> None:
    """게이트 캐시 비우기 (테스트 전용 — 상한을 바꿔 검증할 때)."""
    with _gates_lock:
        _gates.clear()


# ── 채널 검색 ────────────────────────────────────────────────────────────────

def search_channel(retriever, channel: SearchChannel, query: str) -> list[dict]:
    """채널 1건 동기 검색 — 질의 → (0건이면) 구제 재질의 → 집계. 실패는 빈 결과로 강등한다.

    검색 한 건의 실패가 조사 전체를 막지 않게 하는 계약은 v3 그대로다(부분 실패 격리).
    """
    shaped = (query or "").strip()
    if not shaped:
        return []
    try:
        hits = retriever.search(shaped, limit=channel.limit, source_types=channel.types())
    except Exception as e:  # noqa: BLE001 — 검색 한 건 실패가 조사 전체를 막지 않게
        logger.warning("채널 '%s' 검색 실패(%r): %s", channel.name, shaped[:50], e)
        record_hits(channel, 0)
        return []
    if not hits:
        rescue = channel.rescue(query)
        if rescue:
            try:
                hits = retriever.search(rescue, limit=channel.limit, source_types=channel.types())
            except Exception as e:  # noqa: BLE001
                logger.warning("채널 '%s' 구제 재질의 실패(%r): %s", channel.name, rescue[:50], e)
                hits = []
    record_hits(channel, len(hits))
    return list(hits)


async def gather_channel(retriever, channel: SearchChannel, queries: list[str]) -> list[list[dict]]:
    """질의 여러 건을 한 채널에 병렬로 던진다 — 검색 세마포어 안에서, 질의별 결과를 보존한다.

    질의별로 리스트를 **따로** 돌려주는 게 핵심이다. 합쳐서 돌려주면 호출부가 점수로 다시
    줄을 세우게 되고, 그게 바로 `merge_by_rank`가 없애려는 교차 질의 점수 비교다.
    """
    gate = search_gate()

    async def one(q: str) -> list[dict]:
        async with gate:
            return await asyncio.to_thread(search_channel, retriever, channel, q)

    return list(await asyncio.gather(*(one(q) for q in queries)))


def _default_key(hit: dict):
    """중복 판정 키 — 액션 행은 **표기**로, 그 외는 문서 id로.

    액션 스펙은 같은 (package, action)이 여러 행(로케일·청크)으로 적재돼 있어 id로 접으면
    같은 액션이 메뉴 자리를 두 번 먹는다. 이중 질의(한/영)가 같은 액션을 서로 다른 행으로
    물어오는 일이 흔해 실제로 자리 낭비가 난다. package_name·action_name이 비어 있는
    doc_page·package_overview 행은 표기가 없으므로 id로 떨어뜨린다.
    """
    pkg, act = hit.get("package_name"), hit.get("action_name")
    if pkg and act:
        return (pkg, act)
    return hit.get("id")


def merge_by_rank(
    result_lists: list[list[dict]], channel: SearchChannel, key_fn=None
) -> list[dict]:
    """질의별 결과를 **순위 라운드로빈**으로 합친다 — 점수를 비교하지 않는다.

    왜 점수로 합치면 안 되나: `search_actions`의 `score`는 리랭크가 되면 rerank_score
    (0~1 스케일), 리랭커가 폴백하면(VOYAGE 키 부재·레이트 리밋) rrf_score(≈0.03 스케일)다.
    같은 검색 안에서는 일관되지만 **질의 사이에는 비교 불가**다 — 한 질의만 폴백해도 그
    질의의 후보가 전부 하위로 밀려 메뉴에서 통째로 사라진다. 기존 `best[key]=max(...)`가
    정확히 그 비교를 하고 있었고, 채널이 늘면 폴백 확률도 함께 는다.

    순위는 스케일이 없다. 각 질의의 1등을 먼저 다 담고, 그다음 2등을 담는 식으로 돌면
    어떤 질의도 통째로 굶지 않는다 — 보장이 별도 노브가 아니라 병합 방식 자체에서 나온다.
    라운드는 `quota`가 찰 때까지 계속 돈다: 질의당 상한을 따로 두면 질의 수가 적은 턴
    (요구 2~3개짜리 짧은 업무)에서 메뉴가 얇아진다(실측: 5질의 케이스 후보 13개 → 8개).
    깊은 라운드도 순위 순서라 점수 비교는 끝까지 하지 않는다. 동순위 안의 순서는 질의
    순서라 결정론이다.
    """
    key_fn = key_fn or _default_key
    out: list[dict] = []
    seen: set = set()
    depth = max((len(hits) for hits in result_lists), default=0)
    for rank in range(depth):
        for hits in result_lists:
            if rank >= len(hits):
                continue
            hit = hits[rank]
            key = key_fn(hit)
            if key is None or key in seen:
                continue
            seen.add(key)
            out.append(hit)
            if len(out) >= channel.quota:
                return out
    return out
