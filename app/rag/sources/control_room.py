"""Control Room API 클라이언트 — 봇 목록/JSON 수집, 패키지 포함 BLM export.

필요 환경변수:
  CR_URL       예: https://your-tenant.cloud.automationanywhere.digital
  CR_USERNAME
  CR_API_KEY   (또는 CR_PASSWORD)
"""

import os
import time

import httpx


class ControlRoomClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("CR_URL", "")).rstrip("/")
        if not self.base_url:
            raise RuntimeError("CR_URL 환경변수가 필요합니다")
        self._client = httpx.Client(base_url=self.base_url, timeout=60.0)
        self._token: str | None = None

    def login(self) -> None:
        username = os.getenv("CR_USERNAME", "")
        api_key = os.getenv("CR_API_KEY", "")
        password = os.getenv("CR_PASSWORD", "")
        if not username or not (api_key or password):
            raise RuntimeError("CR_USERNAME과 CR_API_KEY(또는 CR_PASSWORD)가 필요합니다")
        payload: dict = {"username": username}
        if api_key:
            payload["apiKey"] = api_key
        else:
            payload["password"] = password
            payload["multipleLogin"] = True
        # 최신 빌드(Community 포함)는 /v2/authentication, 구버전은 /v1
        resp = self._client.post("/v2/authentication", json=payload)
        if resp.status_code == 404:
            resp = self._client.post("/v1/authentication", json=payload)
        resp.raise_for_status()
        self._token = resp.json()["token"]
        self._client.headers["X-Authorization"] = self._token

    def _ensure_auth(self) -> None:
        if self._token is None:
            self.login()

    def _list_page(self, url: str, offset: int, page_size: int) -> dict:
        resp = self._client.post(url, json={"page": {"offset": offset, "length": page_size}})
        resp.raise_for_status()
        return resp.json()

    def _list_all(self, url: str, page_size: int = 200) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            data = self._list_page(url, offset, page_size)
            batch = data.get("list", [])
            items.extend(batch)
            offset += len(batch)
            total = data.get("page", {}).get("totalFilter", data.get("page", {}).get("total", 0))
            if not batch or offset >= total:
                break
        return items

    def list_bots(self, workspace: str = "public", page_size: int = 200) -> list[dict]:
        """워크스페이스 전체를 폴더 재귀로 탐색해 Task Bot 파일을 수집.

        루트 목록 API는 루트 항목만 반환하므로, 디렉터리는
        /v2/repository/folders/{id}/list 로 내려가며 훑는다.
        """
        self._ensure_auth()
        bots: list[dict] = []
        seen_bots: set = set()
        seen_dirs: set = set()
        roots = self._list_all(
            f"/v2/repository/workspaces/{workspace}/files/list", page_size
        )
        queue = list(roots)
        while queue:
            item = queue.pop(0)
            item_type = item.get("type", "")
            item_id = item.get("id")
            if item_type == "application/vnd.aa.taskbot":
                if item_id not in seen_bots:
                    seen_bots.add(item_id)
                    bots.append(item)
            elif item_type == "application/vnd.aa.directory":
                if item_id in seen_dirs:
                    continue
                seen_dirs.add(item_id)
                queue.extend(
                    self._list_all(f"/v2/repository/folders/{item_id}/list", page_size)
                )
        return bots

    def get_bot_json(self, file_id: int | str) -> dict:
        self._ensure_auth()
        resp = self._client.get(f"/v2/repository/files/{file_id}/content")
        resp.raise_for_status()
        return resp.json()

    def export_with_packages(
        self, file_ids: list[int], name: str = "rag-package-export", timeout_seconds: int = 600
    ) -> bytes:
        """BLM export(패키지 포함) 요청 → 완료 대기 → zip 바이트 반환."""
        self._ensure_auth()
        resp = self._client.post(
            "/v2/blm/export",
            json={"name": name, "fileIds": file_ids, "includePackages": True},
        )
        resp.raise_for_status()
        request_id = resp.json()["requestId"]

        deadline = time.time() + timeout_seconds
        download_id = None
        while time.time() < deadline:
            status_resp = self._client.get(f"/v2/blm/status/{request_id}")
            status_resp.raise_for_status()
            status = status_resp.json()
            state = status.get("status", "").upper()
            if state == "COMPLETED":
                download_id = status.get("downloadFileId") or request_id
                break
            if state in ("FAILED", "ERROR"):
                raise RuntimeError(f"BLM export 실패: {status}")
            time.sleep(5)
        if download_id is None:
            raise TimeoutError("BLM export가 제한 시간 내에 완료되지 않았습니다")

        dl = self._client.get(f"/v2/blm/download/{download_id}")
        dl.raise_for_status()
        return dl.content

    def close(self) -> None:
        self._client.close()
