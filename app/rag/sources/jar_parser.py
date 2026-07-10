"""A360 패키지 JAR에서 액션 스키마를 추출한다.

패키지 JAR 안의 package.json이 Control Room이 봇 편집기 UI를 그릴 때 쓰는
공식 디스크립터다: 액션별 파라미터명·타입·기본값·필수 규칙·리턴 타입이 모두 들어 있다.
라벨은 locales/<locale>.json의 키를 [[key]] 형태로 참조한다.
선호 로케일에 없는 키는 en_US → 속성 name 순으로 폴백한다 (미해석 [[key]]가 남지 않도록).

입력은 .jar 파일, .jar가 들어있는 디렉터리, 또는 BLM export .zip 모두 가능.
"""

import io
import json
import re
import zipfile
from pathlib import Path

_PLACEHOLDER = re.compile(r"^\[\[(.+)\]\]$")


def _resolve(value, locale_chain: list[dict], fallback: str | None = None) -> str:
    if not isinstance(value, str):
        return value
    match = _PLACEHOLDER.match(value)
    if not match:
        return value
    key = match.group(1)
    for labels in locale_chain:
        if key in labels:
            return labels[key]
    return value if fallback is None else fallback


def _resolve_deep(value, locale_chain: list[dict]):
    """defaultValue처럼 중첩 구조 안에 로케일 참조가 들어있는 값을 재귀 해석한다."""
    if isinstance(value, dict):
        return {k: _resolve_deep(v, locale_chain) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_deep(v, locale_chain) for v in value]
    return _resolve(value, locale_chain)


def _load_locales(jar: zipfile.ZipFile) -> dict[str, dict]:
    locales: dict[str, dict] = {}
    for name in jar.namelist():
        if name.startswith("locales/") and name.endswith(".json"):
            locale = Path(name).stem
            try:
                locales[locale] = json.loads(jar.read(name).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    return locales


def _locale_chain(locales: dict[str, dict], preferred: str) -> list[dict]:
    chain = [locales[c] for c in dict.fromkeys((preferred, "en_US")) if c in locales]
    if not chain and locales:
        chain.append(next(iter(locales.values())))
    return chain


def _normalize_attribute(attr: dict, locale_chain: list[dict]) -> dict:
    rules = [r.get("name") for r in attr.get("rules", []) if isinstance(r, dict)]
    param = {
        "name": _resolve(attr.get("name"), locale_chain),
        "label": _resolve(attr.get("label", ""), locale_chain, fallback=attr.get("name") or ""),
        "description": _resolve(attr.get("description", ""), locale_chain, fallback=""),
        "type": attr.get("type"),
        "required": "NOT_EMPTY" in rules,
        "rules": rules,
    }
    if "defaultValue" in attr:
        param["default"] = _resolve_deep(attr["defaultValue"], locale_chain)
    if "options" in attr:
        param["options"] = [
            {
                "label": _resolve(
                    o.get("label", ""), locale_chain, fallback=str(o.get("value") or "")
                ),
                "value": o.get("value"),
            }
            if isinstance(o, dict)
            else o
            for o in attr["options"]
        ]
    return param


def _dedupe_actions_by_name(actions: list[dict], package_name: str, source_name: str) -> list[dict]:
    """일부 JAR은 자체 `commands` 배열 안에 같은 액션 이름을 파라미터 개수가 다른
    여러 버전으로 중복 정의해 둔다(실측: 커뮤니티 WebAutomation JAR, 17개 액션이
    구형/신형 두 버전으로 중복 — 하나는 파라미터가 더 적은 구버전, 하나는 더 많은
    신버전이었다). 두 스키마를 억지로 합치면(union) 실제로 존재하지 않는 파라미터
    조합이 되어 봇 생성이 깨질 수 있으므로, 실제 존재하는 스키마 중 파라미터가 더
    많은(더 완전한) 쪽을 그대로 채택한다. downstream(BackendCatalog 등) 전체가
    (package_name, action_name)만으로 액션을 유일 식별하므로, 파싱 시점에 미리
    하나로 정리해 두지 않으면 이후 단계에서 id 충돌로 build가 막힌다.
    """
    by_name: dict[str, dict] = {}
    for action in actions:
        name = action.get("name")
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = action
            continue

        existing_count = len(existing.get("parameters", []))
        new_count = len(action.get("parameters", []))
        # 승패와 무관하게 항상 로그를 남긴다 — 파라미터 개수가 같은 순수 중복이거나
        # 새 액션 쪽이 더 적은 경우도 실제로 중복이 있었다는 사실 자체는 남아야 한다
        # (그래야 이 함수의 목적인 "중복 정의 발견"이 어떤 경우에도 가시화된다).
        print(
            f"  [경고] {package_name}({source_name}): 액션 '{name}' 중복 정의 발견, "
            f"파라미터 {existing_count}개 버전과 {new_count}개 버전 중 "
            f"{max(existing_count, new_count)}개 버전 채택"
        )
        if new_count > existing_count:
            by_name[name] = action
    return list(by_name.values())


def parse_jar_bytes(data: bytes, source_name: str, preferred_locale: str = "ko_KR") -> dict | None:
    with zipfile.ZipFile(io.BytesIO(data)) as jar:
        if "package.json" not in jar.namelist():
            return None
        pkg = json.loads(jar.read("package.json").decode("utf-8"))
        locales = _load_locales(jar)
        chain = _locale_chain(locales, preferred_locale)

        actions = []
        for cmd in pkg.get("commands", []):
            actions.append(
                {
                    # 일부 커뮤니티 패키지는 name까지 로케일 참조로 넣는다 (예: Twilio)
                    "name": _resolve(cmd.get("name"), chain),
                    "label": _resolve(cmd.get("label", ""), chain, fallback=cmd.get("name") or ""),
                    "description": _resolve(cmd.get("description", ""), chain, fallback=""),
                    "return_type": cmd.get("returnType"),
                    "return_label": _resolve(cmd.get("returnLabel", ""), chain, fallback=""),
                    "return_required": cmd.get("returnRequired", False),
                    "parameters": [
                        _normalize_attribute(a, chain) for a in cmd.get("attributes", [])
                    ],
                }
            )
        actions = _dedupe_actions_by_name(actions, pkg.get("name") or source_name, source_name)

        return {
            "package_name": pkg.get("name"),
            "package_label": _resolve(pkg.get("label", ""), chain, fallback=pkg.get("name") or ""),
            "package_description": _resolve(pkg.get("description", ""), chain, fallback=""),
            "package_version": pkg.get("packageVersion"),
            "source_jar": source_name,
            "actions": actions,
        }


def _iter_jar_bytes(path: Path):
    """경로에서 (이름, jar 바이트) 를 순회. zip이면 내부의 .jar들을 꺼낸다."""
    if path.is_dir():
        for jar_path in sorted(path.rglob("*.jar")):
            yield jar_path.name, jar_path.read_bytes()
    elif path.suffix.lower() == ".jar":
        yield path.name, path.read_bytes()
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".jar"):
                    yield Path(name).name, z.read(name)
    else:
        raise ValueError(f"unsupported input: {path}")


