"""B(번들 레지스트리)와 주고받는 쪽: 번들이 배포 가능한 상태인지 묻고, 배포 결과를 알린다.

B 의 API (b/registry 브랜치의 docs/interfaces.md·common/contracts.py REGISTRY_TRANSITIONS, 2026-11-17 기준):
  GET  /bundles/{id}              -> {..., "status": "candidate|validated|approved|canary|active|..."}
  POST /bundles/{id}/transitions  <- {to, actor, reason}  -> {from, to, changed_at}
       X-Actor 헤더를 보내면 본문의 actor 와 같아야 한다. 허용되지 않는 전이는 409.
배포 시작은 B 가 한다: B 의 POST /bundles/{id}/deployments → D 의 POST /deployments → 202 를 받은 B 가 스스로
approved -> canary 로 바꾼다 (이때 직전 active 를 previous_active_id 에 적는다). B 는 다른 모듈의 canary 전이를 409 로 막는다.
D 가 보내는 전이:
  100% 반영 완료       후보 번들  canary     -> active     (B 가 직전 active 를 superseded 로)
  진행 중 중단·롤백    후보 번들  canary     -> cancelled
  100% 반영 뒤 롤백    직전 번들  superseded -> active     (B 가 지금 active 인 번들을 cancelled 로)
B 가 준비되기 전에는 REGISTRY_MODE=fake 로 FAKE_APPROVED_BUNDLES 목록만 승인된 것으로 본다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

APPROVED = "approved"
CANARY = "canary"
ACTIVE = "active"
CANCELLED = "cancelled"
ACTOR = "d-platform"


class RegistryUnavailable(Exception):
    """B 에 연결할 수 없거나 B 가 5xx 를 돌려줌 → 나중에 다시 시도할 수 있다."""


class TransitionRejected(Exception):
    """B 가 전이를 거부함 (409 등) → 다시 시도해도 같다."""


class FakeRegistry:
    kind = "fake"

    def __init__(self, approved: frozenset[str]):
        self.statuses: dict[str, str] = {b: APPROVED for b in approved}
        self.transitions: list[dict] = []     # 테스트에서 확인용

    def bundle_status(self, bundle_id: str) -> str | None:
        """번들 상태. 없는 번들이면 None."""
        return self.statuses.get(bundle_id)

    def transition(self, bundle_id: str, to: str, reason: str) -> None:
        self.transitions.append({"bundle_id": bundle_id, "to": to, "actor": ACTOR, "reason": reason})
        self.statuses[bundle_id] = to


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

    def transition(self, bundle_id: str, to: str, reason: str) -> None:
        req = urllib.request.Request(
            f"{self.base_url}/bundles/{bundle_id}/transitions", method="POST",
            data=json.dumps({"to": to, "actor": ACTOR, "reason": reason}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Actor": ACTOR},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):  # noqa: S310  내부 주소
                return None
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise RegistryUnavailable(f"registry {e.code}") from e
            raise TransitionRejected(f"registry {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise RegistryUnavailable(str(e)) from e


def make_registry(mode: str, base_url: str | None, fake_approved: frozenset[str]):
    if mode == "http":
        if not base_url:
            raise ValueError("REGISTRY_MODE=http 인데 REGISTRY_URL 이 없음")
        return HttpRegistry(base_url)
    return FakeRegistry(fake_approved)
