"""app/agent 버전 레지스트리·디스패처 단위 테스트 (RPA-167).

버전 자동탐색(v\\d+)·기본버전(env AGENT_VERSION)·지연 위임·미지버전 거부·버전 격리를
LLM/DB 없이 검증한다. "v1/v2가 각자 위치에서 온전히 import되고 디스패처가 올바른 구현으로
위임한다"는 계약과 "버전 추가 시 목록이 코드 수정 없이 반영된다"는 원칙을 CI에서 지킨다.
"""

import ast
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.agent import available_versions, default_version
from app.agent.registry import resolve_version


def test_discovers_v1_and_v2_with_metadata():
    """레지스트리가 vN 폴더를 자동 발견하고 meta.py의 label/description을 싣는다."""
    versions = available_versions()
    ids = {v["id"] for v in versions}
    assert {"v1", "v2"} <= ids
    for v in versions:
        assert set(v) >= {"id", "label", "description", "default"}
        assert v["label"]  # meta.py가 비어도 id로 폴백하므로 최소 id는 보장
    # 기본은 정확히 하나.
    assert sum(v["default"] for v in versions) == 1


def test_default_version_is_v2_without_env(monkeypatch):
    monkeypatch.delenv("AGENT_VERSION", raising=False)
    assert default_version() == "v2"


def test_default_version_follows_env(monkeypatch):
    """env AGENT_VERSION이 발견된 버전이면 그것이 기본이 되고 available_versions에도 반영된다."""
    monkeypatch.setenv("AGENT_VERSION", "v1")
    assert default_version() == "v1"
    v1 = next(v for v in available_versions() if v["id"] == "v1")
    assert v1["default"] is True


def test_default_version_falls_back_for_unknown_env(monkeypatch):
    """존재하지 않는 env 값은 조용히 폴백(v2) — 부팅을 죽이지 않는다."""
    monkeypatch.setenv("AGENT_VERSION", "v99")
    assert default_version() == "v2"


@pytest.mark.parametrize("name", ["v1", "v2"])
def test_resolve_version_exposes_entrypoints(name):
    """양 버전이 각자 위치에서 온전히 import되고 진입점 3종을 노출한다(이동 무결성)."""
    mod = resolve_version(name)
    for attr in ("stream_agent_turn", "recommend", "analyze"):
        assert hasattr(mod, attr), f"{name}.{attr} 누락"


def test_resolve_none_uses_default(monkeypatch):
    monkeypatch.delenv("AGENT_VERSION", raising=False)
    assert resolve_version(None) is resolve_version("v2")


def test_unknown_explicit_version_raises():
    """명시 요청이 미지 버전이면 계약을 코드에서도 강제(ValueError)."""
    with pytest.raises(ValueError):
        resolve_version("v99")


def test_version_isolation_v1_plan_v2_agentic():
    """벤더링 격리 — v1은 단계분해(build_graph), v2는 agentic(build_agent_graph)."""
    g1 = importlib.import_module("app.agent.v1.recommend.graph")
    g2 = importlib.import_module("app.agent.v2.recommend.graph")
    assert hasattr(g1, "build_graph") and not hasattr(g1, "build_agent_graph")
    assert hasattr(g2, "build_agent_graph")


def _declared_meta(version: str) -> dict | None:
    """`vN/meta.py`가 선언한 VERSION_META를 **코드 실행 없이** 파싱한다 (파일 없으면 None).

    구현(`registry._meta`)과 **독립된 오라클**이다 — 같은 로더로 기대값을 만들면 로더가 늘 `{}`를
    돌려줘도 통과하는 동어반복이 된다. meta.py는 dict 리터럴이라 ast로 그대로 읽힌다.
    """
    import app.agent

    path = Path(app.agent.__path__[0]) / version / "meta.py"
    if not path.is_file():
        return None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "VERSION_META" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    return None


def test_available_versions_does_not_import_agent_stacks():
    """목록 조회가 **에이전트 전체 스택을 로드하지 않는다** — 콜드 컨테이너 지연의 원인 (RPA-190).

    `_meta()`가 `import_module("app.agent.vN.meta")`를 쓰면 파이썬이 부모 패키지
    `app.agent.vN/__init__.py`를 먼저 실행해 analysis·orchestrator·recommend까지 끌어온다.
    그래서 LLM도 안 부르는 목록 조회가 실측 **7.4초**(관측 p95 8,094ms)였다.

    ⚠️ **서브프로세스(콜드 인터프리터)로 본다.** 같은 프로세스에서 재면 다른 테스트가 이미
       v1·v2를 import해 둔 상태라(예: test_version_isolation_…) 실행 순서에 따라 통과해버린다 —
       그건 이 회귀를 못 잡는 가짜 초록이다.
    """
    code = textwrap.dedent(
        """
        import json, sys
        from app.agent import available_versions
        versions = available_versions()
        heavy = sorted(
            m for m in sys.modules
            if m.startswith("app.agent.v")
            and any(k in m for k in ("orchestrator", "recommend", "analysis", "verify", "prompts"))
        )
        print(json.dumps({
            "versions": [{"id": v["id"], "label": v["label"], "description": v["description"]}
                         for v in versions],
            "heavy": heavy,
        }))
        """
    )
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(  # noqa: S603 — 같은 인터프리터로 우리 코드만 실행
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root, timeout=180
    )
    assert proc.returncode == 0, f"서브프로세스 실패:\n{proc.stderr}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])

    assert data["heavy"] == [], (
        "목록 조회가 에이전트 스택을 import했다 — vN/meta.py를 부모 패키지 경유로 읽고 있다: "
        f"{data['heavy'][:5]}"
    )
    # 가벼워지기만 하고 메타를 못 읽으면 label이 id로 폴백돼 조용히 빈 껍데기가 된다.
    # "안 무겁다"와 "제대로 읽었다"를 **둘 다** 본다.
    assert data["versions"], "버전 목록이 비었다"
    for v in data["versions"]:
        declared = _declared_meta(v["id"])
        if declared is None:
            continue  # meta.py 없는 버전은 프로덕션이 id 폴백을 허용 — 테스트가 더 엄격하면 안 된다
        assert v["label"] == (declared.get("label") or v["id"]), f"{v['id']}: label이 meta.py와 다르다"
        assert v["description"] == (declared.get("description") or ""), (
            f"{v['id']}: description이 meta.py와 다르다"
        )


def test_dispatcher_keeps_public_symbol():
    """백엔드 import·테스트 monkeypatch 대상인 app.agent.stream_agent_turn이 최상위에 유지된다."""
    import app.agent as agent_pkg

    assert callable(agent_pkg.stream_agent_turn)
    assert callable(agent_pkg.available_versions)
