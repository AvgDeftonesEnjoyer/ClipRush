import httpx

from cliprush.config import get_settings


class OpenShorts:
    def __init__(self, client: httpx.Client | None = None):
        self.settings = get_settings()
        self.client = client or httpx.Client(
            base_url=self.settings.openshorts_api_url.rstrip("/"),
            timeout=self.settings.request_timeout_seconds,
            headers=(
                {"Authorization": f"Bearer {self.settings.openshorts_api_key}"}
                if self.settings.openshorts_api_key else {}
            ),
        )

    def process(self, url: str) -> str:
        body = {"url": url, "acknowledged": True, "auto_hook": True, "captions": True}
        if self.settings.public_base_url:
            body["webhook_url"] = self.settings.public_base_url.rstrip("/") + "/hooks/openshorts"
            if self.settings.openshorts_webhook_secret:
                body["webhook_secret"] = self.settings.openshorts_webhook_secret
        response = self.client.post("/api/process", json=body)
        response.raise_for_status()
        return response.json()["job_id"]

    def status(self, job_id: str) -> dict:
        response = self.client.get(f"/api/status/{job_id}")
        response.raise_for_status()
        return response.json()

    def publish(self, job_id: str, clip_index: int, platforms: list[str], title: str) -> dict:
        response = self.client.post(
            "/api/social/post",
            json={"job_id": job_id, "clip_index": clip_index, "platforms": platforms, "title": title},
        )
        response.raise_for_status()
        return response.json()
