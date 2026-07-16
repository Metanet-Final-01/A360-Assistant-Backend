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
    monkeypatch.setattr(mig, "_revisions_not_in_dev", lambda script: [])  # 기본: dev와 일치

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


def test_apply_blocked_when_revision_files_differ_from_dev(apply_run, monkeypatch):
    """`HEAD`가 dev에 있어도 **리비전 파일이 dev와 다르면** 차단한다.

    게이트는 `HEAD`(커밋 이력)를 보는데 alembic은 **디스크**를 읽는다. dev를 체크아웃한 채
    `0017_실험.py`를 만들어두면 HEAD 검사는 통과하고 그 파일이 공유 DB에 올라간다 —
    팀에는 존재하지 않는 리비전이다 (CodeRabbit #258).
    """
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: True)  # HEAD 검사는 통과시킨다
    monkeypatch.setattr(mig, "_revisions_not_in_dev",
                        lambda script: ["migrations/versions/0017_x.py  (origin/dev에 없음)"])

    rc, applied = apply_run("--apply")

    assert rc == 1, "dev와 다른 리비전 파일이 있는데 --apply가 통과했다"
    assert applied == 0, "차단했다면서 마이그레이션이 실제로 돌았다"


def test_apply_blocked_when_dev_comparison_unknown(apply_run, monkeypatch):
    """`origin/dev`와 대조할 수 없으면 막는다 (fail-closed)."""
    monkeypatch.setattr(mig, "_head_is_in_dev", lambda: True)
    monkeypatch.setattr(mig, "_revisions_not_in_dev", lambda script: None)

    rc, applied = apply_run("--apply")

    assert rc == 1 and applied == 0


def test_revisions_not_in_dev_catches_a_real_file_git_ignores(script_dir, tmp_path):
    """**모킹 없이** 진짜 파일로 검증한다 — 위 테스트들은 `_revisions_not_in_dev`를 monkeypatch
    하므로 그 함수 자체가 옳은지는 아무것도 증명하지 못한다.

    ⚠️ 이 테스트가 존재하는 이유: 처음엔 `git status --porcelain -- migrations`로 판정했는데,
    **git이 무시하는 파일은 `??`로도 안 뜬다**. 실측에서 status는 빈 문자열인데
    `get_heads()`는 `['0099_hidden']`이었다 — 가드가 통과시킨 채 공유 DB에 올라갈 뻔했다.
    그래서 대리 지표(git status)를 버리고 **alembic이 로드한 파일 경로 자체**를 대조한다.

    origin/dev가 없는 환경(CI의 얕은 클론 등)에선 판정 불가(None)라 skip한다.
    """
    if mig._revisions_not_in_dev(script_dir) is None:
        pytest.skip("origin/dev를 확인할 수 없다 — 이 검증은 dev 참조가 필요하다")

    versions = _ROOT / "migrations" / "versions"
    probe = versions / "0099_ignored_probe.py"
    exclude = _ROOT / ".git" / "info" / "exclude"
    original = exclude.read_text(encoding="utf-8") if exclude.exists() else ""

    exclude.write_text(original + "\nmigrations/versions/0099_ignored_probe.py\n",
                       encoding="utf-8")
    probe.write_text('revision = "0099_ignored_probe"\ndown_revision = "0016"\n'
                     "def upgrade(): pass\ndef downgrade(): pass\n", encoding="utf-8")
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(_ROOT / "migrations"))
        fresh = ScriptDirectory.from_config(cfg)

        assert "0099_ignored_probe" in fresh.get_heads(), (
            "전제 확인: alembic이 무시된 파일도 읽는다 — 이게 아니면 이 구멍은 없다")

        bad = mig._revisions_not_in_dev(fresh)
        assert bad, "git이 무시하는 리비전 파일을 놓쳤다 — 공유 DB에 올라갈 수 있다"
        assert any("0099_ignored_probe" in b for b in bad)
    finally:
        probe.unlink(missing_ok=True)
        exclude.write_text(original, encoding="utf-8")


def test_revisions_not_in_dev_only_flags_files_that_differ(script_dir):
    """가드는 **dev와 다른 파일만** 잡는다 — 상시 차단이면 아무도 못 쓴다.

    ⚠️ "빈 목록이어야 한다"로 짰다가 **마이그레이션을 추가하는 모든 PR에서 CI가 깨졌다**
    (RPA-189의 0017을 만들자마자 이 테스트가 나를 막았다). feature 브랜치는 정의상 dev에 없는
    리비전을 갖고 있으므로 "깨끗함"은 dev에서만 참이다 — 테스트가 브랜치 상태에 의존하면 안 된다.

    대신 **불변식**을 검증한다: 잡힌 항목은 전부 실제로 dev와 다른 파일이어야 한다(오탐 없음).
    dev에 이미 있고 수정도 안 된 파일이 잡히면 그건 가드가 망가진 것이다.
    """
    result = mig._revisions_not_in_dev(script_dir)
    if result is None:
        pytest.skip("origin/dev를 확인할 수 없다")

    flagged = {line.split()[0] for line in result}
    for rel in flagged:
        in_dev = mig._git("rev-parse", f"origin/dev:{rel}")
        if in_dev.returncode != 0:
            continue  # dev에 없다 = 정당한 지적(새 리비전)
        local = mig._git("hash-object", str(_ROOT / rel))
        assert local.stdout.strip() != in_dev.stdout.strip(), (
            f"{rel}은 dev와 내용이 같은데 잡혔다 — 오탐이다. 가드가 상시 차단이 된다")


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
