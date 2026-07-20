"""Automation Anywhere 공개 GitHub에서 실제 봇/패키지를 수집한다.

Community Edition은 BLM export가 막혀 있지만, AA 공식 GitHub 조직에는
- 패키지 저장소의 빌드된 JAR (build/libs/*.jar) → 액션 스키마
- export된 봇 zip (내부에 봇 JSON + 의존 패키지 JAR) → 봇 예시 + 액션 스키마
가 공개돼 있어, 계정/라이선스 없이 두 층을 대량 확보할 수 있다.

산출물:
- data/ingest/gh_jars/*.jar      (패키지 스키마용 — parse-jars가 소비)
- data/ingest/bots.jsonl 에 append (봇 예시 — build가 소비)
"""

import io
import json
import time
import zipfile
from pathlib import Path

import httpx

from .. import config

ORG = "AutomationAnywhere"
GH_API = "https://api.github.com"
GH_RAW = "https://raw.githubusercontent.com"

# export zip 안에서 봇 파일이 위치하는 경로 조각 (확장자 없는 JSON 파일)
_BOT_PATH_HINT = "/Bots/"


def _client(token: str | None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "a360-ingest"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=120.0, follow_redirects=True)


def _get_json(client: httpx.Client, url: str, retries: int = 3):
    for attempt in range(retries):
        resp = client.get(url)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("x-ratelimit-reset", "0"))
            wait = max(reset - time.time(), 5)
            raise RuntimeError(
                f"GitHub API rate limit. GITHUB_TOKEN 환경변수를 설정하면 한도가 커집니다. "
                f"(약 {int(wait)}초 후 초기화)"
            )
        if resp.status_code in (429, 502, 503):
            time.sleep(2**attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"GitHub API 실패: {url}")


def list_org_repos(client: httpx.Client) -> list[dict]:
    repos = []
    page = 1
    while True:
        batch = _get_json(
            client, f"{GH_API}/orgs/{ORG}/repos?per_page=100&type=public&page={page}"
        )
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def list_repo_tree(client: httpx.Client, repo: str, branch: str) -> list[dict]:
    try:
        data = _get_json(
            client, f"{GH_API}/repos/{ORG}/{repo}/git/trees/{branch}?recursive=1"
        )
    except httpx.HTTPStatusError:
        return []
    return data.get("tree", [])


def _download(client: httpx.Client, repo: str, branch: str, path: str) -> bytes:
    # raw.githubusercontent.com은 공백을 %20으로 인코딩해야 함
    from urllib.parse import quote

    url = f"{GH_RAW}/{ORG}/{repo}/{branch}/{quote(path)}"
    resp = client.get(url)
    resp.raise_for_status()
    return resp.content


def _extract_bots_from_zip(data: bytes, repo: str) -> list[dict]:
    """export zip 안의 봇 JSON 파일들을 봇 레코드로 변환."""
    bots = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return bots
    for name in z.namelist():
        if _BOT_PATH_HINT not in name or name.endswith("/"):
            continue
        try:
            content = z.read(name).decode("utf-8")
            bot_json = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(bot_json, dict) or "nodes" not in bot_json:
            continue
        bot_name = name.split("/")[-1]
        bots.append(
            {
                "file_id": f"gh:{repo}:{bot_name}",
                "name": bot_name,
                "path": name,
                "workspace": f"github/{repo}",
                "json": bot_json,
            }
        )
    return bots


def _extract_jars_from_zip(data: bytes) -> list[tuple[str, bytes]]:
    jars = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return jars
    for name in z.namelist():
        if name.lower().endswith(".jar"):
            jars.append((Path(name).name, z.read(name)))
    return jars


def _is_package_jar(jar_bytes: bytes) -> bool:
    """package.json이 있는 진짜 패키지 JAR만 골라낸다 (의존 라이브러리 JAR 제외)."""
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as z:
            return "package.json" in z.namelist()
    except zipfile.BadZipFile:
        return False


def harvest(token: str | None = None, max_repos: int | None = None, on_log=print) -> dict:
    jar_dir = config.DATA_DIR / "gh_jars"
    jar_dir.mkdir(parents=True, exist_ok=True)
    config.BOTS_JSONL.parent.mkdir(parents=True, exist_ok=True)

    # 이미 수집한 봇 id (중복 방지, 로컬 CR 봇과 공존)
    seen_bot_ids: set[str] = set()
    if config.BOTS_JSONL.exists():
        with open(config.BOTS_JSONL, encoding="utf-8") as f:
            for line in f:
                try:
                    seen_bot_ids.add(str(json.loads(line)["file_id"]))
                except (json.JSONDecodeError, KeyError):
                    continue
    saved_jars: set[str] = {p.name for p in jar_dir.glob("*.jar")}

    stats = {"repos": 0, "jars": 0, "bots": 0, "zips": 0}
    client = _client(token)
    try:
        repos = list_org_repos(client)
        if max_repos:
            repos = repos[:max_repos]
        on_log(f"저장소 {len(repos)}개 스캔")

        bot_out = open(config.BOTS_JSONL, "a", encoding="utf-8")
        try:
            for repo in repos:
                name, branch = repo["name"], repo.get("default_branch", "main")
                tree = list_repo_tree(client, name, branch)
                jar_paths = [n["path"] for n in tree if n["path"].lower().endswith(".jar")]
                zip_paths = [n["path"] for n in tree if n["path"].lower().endswith(".zip")]
                if not jar_paths and not zip_paths:
                    continue
                stats["repos"] += 1
                on_log(f"[{name}] jar {len(jar_paths)}개, zip {len(zip_paths)}개")

                # 1) 저장소에 직접 있는 패키지 JAR
                for path in jar_paths:
                    fname = f"{name}__{Path(path).name}"
                    if fname in saved_jars:
                        continue
                    try:
                        data = _download(client, name, branch, path)
                    except httpx.HTTPStatusError:
                        continue
                    if _is_package_jar(data):
                        (jar_dir / fname).write_bytes(data)
                        saved_jars.add(fname)
                        stats["jars"] += 1

                # 2) export zip → 내부 봇 JSON + 패키지 JAR
                for path in zip_paths:
                    try:
                        data = _download(client, name, branch, path)
                    except httpx.HTTPStatusError:
                        continue
                    stats["zips"] += 1
                    for bot in _extract_bots_from_zip(data, name):
                        if bot["file_id"] in seen_bot_ids:
                            continue
                        seen_bot_ids.add(bot["file_id"])
                        bot_out.write(json.dumps(bot, ensure_ascii=False) + "\n")
                        stats["bots"] += 1
                    for jar_name, jar_bytes in _extract_jars_from_zip(data):
                        fname = f"{name}__{jar_name}"
                        if fname in saved_jars or not _is_package_jar(jar_bytes):
                            continue
                        (jar_dir / fname).write_bytes(jar_bytes)
                        saved_jars.add(fname)
                        stats["jars"] += 1
        finally:
            bot_out.close()
    finally:
        client.close()

    return {**stats, "jar_dir": str(jar_dir)}
