"""시연용 부하 생성기 (시연 장면 3·4: canary 트래픽, 성능 저하 모델 감지).

심사 API(POST /screenings)에 정해진 속도로 요청을 보내고 결과를 요약 JSON 으로 출력한다.

  python tools/loadgen.py --url http://localhost:8000 --rate 5 --duration 60
  python tools/loadgen.py --rate 10 --count 200 --concurrency 8 --mode mixed --wait
  python tools/loadgen.py --mode upload --samples TX-0001,TX-0027 --count 10 --wait

모드
  sample  POST /screenings {"sample_tx_id": "TX-0001"}            (data/docs 의 PNG 를 워커가 실제 OCR 해서 판정)
  upload  POST /screenings 멀티파트 (필드 invoice, bl = data/docs/<tx>/invoice.pdf, bl.pdf)
  mixed   sample 과 upload 를 번갈아 보냄

출력 (stdout, JSON)
  sent, accepted, failed, http_status(코드별), error_codes, bundle_id 분포·비율(canary 비율 확인용),
  --wait 면 최종 status·classification 분포와 접수(202 수신)→완료 지연 p50/p95.
진행 중에는 --log-every 초마다 stderr 에 한 줄 로그를 찍는다.

표준 라이브러리만 쓴다 (urllib, threading, json). 멀티파트도 직접 인코딩한다.
"""
from __future__ import annotations

import argparse
import heapq
import json
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402

MODES = ("sample", "upload", "mixed")
TERMINAL = (C.SCREENING_COMPLETED, C.SCREENING_FAILED)
_EXT_TYPES = {ext: ctype for ctype, ext in C.UPLOAD_CONTENT_TYPES.items()}
DOC_EXT = ".pdf"   # data/docs/<tx>/<doc_type>.pdf


# ---------------------------------------------------------------- 입력 준비
def find_samples(docs_dir: Path | str = C.DOCS_DIR, only: list[str] | None = None) -> list[str]:
    """docs_dir 의 TX-* 폴더 이름을 정렬해서 돌려준다. only 를 주면 그 거래만 (순서는 정렬)."""
    docs_dir = Path(docs_dir)
    found = sorted(p.name for p in docs_dir.glob("TX-*") if p.is_dir())
    if only:
        missing = [t for t in only if t not in found]
        if missing:
            raise SystemExit(f"[loadgen] {docs_dir} 에 없는 샘플: {', '.join(missing)}")
        found = sorted(set(only))
    if not found:
        raise SystemExit(f"[loadgen] 샘플이 없음: {docs_dir}/TX-*")
    return found


def doc_paths(docs_dir: Path | str, tx: str) -> dict[str, Path]:
    """업로드할 원문 경로 {invoice: .../invoice.pdf, bl: .../bl.pdf}"""
    return {d: Path(docs_dir) / tx / f"{d}{DOC_EXT}" for d in C.UPLOAD_DOC_TYPES}


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]],
                     boundary: str | None = None) -> tuple[bytes, str]:
    """multipart/form-data 본문을 만든다. files = {필드: (파일 이름, 바이트, content-type)}.
    (본문, Content-Type 헤더 값)을 돌려준다."""
    boundary = boundary or f"tradeproof-{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n").encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, data, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class Payloads:
    """요청 본문을 만든다. 업로드 파일은 한 번만 읽어 둔다."""

    def __init__(self, docs_dir: Path | str, samples: list[str], mode: str):
        self.docs_dir, self.samples, self.mode = Path(docs_dir), samples, mode
        self._files: dict[str, dict[str, tuple[str, bytes, str]]] = {}
        self._lock = threading.Lock()
        if mode in ("upload", "mixed"):
            for tx in samples:
                for d, p in doc_paths(self.docs_dir, tx).items():
                    if not p.is_file():
                        raise SystemExit(f"[loadgen] 업로드할 원문이 없음: {p}")

    def kind(self, i: int) -> str:
        if self.mode == "mixed":
            return "sample" if i % 2 == 0 else "upload"
        return self.mode

    def _load(self, tx: str) -> dict[str, tuple[str, bytes, str]]:
        with self._lock:
            if tx not in self._files:
                self._files[tx] = {d: (p.name, p.read_bytes(), _EXT_TYPES[p.suffix])
                                   for d, p in doc_paths(self.docs_dir, tx).items()}
            return self._files[tx]

    def build(self, i: int) -> tuple[str, str, bytes, str]:
        """i 번째 요청 -> (종류, 샘플 거래, 본문, Content-Type). 샘플은 라운드로빈."""
        tx = self.samples[i % len(self.samples)]
        kind = self.kind(i)
        if kind == "sample":
            return kind, tx, json.dumps({"sample_tx_id": tx}).encode(), "application/json"
        body, ctype = encode_multipart({}, self._load(tx))
        return kind, tx, body, ctype


