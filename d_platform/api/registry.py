"""번들이 배포 가능한 상태(approved)인지 B(레지스트리)에 묻는 쪽.

TODO(B 확인 필요): `GET /bundles/{id}` 응답에서 상태가 담기는 필드 이름과 승인 상태값.
  지금은 응답 JSON 의 "status" 가 "approved" 라고 **가정**했다. B 의 API 가 정해지면 이 파일만 고친다.
B 가 준비되기 전에는 REGISTRY_MODE=fake 로 FAKE_APPROVED_BUNDLES 목록만 승인된 것으로 본다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

APPROVED = "approved"


class RegistryUnavailable(Exception):
    pass


class FakeRegistry:
    kind = "fake"

    def __init__(self, approved: frozenset[str]):
        self.approved = approved

    def bundle_status(self, bundle_id: str) -> str | None:
        """번들 상태. 없는 번들이면 None."""
        return APPROVED if bundle_id in self.approved else None


class HttpRegistry:
    kind = "http"

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def bundle_status(self, bundle_id: str) -> str | None:
        url = f"{self.base_url}/bundles/{bundle_id}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310  내부 주소
                return json.loads(resp.read()).get("status")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RegistryUnavailable(f"registry {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise RegistryUnavailable(str(e)) from e


def make_registry(mode: str, base_url: str | None, fake_approved: frozenset[str]):
    if mode == "http":
        if not base_url:
            raise ValueError("REGISTRY_MODE=http 인데 REGISTRY_URL 이 없음")
        return HttpRegistry(base_url)
    return FakeRegistry(fake_approved)
