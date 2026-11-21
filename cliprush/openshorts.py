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

    def _webhook_fields(self) -> dict:
        if self.settings.public_base_url and self.settings.openshorts_webhook_secret:
            return {
                "webhook_url": self.settings.public_base_url.rstrip("/") + "/hooks/openshorts",
                "webhook_secret": self.settings.openshorts_webhook_secret,
            }
        return {}

    def process(self, url: str) -> str:
        body = {"url": url, "acknowledged": True, "auto_hook": True, "captions": True,
                "clip_min_seconds": 15, "clip_max_seconds": 45, **self._webhook_fields()}
        response = self.client.post("/api/process", json=body)
        response.raise_for_status()
        return response.json()["job_id"]

    def process_file(self, path: str) -> str:
        data = {"acknowledged": "true", "auto_hook": "true", "captions": "true",
                "clip_min_seconds": "15", "clip_max_seconds": "45",
                **self._webhook_fields()}
        with open(path, "rb") as file:
            response = self.client.post(
                "/api/process", data=data, files={"file": ("candidate.mp4", file, "video/mp4")},
                timeout=300,
            )
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
