"""공유 DB 마이그레이션 스크립트의 계산이 맞는지 (RPA-186).

`scripts/migrate_shared_db.py`는 **공유 앱 DB에 스키마를 올리는 유일한 경로**다(앱 기동 시
자동 마이그레이션은 공유 DB를 건너뛴다). 그래서 이게 틀리면 대안이 없다.

⚠️ 이 파일이 생긴 이유: 스크립트를 써놓고 **한 번도 돌리지 않아** CodeRabbit이 버그 2개를
   잡았다(#258) — ① 신규 DB에 `alembic_version`이 없어 크래시 ② 이미 적용된 과거 리비전까지
   '대기'로 표시. 공유 DB를 처음 붙이는 순간이 이 스크립트가 가장 필요한 때인데 거기서 죽는다.

실제 `migrations/versions/`를 읽어 검증하므로 DB가 없어도 진짜를 잰다.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

_ROOT = Path(__file__).resolve().parent.parent


def _load_script_module():
    """scripts/migrate_shared_db.py를 모듈로 읽는다 (scripts/는 패키지가 아니다)."""
    path = _ROOT / "scripts" / "migrate_shared_db.py"
    spec = importlib.util.spec_from_file_location("migrate_shared_db", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_shared_db"] = mod
    spec.loader.exec_module(mod)
    return mod


mig = _load_script_module()


@pytest.fixture(scope="module")
def script_dir() -> ScriptDirectory:
    """실제 migrations/versions/ — 하드코딩한 가짜가 아니라 진짜 리비전 그래프."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


# --- ① 신규 DB: alembic_version 테이블이 없다 ---

def test_current_revision_returns_none_on_fresh_db():
    """`alembic_version`이 없는 DB에서 **예외가 아니라 None**을 준다.

    공유 DB를 처음 붙이는 순간이 이 스크립트가 가장 필요한 때다. 거기서 크래시하면 쓸모가 없다.
    (sqlite 인메모리로 '테이블 없는 DB'를 재현한다 — postgres 없이도 이 분기를 잰다.)
    """
    engine = create_engine("sqlite://")  # 완전히 빈 DB

    assert mig._current_revision(engine) is None, (
        "신규 DB에서 None이 아니다 — select를 그냥 던지면 여기서 예외가 난다")


def test_current_revision_reads_the_row_when_table_exists():
    """테이블이 있으면 그 값을 읽는다 — 가드가 기능을 죽이면 안 된다."""
    from sqlalchemy import text

    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("create table alembic_version (version_num varchar(32) not null)"))
        c.execute(text("insert into alembic_version values ('0014')"))

    assert mig._current_revision(engine) == "0014"


# --- ② 대기 목록: 과거 리비전이 섞이면 안 된다 ---

def test_pending_from_mid_revision_excludes_the_past(script_dir):
    """중간 리비전 DB에서 **이미 적용된 과거**가 대기 목록에 뜨면 안 된다.

    `walk_revisions(base="base")`로 전부 훑고 current만 빼면 0001~0013까지 '적용 대기'로 뜬다 —
    사람이 그걸 보고 "이걸 다 올린다고?" 하고 멈추거나, 더 나쁘게는 그냥 --apply 한다.
    """
    head = script_dir.get_heads()[0]
    pending = mig._pending_revisions(script_dir, "0014")

    assert "0014" not in pending, "현재 리비전 자신이 대기에 들어 있다"
    assert not any(r < "0014" for r in pending), f"과거 리비전이 대기에 섞였다: {pending}"
    assert pending[-1] == head, "마지막이 head가 아니다 — 적용 순서가 아래→위가 아니다"


def test_pending_on_fresh_db_is_everything(script_dir):
    """신규 DB(current=None)면 전부가 대기다."""
    all_revs = list(script_dir.walk_revisions())
    pending = mig._pending_revisions(script_dir, None)

    assert len(pending) == len(all_revs), "신규 DB인데 일부만 대기로 잡혔다"
    assert pending[-1] == script_dir.get_heads()[0], "마지막이 head여야 한다"


def test_pending_is_empty_at_head(script_dir):
    """이미 head면 대기가 없다."""
    head = script_dir.get_heads()[0]

    assert mig._pending_revisions(script_dir, head) == []


def test_pending_order_is_applicable(script_dir):
    """대기 목록은 **적용 순서**(아래→위)여야 한다 — 거꾸로 보여주면 사람이 오해한다."""
    pending = mig._pending_revisions(script_dir, None)
    revmap = {s.revision: s for s in script_dir.walk_revisions()}

    for earlier, later in zip(pending, pending[1:]):
        assert revmap[later].down_revision == earlier, (
            f"{earlier} → {later} 순서가 리비전 그래프와 다르다")


# --- --apply 게이트: dev에 머지된 것만 (CONVENTIONS §7 ①) ---