def _version_sort_key(version: str | None) -> tuple:
    """"5.3.0"이나 "2.3.0-20210118-185335" 같은 버전 문자열을 비교 가능한 튜플로
    바꾼다. 앞의 점(.) 구분 숫자를 주 기준으로, 뒤에 붙은 날짜(YYYYMMDD-HHMMSS)를
    보조 기준으로 쓴다.
    """
    if not version:
        return ((), 0)
    main, _, suffix = version.partition("-")
    main_parts = tuple(int(x) for x in main.split(".") if x.isdigit())
    suffix_digits = "".join(ch for ch in suffix if ch.isdigit())
    return (main_parts, int(suffix_digits) if suffix_digits else 0)


def select_better_version(a: dict, b: dict) -> dict:
    """같은 package_name의 패키지가 여러 버전으로 발견되면(실측: GitHub의 여러 봇
    저장소가 각자 다른 시기에 만들어져 서로 다른 버전을 번들하고 있었다 — 예:
    Number 패키지가 9개 저장소에서 v2.0.0~v3.8.0까지 제각각 발견됨) 더 높은 버전을
    채택한다. downstream(BackendCatalog 등) 전체가 (package_name, action_name)으로만
    액션을 조회하고 버전 차원이 없어서, 여러 버전을 다 살려둬도 어느 걸 쓸지 판단할
    근거가 없다 — 그래서 실제 action_schema/package_overview에는 채택된 버전만 반영하고,
    나머지는 지우지 않고 `other_versions_seen`에 정보만 남긴다(원본 JAR도 gh_jars/에
    그대로 있으니 나중에 필요해지면 다시 파싱해 살릴 수 있다).

    같은 버전을 같은 소스 JAR에서 다시 파싱한 경우(예: parse-jars를 같은 디렉터리에
    재실행)는 새로 배울 게 없으므로 other_versions_seen에 자기 자신을 중복 기록하지
    않는다.
    """
    if a.get("package_version") == b.get("package_version") and a.get("source_jar") == b.get("source_jar"):
        return a
    if _version_sort_key(b.get("package_version")) > _version_sort_key(a.get("package_version")):
        winner, loser = b, a
    else:
        winner, loser = a, b
    print(
        f"  [정보] {winner.get('package_name')}: 버전 {loser.get('package_version')} 대신 "
        f"{winner.get('package_version')} 채택 (그 외 버전은 metadata.other_versions_seen에 기록)"
    )
    winner = dict(winner)
    seen = list(winner.get("other_versions_seen", []))
    seen.append(
        {
            "package_version": loser.get("package_version"),
            "source_jar": loser.get("source_jar"),
            "action_count": len(loser.get("actions", [])),
        }
    )
    seen.extend(loser.get("other_versions_seen", []))
    winner["other_versions_seen"] = seen
    return winner


def _select_latest_per_package(packages: list[dict]) -> list[dict]:
    by_name: dict[str, dict] = {}
    for pkg in packages:
        name = pkg.get("package_name")
        by_name[name] = pkg if name not in by_name else select_better_version(by_name[name], pkg)
    return list(by_name.values())


def parse_packages(paths: list[Path], preferred_locale: str = "ko_KR") -> list[dict]:
    packages = []
    for path in paths:
        for name, data in _iter_jar_bytes(path):
            try:
                pkg = parse_jar_bytes(data, name, preferred_locale)
            except zipfile.BadZipFile:
                continue
            if pkg:
                packages.append(pkg)
    return _select_latest_per_package(packages)
