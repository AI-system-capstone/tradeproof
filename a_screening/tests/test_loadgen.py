"""부하 생성기 테스트. http.server 로 띄운 가짜 심사 API(스레드)에 보낸다.

  python -m pytest tools -q
"""
from __future__ import annotations

import json
import sys
import threading
from email import message_from_bytes
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from a_screening.tools import loadgen as L  # noqa: E402

CANARY = "B-9"
MISSING_TX = "TX-0003"      # 가짜 API 가 404 로 답하는 샘플
POLLS_UNTIL_DONE = 2        # GET 몇 번째에 completed 가 되는지


def parse_multipart(body: bytes, ctype: str) -> dict[str, tuple[str | None, bytes, str]]:
    """{필드: (파일 이름, 바이트, content-type)}"""
    msg = message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body, policy=HTTP)
    assert msg.is_multipart()
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = (part.get_filename(), part.get_payload(decode=True), part.get_content_type())
    return out


class FakeApi:
    """POST /screenings 202 (4건 중 1건은 canary 번들), GET 은 POLLS_UNTIL_DONE 번째에 completed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.posts: list[dict] = []
        self.jobs: dict[str, dict] = {}
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if self.path != "/screenings":
                    return self._send(404, {"code": C.ERR_NOT_FOUND, "message": "없음", "target_id": None})
                body = self.rfile.read(int(self.headers["Content-Length"]))
                ctype = self.headers["Content-Type"]
                if ctype.startswith("application/json"):
                    req = {"kind": "sample", "sample_tx_id": json.loads(body)["sample_tx_id"]}
                else:
                    req = {"kind": "upload", "parts": parse_multipart(body, ctype), "ctype": ctype}
                with api.lock:
                    api.posts.append(req)
                    n = len(api.posts)
                if req.get("sample_tx_id") == MISSING_TX:
                    return self._send(404, {"code": C.ERR_NOT_FOUND, "message": "샘플 없음", "target_id": MISSING_TX})
                tx_id = f"TX-FAKE-{n:04d}"
                bundle = CANARY if n % 4 == 0 else C.BASELINE_BUNDLE
                cls = C.HOLD if req.get("sample_tx_id") == "TX-0002" else C.PASS
                with api.lock:
                    api.jobs[tx_id] = {"polls": 0, "bundle_id": bundle, "classification": cls}
                self._send(202, {"tx_id": tx_id, "status": C.SCREENING_QUEUED, "bundle_id": bundle,
                                 "status_url": f"/screenings/{tx_id}"})

            def do_GET(self):
                tx_id = self.path.rsplit("/", 1)[-1]
                with api.lock:
                    job = api.jobs.get(tx_id)
                    if job is None:
                        return self._send(404, {"code": C.ERR_NOT_FOUND, "message": "없음", "target_id": tx_id})
                    job["polls"] += 1
                    done = job["polls"] >= POLLS_UNTIL_DONE
                self._send(200, {"tx_id": tx_id, "bundle_id": job["bundle_id"],
                                 "status": C.SCREENING_COMPLETED if done else C.SCREENING_PROCESSING,
                                 "classification": job["classification"] if done else None})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def docs(tmp_path):
    """가짜 샘플 서류 4건 (파일 바이트가 거래·서류마다 다름)."""
    for i in (1, 2, 3, 4):
        d = tmp_path / f"TX-{i:04d}"
        d.mkdir()
        for doc in C.UPLOAD_DOC_TYPES:
            (d / f"{doc}.pdf").write_bytes(b"%PDF-1.4\n" + f"{d.name}/{doc}\r\n--x\x00".encode() + bytes(range(256)))
    (tmp_path / "not-a-tx").mkdir()
    return tmp_path


@pytest.fixture
def api():
    with FakeApi() as a:
        yield a


def _run(api, docs, **kw):
    kw.setdefault("rate", 0)
    kw.setdefault("concurrency", 3)
    kw.setdefault("log_every", 0)
    kw.setdefault("poll_interval", 0.01)
    return L.run(api.url, docs_dir=docs, log=lambda s: None, **kw)


def test_find_samples_sorted(docs):
    assert L.find_samples(docs) == ["TX-0001", "TX-0002", "TX-0003", "TX-0004"]
    assert L.find_samples(docs, ["TX-0004", "TX-0002"]) == ["TX-0002", "TX-0004"]
    with pytest.raises(SystemExit):
        L.find_samples(docs, ["TX-9999"])


def test_sample_mode_summary(api, docs):
    s = _run(api, docs, mode="sample", count=8, wait=True)
    # 라운드로빈: 4건씩 두 바퀴
    assert sorted(p["sample_tx_id"] for p in api.posts) == sorted(["TX-0001", "TX-0002", "TX-0003", "TX-0004"] * 2)
    assert s["sent"] == 8 and s["accepted"] == 6 and s["failed"] == 2
    assert s["http_status"] == {"202": 6, "404": 2}
    assert s["error_codes"] == {C.ERR_NOT_FOUND: 2}
    assert s["by_kind"] == {"sample": 8}
    # 가짜 API 는 POST 순번 4의 배수에 canary 번들을 준다 (404 포함 순번이라 정확히 2건 중 0~2건)
    assert sum(s["bundle_id"].values()) == 6
    assert set(s["bundle_id"]) <= {C.BASELINE_BUNDLE, CANARY}
    assert abs(sum(s["bundle_ratio"].values()) - 1) < 0.01
    w = s["wait"]
    assert w["status"] == {C.SCREENING_COMPLETED: 6}
    assert w["classification"] == {C.HOLD: 2, C.PASS: 4}
    assert w["bundle_id"] == s["bundle_id"]
    assert w["latency_ms"]["n"] == 6 and w["latency_ms"]["p50"] <= w["latency_ms"]["p95"]
    assert s["post_latency_ms"]["n"] == 8


def test_canary_ratio_sequential(api, docs):
    s = _run(api, docs, mode="sample", count=8, concurrency=1, samples=["TX-0001", "TX-0002"])
    assert s["bundle_id"] == {C.BASELINE_BUNDLE: 6, CANARY: 2}
    assert s["bundle_ratio"] == {C.BASELINE_BUNDLE: 0.75, CANARY: 0.25}
    assert "wait" not in s


def test_upload_mode_multipart(api, docs):
    s = _run(api, docs, mode="upload", count=4, wait=True)
    assert s["sent"] == 4 and s["accepted"] == 4 and s["by_kind"] == {"upload": 4}
    assert s["wait"]["status"] == {C.SCREENING_COMPLETED: 4}
    seen = set()
    for p in api.posts:
        assert p["kind"] == "upload" and p["ctype"].startswith("multipart/form-data; boundary=")
        parts = p["parts"]
        assert set(parts) == set(C.UPLOAD_DOC_TYPES)
        tx = parts["invoice"][1].split(b"\n")[1].split(b"/")[0].decode()
        seen.add(tx)
        for doc, (filename, data, ctype) in parts.items():
            assert filename == f"{doc}.pdf"
            assert ctype == "application/pdf" and C.UPLOAD_CONTENT_TYPES[ctype] == ".pdf"
            assert data == (docs / tx / f"{doc}.pdf").read_bytes()   # 바이트 그대로
    assert seen == {"TX-0001", "TX-0002", "TX-0003", "TX-0004"}


def test_mixed_mode_alternates(api, docs):
    s = _run(api, docs, mode="mixed", count=6, concurrency=1)
    assert [p["kind"] for p in api.posts] == ["sample", "upload"] * 3
    assert s["by_kind"] == {"sample": 3, "upload": 3}


def test_encode_multipart_format():
    body, ctype = L.encode_multipart({"note": "가"}, {"invoice": ("invoice.pdf", b"\x00\r\nAB", "application/pdf")},
                                     boundary="BND")
    assert ctype == "multipart/form-data; boundary=BND"
    assert body == (b'--BND\r\nContent-Disposition: form-data; name="note"\r\n\r\n' + "가".encode() + b"\r\n"
                    b'--BND\r\nContent-Disposition: form-data; name="invoice"; filename="invoice.pdf"\r\n'
                    b"Content-Type: application/pdf\r\n\r\n\x00\r\nAB\r\n--BND--\r\n")
    parts = parse_multipart(body, ctype)
    assert parts["invoice"] == ("invoice.pdf", b"\x00\r\nAB", "application/pdf")


def test_rate_and_duration(api, docs):
    s = _run(api, docs, mode="sample", rate=20, duration=0.5, samples=["TX-0001"])
    assert 8 <= s["sent"] <= 12          # 20/s x 0.5s = 10
    assert s["accepted"] == s["sent"]


def test_wait_timeout(api, docs, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "POLLS_UNTIL_DONE", 10 ** 6)
    s = _run(api, docs, mode="sample", count=2, samples=["TX-0001"], wait=True, wait_timeout=0.2)
    assert s["wait"]["status"] == {"timeout": 2}
    assert s["wait"]["latency_ms"]["n"] == 0


def test_connection_error(docs):
    s = L.run("http://127.0.0.1:9", docs_dir=docs, mode="sample", count=2, rate=0, log=lambda s: None,
              http_timeout=1, log_every=0)
    assert s["sent"] == 2 and s["accepted"] == 0
    assert set(s["http_status"]) <= {"conn_error", "timeout"}


def test_main_cli(api, docs, capsys, tmp_path):
    out = tmp_path / "summary.json"
    rc = L.main(["--url", api.url, "--docs-dir", str(docs), "--count", "3", "--rate", "0",
                 "--samples", "TX-0001,TX-0002", "--log-every", "0", "--out", str(out)])
    assert rc == 0
    s = json.loads(capsys.readouterr().out)
    assert s["sent"] == 3 and json.loads(out.read_text()) == s
