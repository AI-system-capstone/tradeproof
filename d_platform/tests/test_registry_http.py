"""HttpRegistry 가 B 의 API 형식(b_registry/README.md)대로 부르는지, 작은 가짜 B 서버로 확인한다."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from d_platform.api.registry import HttpRegistry, RegistryUnavailable, TransitionRejected

ALLOWED = {"approved": {"active", "rolled_back"}, "active": {"rolled_back"}}


class FakeB(BaseHTTPRequestHandler):
    bundles: dict[str, str] = {}
    calls: list[dict] = []

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        bundle_id = self.path.rsplit("/", 1)[-1]
        if bundle_id == "B-500":
            return self._send(500, {"code": "INTERNAL_ERROR", "message": "x", "target_id": None})
        if bundle_id not in self.bundles:
            return self._send(404, {"code": "NOT_FOUND", "message": "Bundle not found", "target_id": bundle_id})
        self._send(200, {"bundle_id": bundle_id, "status": self.bundles[bundle_id],
                         "model": {"model_id": "m", "uri": "s3://x"}, "list": {"list_id": "l", "uri": "s3://y"}})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        bundle_id = self.path.split("/")[2]
        self.calls.append({"path": self.path, "body": body, "x_actor": self.headers.get("X-Actor")})
        if self.headers.get("X-Actor") != body["actor"]:
            return self._send(422, {"code": "VALIDATION_ERROR", "message": "actor", "target_id": bundle_id})
        current = self.bundles.get(bundle_id)
        if body["to"] not in ALLOWED.get(current, set()):
            return self._send(409, {"code": "CONFLICT", "message": "not allowed", "target_id": bundle_id})
        self.bundles[bundle_id] = body["to"]
        self._send(200, {"from": current, "to": body["to"], "changed_at": "2026-11-15T00:00:00+00:00"})

    def log_message(self, *args):  # 테스트 출력 조용히
        return None


@pytest.fixture
def registry():
    FakeB.bundles = {"B-8": "approved", "B-9": "validated"}
    FakeB.calls = []
    server = HTTPServer(("127.0.0.1", 0), FakeB)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield HttpRegistry(f"http://127.0.0.1:{server.server_port}", timeout=2)
    server.shutdown()
    server.server_close()


def test_bundle_status(registry):
    assert registry.bundle_status("B-8") == "approved"
    assert registry.bundle_status("B-9") == "validated"
    assert registry.bundle_status("B-404") is None
    with pytest.raises(RegistryUnavailable):
        registry.bundle_status("B-500")


def test_transition_sends_to_actor_reason_and_header(registry):
    registry.transition("B-8", "active", "배포 DP-1 100% 반영 완료")
    call = FakeB.calls[0]
    assert call["path"] == "/bundles/B-8/transitions"
    assert call["body"] == {"to": "active", "actor": "d-platform", "reason": "배포 DP-1 100% 반영 완료"}
    assert call["x_actor"] == "d-platform"
    assert registry.bundle_status("B-8") == "active"
    registry.transition("B-8", "rolled_back", "배포 DP-1 롤백 (manual)")     # active -> rolled_back


def test_transition_not_allowed_is_rejected(registry):
    with pytest.raises(TransitionRejected):
        registry.transition("B-9", "active", "승인 전 번들")                 # validated -> active 는 409


def test_registry_unreachable():
    with pytest.raises(RegistryUnavailable):
        HttpRegistry("http://127.0.0.1:9", timeout=1).bundle_status("B-8")
