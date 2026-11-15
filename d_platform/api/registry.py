"""B(번들 레지스트리)와 주고받는 쪽: 번들이 배포 가능한 상태인지 묻고, 반영 완료·롤백을 알린다.

B 의 API (b_registry/README.md, PR #8 기준):
  GET  /bundles/{id}              -> {..., "status": "candidate|validated|approved|active|..."}
  POST /bundles/{id}/transitions  <- {to, actor, reason}  -> {from, to, changed_at}
       허용 전이 중 D 가 쓰는 것: approved -> active (반영 완료), approved|active -> rolled_back (롤백)
       X-Actor 헤더를 보내면 본문의 actor 와 같아야 한다. 허용되지 않는 전이는 409.
       B 는 번들이 active 가 될 때 직전 active 를 bundles.previous_active_id 에 적어 두고,
       active -> rolled_back 을 받으면 그 번들을 스스로 다시 active 로 복원한다.
       → D 는 후보 번들에 rolled_back 하나만 보낸다 (직전 번들에 대한 전이는 보내지 않는다).
B 가 준비되기 전에는 REGISTRY_MODE=fake 로 FAKE_APPROVED_BUNDLES 목록만 승인된 것으로 본다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

APPROVED = "approved"
ACTIVE = "active"
ROLLED_BACK = "rolled_back"
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