# ---------------------------------------------------------------- HTTP
def http(method: str, url: str, body: bytes | None = None, ctype: str | None = None,
         timeout: float = 10.0) -> tuple[str, dict | None]:
    """(상태 키, JSON 본문). 상태 키는 HTTP 코드 문자열 또는 연결 오류 이름 ("timeout", "conn_error")."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(r.status), _json(r.read())
    except urllib.error.HTTPError as e:
        return str(e.code), _json(e.read())
    except (socket.timeout, TimeoutError):
        return "timeout", None
    except urllib.error.URLError as e:
        return ("timeout" if isinstance(e.reason, (socket.timeout, TimeoutError)) else "conn_error"), None
    except (ConnectionError, OSError):
        return "conn_error", None


def _json(raw: bytes) -> dict | None:
    try:
        v = json.loads(raw.decode("utf-8"))
        return v if isinstance(v, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def percentiles(values: list[float]) -> dict:
    """nearest-rank p50/p95/max (ms, 소수 1자리). 값이 없으면 None."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    v = sorted(values)

    def p(q):
        k = max(0, min(len(v) - 1, -(-len(v) * q // 100) - 1))
        return round(v[int(k)], 1)
    return {"n": len(v), "p50": p(50), "p95": p(95), "max": round(v[-1], 1)}


def _ratio(c: Counter) -> dict:
    total = sum(c.values())
    return {k: round(n / total, 3) for k, n in sorted(c.items())} if total else {}


# ---------------------------------------------------------------- 실행
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.sent = 0
        self.accepted = 0
        self.http_status = Counter()
        self.error_codes = Counter()
        self.bundle = Counter()
        self.kinds = Counter()
        self.post_ms: list[float] = []
        self.final_status = Counter()
        self.classification = Counter()
        self.final_bundle = Counter()
        self.wait_ms: list[float] = []

    def done_count(self) -> int:
        return sum(self.final_status.values())


class Poller:
    """접수된 거래를 GET 으로 폴링한다. 다음 폴링 시각 순서의 힙을 여러 스레드가 나눠 처리."""

    def __init__(self, base: str, stats: Stats, interval: float, timeout: float, http_timeout: float, threads: int):
        self.base, self.stats = base, stats
        self.interval, self.timeout, self.http_timeout = interval, timeout, http_timeout
        self.heap: list = []
        self.cv = threading.Condition()
        self.closed = False
        self._seq = 0
        self.threads = [threading.Thread(target=self._loop, daemon=True, name=f"poll-{i}") for i in range(threads)]
        for t in self.threads:
            t.start()

    def add(self, tx_id: str, status_url: str | None, accepted_at: float) -> None:
        url = self.base + (status_url or f"/screenings/{tx_id}")
        with self.cv:
            self._seq += 1
            heapq.heappush(self.heap, (time.monotonic() + self.interval, self._seq, tx_id, url, accepted_at))
            self.cv.notify()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def join(self) -> None:
        for t in self.threads:
            t.join()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while True:
                    if not self.heap:
                        if self.closed:
                            return
                        self.cv.wait()
                        continue
                    due = self.heap[0][0] - time.monotonic()
                    if due <= 0:
                        item = heapq.heappop(self.heap)
                        break
                    self.cv.wait(due)
            self._poll(*item[2:])

    def _poll(self, tx_id: str, url: str, accepted_at: float) -> None:
        code, body = http("GET", url, timeout=self.http_timeout)
        now = time.monotonic()
        status = (body or {}).get("status") if code == "200" else None
        if status in TERMINAL:
            with self.stats.lock:
                self.stats.final_status[status] += 1
                self.stats.classification[body.get("classification") or "none"] += 1
                self.stats.final_bundle[body.get("bundle_id") or "none"] += 1
                self.stats.wait_ms.append((now - accepted_at) * 1000)
            return
        if now - accepted_at >= self.timeout:
            with self.stats.lock:
                self.stats.final_status["timeout"] += 1
            return
        with self.cv:
            self._seq += 1
            heapq.heappush(self.heap, (now + self.interval, self._seq, tx_id, url, accepted_at))
            self.cv.notify()


def _log_line(stats: Stats, t0: float, wait: bool) -> str:
    with stats.lock:
        fail = stats.sent - stats.accepted
        bundles = " ".join(f"{k}:{n}" for k, n in sorted(stats.bundle.items())) or "-"
        line = (f"[loadgen] {time.monotonic() - t0:6.1f}s sent={stats.sent} ok={stats.accepted} "
                f"fail={fail} bundle[{bundles}]")
        if wait:
            line += f" done={stats.done_count()}/{stats.accepted}"
    return line


def run(url: str, mode: str = "sample", rate: float = 5.0, count: int | None = None,
        duration: float | None = None, concurrency: int = 4, samples: list[str] | None = None,
        docs_dir: Path | str = C.DOCS_DIR, wait: bool = False, wait_timeout: float = 60.0,
        poll_interval: float = 0.2, http_timeout: float = 10.0, log_every: float = 5.0,
        log=None) -> dict:
    """부하를 보내고 요약 dict 를 돌려준다. count 와 duration 이 모두 없으면 샘플 수만큼 보낸다."""
    if mode not in MODES:
        raise ValueError(f"mode 는 {MODES} 중 하나")
    if concurrency < 1:
        raise ValueError("concurrency 는 1 이상")
    log = log or (lambda s: print(s, file=sys.stderr, flush=True))
    samples = find_samples(docs_dir, samples)
    if count is None and duration is None:
        count = len(samples)
    payloads = Payloads(docs_dir, samples, mode)
    base = url.rstrip("/")
    stats = Stats()
    poller = Poller(base, stats, poll_interval, wait_timeout, http_timeout, concurrency) if wait else None
    jobs: queue.Queue = queue.Queue(maxsize=concurrency * 2)   # 꽉 차면 디스패처가 기다림 (목표 속도보다 느려짐)

    def worker():
        while True:
            i = jobs.get()
            if i is None:
                return
            kind, tx, body, ctype = payloads.build(i)
            t = time.monotonic()
            code, resp = http("POST", base + "/screenings", body, ctype, timeout=http_timeout)
            now = time.monotonic()
            ok = code.isdigit() and 200 <= int(code) < 300 and bool(resp) and bool(resp.get("tx_id"))
            with stats.lock:
                stats.sent += 1
                stats.kinds[kind] += 1
                stats.http_status[code] += 1
                stats.post_ms.append((now - t) * 1000)
                if ok:
                    stats.accepted += 1
                    stats.bundle[resp.get("bundle_id") or "none"] += 1
                elif resp and resp.get("code"):
                    stats.error_codes[resp["code"]] += 1
            if ok and poller:
                poller.add(resp["tx_id"], resp.get("status_url"), now)

    workers = [threading.Thread(target=worker, daemon=True, name=f"send-{i}") for i in range(concurrency)]
    for w in workers:
        w.start()

    t0 = time.monotonic()
    next_log = t0 + log_every if log_every > 0 else float("inf")
    dispatched = 0
    interrupted = False
    try:
        while True:
            now = time.monotonic()
            if count is not None and dispatched >= count:
                break
            if duration is not None and now - t0 >= duration:
                break
            if rate > 0:
                due = t0 + dispatched / rate
                if due > now:
                    time.sleep(min(due - now, max(0.0, next_log - now), 0.5) or 0.001)
                    if time.monotonic() >= next_log:
                        log(_log_line(stats, t0, wait))
                        next_log += log_every
                    continue
            jobs.put(dispatched)
            dispatched += 1
            if time.monotonic() >= next_log:
                log(_log_line(stats, t0, wait))
                next_log += log_every
    except KeyboardInterrupt:
        interrupted = True
        log("[loadgen] 중단: 보낸 것까지만 요약")
    for _ in workers:
        jobs.put(None)
    for w in workers:
        w.join()
    send_sec = time.monotonic() - t0
    if poller:
        poller.close()
        while True:
            alive = [t for t in poller.threads if t.is_alive()]
            if not alive:
                break
            alive[0].join(timeout=log_every if log_every > 0 else None)
            if log_every > 0 and alive[0].is_alive():
                log(_log_line(stats, t0, wait))
    log(_log_line(stats, t0, wait))

    with stats.lock:
        summary = {
            "url": base, "mode": mode, "target_rate": rate, "concurrency": concurrency,
            "samples": len(samples), "interrupted": interrupted,
            "elapsed_sec": round(send_sec, 2),
            "achieved_rate": round(stats.sent / send_sec, 2) if send_sec > 0 else None,
            "sent": stats.sent, "accepted": stats.accepted, "failed": stats.sent - stats.accepted,
            "by_kind": dict(sorted(stats.kinds.items())),
            "http_status": dict(sorted(stats.http_status.items())),
            "error_codes": dict(sorted(stats.error_codes.items())),
            "bundle_id": dict(sorted(stats.bundle.items())),
            "bundle_ratio": _ratio(stats.bundle),
            "post_latency_ms": percentiles(stats.post_ms),
        }
        if wait:
            summary["wait"] = {
                "timeout_sec": wait_timeout,
                "status": dict(sorted(stats.final_status.items())),
                "classification": dict(sorted(stats.classification.items())),
                "bundle_id": dict(sorted(stats.final_bundle.items())),
                "latency_ms": percentiles(stats.wait_ms),
            }
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="심사 API 시연용 부하 생성기")
    ap.add_argument("--url", default="http://localhost:8000", help="심사 API 주소")
    ap.add_argument("--rate", type=float, default=5.0, help="초당 요청 수 (0 이하 = 제한 없음)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--duration", type=float, default=None, help="보낼 시간(초)")
    g.add_argument("--count", type=int, default=None, help="보낼 건수 (둘 다 없으면 샘플 수만큼)")
    ap.add_argument("--concurrency", type=int, default=4, help="동시 요청 스레드 수 (폴링 스레드 수도 같음)")
    ap.add_argument("--mode", choices=MODES, default="sample")
    ap.add_argument("--samples", default=None, help="쉼표로 구분한 거래 ID (기본: docs-dir 의 TX-* 전체, 정렬 후 라운드로빈)")
    ap.add_argument("--docs-dir", default=str(C.DOCS_DIR), help="샘플 서류 폴더 (기본: data/docs)")
    ap.add_argument("--wait", action="store_true", help="접수 후 status 가 completed/failed 될 때까지 폴링")
    ap.add_argument("--wait-timeout", type=float, default=60.0, help="거래 하나당 폴링 최대 시간(초)")
    ap.add_argument("--poll-interval", type=float, default=0.2, help="폴링 간격(초). 지연 측정 해상도이기도 함")
    ap.add_argument("--http-timeout", type=float, default=10.0, help="요청 하나의 타임아웃(초)")
    ap.add_argument("--log-every", type=float, default=5.0, help="진행 로그 간격(초, 0 = 끔)")
    ap.add_argument("--out", default=None, help="요약 JSON 을 이 파일에도 저장")
    args = ap.parse_args(argv)
    if args.count is not None and args.count < 0:
        ap.error("--count 는 0 이상")
    if args.duration is not None and args.duration <= 0:
        ap.error("--duration 은 0 보다 커야 함")
    if args.concurrency < 1:
        ap.error("--concurrency 는 1 이상")
    only = [s.strip() for s in args.samples.split(",") if s.strip()] if args.samples else None
    summary = run(args.url, args.mode, args.rate, args.count, args.duration, args.concurrency, only,
                  args.docs_dir, args.wait, args.wait_timeout, args.poll_interval, args.http_timeout,
                  args.log_every)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if summary["sent"] == 0 or summary["accepted"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