@pytest.fixture
def apply_run(monkeypatch):
    """`--apply` 경로를 DB·git 없이 실행하고 (rc, run_migrations 호출 횟수)를 돌려준다.

    적용 대기가 **있는** 상황을 만든다 — 실제 공유 DB는 head라 `이미 최신`에서 early-return돼
    게이트에 **닿지도 않는다**(그걸로 "통과했다"고 착각한 적 있다).
    """
    import app.db as app_db

    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://u:p@ep-x-pooler.neon.tech/neondb")
    monkeypatch.setattr(mig, "_current_revision", lambda engine: "0014")  # → 대기 2개
    monkeypatch.setattr(app_db, "engine", create_engine("sqlite://"))  # 실 DB 접속 없음
    monkeypatch.setattr(mig, "_migrations_dirty", lambda: [])  # 기본: migrations/ 깨끗함

    calls: list = []
    monkeypatch.setattr(app_db, "run_migrations", lambda **k: calls.append(k))

    def _run(*argv: str) -> tuple[int, int]:
        calls.clear()
        monkeypatch.setattr(sys, "argv", ["migrate_shared_db.py", *argv])
        return mig.main(), len(calls)

    return _run


def test_apply_blocked_when_head_not_in_dev(apply_run, monkeypatch, capsys):
    """머지 안 된 커밋에서 `--apply`하면 **차단**한다.

    브랜치를 출력만 하고 "dev가 아니면 멈추세요"라고 부탁했었다 — 이 PR이 자동 마이그레이션을
    코드로 막은 이유와 정면으로 배치된다(규약은 지켜지지 않는다). 머지 안 된 리비전이 공유
    스키마로 올라가면 리뷰에서 까여도 되돌리기 어렵고, 다른 사람은 그 컬럼을 모른다.
    """
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: False)

    rc, applied = apply_run("--apply")

    assert rc == 1, "머지 안 된 커밋에서 --apply가 통과했다"
    assert applied == 0, "차단했다면서 마이그레이션이 실제로 돌았다"


def test_apply_allowed_when_head_is_in_dev(apply_run, monkeypatch):
    """dev에 머지된 커밋이면 적용된다 — 게이트가 기능을 죽이면 안 된다.

    이게 없으면 "차단된다"가 **스크립트가 아예 안 도는** 탓일 수도 있다.
    """
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: True)

    rc, applied = apply_run("--apply")

    assert rc == 0 and applied == 1, "머지된 커밋인데도 적용이 안 된다"


def test_apply_blocked_when_dev_state_unknown(apply_run, monkeypatch):
    """`origin/dev`를 못 읽으면 **막는다** (fail-closed).

    모르면 통과시키는 게 아니라 멈춘다 — 공유 DB는 되돌리기 어렵다.
    """
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: None)

    rc, applied = apply_run("--apply")

    assert rc == 1 and applied == 0, "dev 상태를 모르는데 적용했다"


def test_escape_hatch_overrides_the_gate(apply_run, monkeypatch):
    """탈출구 플래그는 게이트를 넘는다 — git 없는 환경 등."""
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: False)

    rc, applied = apply_run("--apply", "--skip-git-checks")

    assert rc == 0 and applied == 1


def test_dry_run_works_on_any_branch(apply_run, monkeypatch):
    """dry-run은 어느 브랜치에서든 된다 — 무엇이 올라갈지 보는 건 안전하다."""
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: False)

    rc, applied = apply_run()

    assert rc == 0 and applied == 0


def test_apply_blocked_when_migrations_uncommitted(apply_run, monkeypatch):
    """`HEAD`가 dev에 있어도 **migrations/에 미커밋 변경**이 있으면 차단한다.

    게이트는 `HEAD`(커밋 이력)를 보는데 alembic의 `ScriptDirectory`는 **작업 트리**를 읽는다.
    dev를 체크아웃한 채 `0017_실험.py`를 만들어두면 HEAD 검사는 통과하고 그 미커밋 파일이
    공유 DB에 올라간다 — 팀에는 존재하지 않는 리비전이다 (CodeRabbit #258).

    실측으로 확인했다: untracked 파일이 그대로 `get_heads()`에 잡혔다.
    """
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: True)  # HEAD 검사는 통과시킨다
    monkeypatch.setattr(mig, "_migrations_dirty",
                        lambda: ["?? migrations/versions/0017_experiment.py"])

    rc, applied = apply_run("--apply")

    assert rc == 1, "미커밋 마이그레이션이 있는데 --apply가 통과했다"
    assert applied == 0, "차단했다면서 마이그레이션이 실제로 돌았다"


def test_apply_blocked_when_migrations_status_unknown(apply_run, monkeypatch):
    """`migrations/`의 git 상태를 못 읽으면 막는다 (fail-closed)."""
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: True)
    monkeypatch.setattr(mig, "_migrations_dirty", lambda: None)

    rc, applied = apply_run("--apply")

    assert rc == 1 and applied == 0


# --- 이 스크립트가 존재하는 이유 자체 ---

def test_migrations_have_exactly_one_head(script_dir):
    """리비전 head는 **하나**여야 한다 — 둘이면 `alembic upgrade head`가 전원 기동을 막는다.

    이게 공유 DB에서 자동 마이그레이션을 끈 이유(RPA-186)이자, 이 스위트가 지켜야 할 불변식이다.
    깨지면 두 브랜치가 각자 리비전을 만든 것 — `alembic merge`가 필요하다.
    """
    heads = script_dir.get_heads()

    assert len(heads) == 1, (
        f"head가 {len(heads)}개다: {heads} — 두 브랜치가 각자 마이그레이션을 만들었다. "
        f"alembic merge로 병합할 것 (CONVENTIONS §7)")
