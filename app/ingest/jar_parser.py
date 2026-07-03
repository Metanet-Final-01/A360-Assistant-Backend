"""A360 패키지 JAR에서 액션 스키마를 추출한다.

패키지 JAR 안의 package.json이 Control Room이 봇 편집기 UI를 그릴 때 쓰는
공식 디스크립터다: 액션별 파라미터명·타입·기본값·필수 규칙·리턴 타입이 모두 들어 있다.
라벨은 locales/<locale>.json의 키를 [[key]] 형태로 참조한다.

입력은 .jar 파일, .jar가 들어있는 디렉터리, 또는 BLM export .zip 모두 가능.
"""

import io
import json
import re
import zipfile
from pathlib import Path

_PLACEHOLDER = re.compile(r"^\[\[(.+)\]\]$")


def _resolve(value, locales: dict) -> str:
    if not isinstance(value, str):
        return value
    match = _PLACEHOLDER.match(value)
    if not match:
        return value
    return locales.get(match.group(1), value)


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


def _pick_locale(locales: dict[str, dict], preferred: str) -> dict:
    for candidate in (preferred, "en_US"):
        if candidate in locales:
            return locales[candidate]
    return next(iter(locales.values()), {})


def _normalize_attribute(attr: dict, labels: dict) -> dict:
    rules = [r.get("name") for r in attr.get("rules", []) if isinstance(r, dict)]
    param = {
        "name": attr.get("name"),
        "label": _resolve(attr.get("label", ""), labels),
        "description": _resolve(attr.get("description", ""), labels),
        "type": attr.get("type"),
        "required": "NOT_EMPTY" in rules,
        "rules": rules,
    }
    if "defaultValue" in attr:
        param["default"] = attr["defaultValue"]
    if "options" in attr:
        param["options"] = [
            {
                "label": _resolve(o.get("label", ""), labels),
                "value": o.get("value"),
            }
            if isinstance(o, dict)
            else o
            for o in attr["options"]
        ]
    return param


def parse_jar_bytes(data: bytes, source_name: str, preferred_locale: str = "ko_KR") -> dict | None:
    with zipfile.ZipFile(io.BytesIO(data)) as jar:
        if "package.json" not in jar.namelist():
            return None
        pkg = json.loads(jar.read("package.json").decode("utf-8"))
        locales = _load_locales(jar)
        labels = _pick_locale(locales, preferred_locale)

        actions = []
        for cmd in pkg.get("commands", []):
            actions.append(
                {
                    "name": cmd.get("name"),
                    "label": _resolve(cmd.get("label", ""), labels),
                    "description": _resolve(cmd.get("description", ""), labels),
                    "return_type": cmd.get("returnType"),
                    "return_label": _resolve(cmd.get("returnLabel", ""), labels),
                    "return_required": cmd.get("returnRequired", False),
                    "parameters": [
                        _normalize_attribute(a, labels) for a in cmd.get("attributes", [])
                    ],
                }
            )

        return {
            "package_name": pkg.get("name"),
            "package_label": _resolve(pkg.get("label", ""), labels),
            "package_description": _resolve(pkg.get("description", ""), labels),
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
    return packages
