"""검증 Job 진입점: 골든셋 검증(VAL-01) + Replay(VAL-02·03) 한 번.

B(M2)가 검증할 번들마다 C 의 Job 양식(c_replay/job/job-template.json)에 아래 환경변수를 넣어
K8s Job(또는 docker 컨테이너)을 만든다 (b_registry/verify/replay.py). 같은 명령을 일반 프로세스로도 실행할 수 있다.

  VALIDATION_ID=RP-... BUNDLE_ID=B-9 BASELINE_BUNDLE_ID=B-7 DATABASE_URL=... \
  VALIDATION_CALLBACK_URL=http://b-registry:8100/bundles/B-9/validation-results/RP-... python -m c_replay.job

환경변수
  BUNDLE_ID                검증할 후보 번들 (필수)
  DATABASE_URL             C 의 validations + A 의 거래·판정·golden_labels (필수)
  VALIDATION_ID            B 가 만든 검증 ID. C 는 이 ID 로 결과를 저장한다. 없으면 직접 만든다 (손으로 돌릴 때)
  BASELINE_BUNDLE_ID       비교 기준(운영) 번들. 없으면 contracts.BASELINE_BUNDLE
  REQUESTED_BY             검증을 요청한 사람·모듈. 없으면 "manual"
  VALIDATION_CALLBACK_URL  끝나면 결과를 보낼 B 주소. 없으면 보내지 않는다 (결과는 C 의 DB 에만)
  B_URL                    번들 구성(model_id, list_id)을 읽을 B 주소 (Job 양식에 있음). 없으면 contracts.BUNDLES
  BLOB_*                   업로드 원문 (common.blob)
종료 코드: 0 = completed 로 저장했고 B 가 받음(또는 보낼 곳 없음), 1 = 그 밖의 경우, 2 = 필수 환경변수 없음
  B 가 결과를 끝내 받지 못하면 1 로 끝나서 K8s Job 이 실패로 남는다 (B 가 알아챌 수 있게).

흐름: 같은 번들의 끝나지 않은 이전 검증 정리 -> 같은 ID 가 이미 끝났으면 그 결과를 다시 보냄
      -> 아니면 검증 행 생성(queued) -> 번들 확인 -> running -> 골든셋·Replay -> completed 또는 failed -> B 에 결과 전송
결과 전송: B 가 200 을 줄 때까지 간격을 늘려 가며 다시 보낸다 (B 의 연동 문서). 409·422 는 다시 보내도 같으므로 멈춘다.
시간 제한(activeDeadlineSeconds)에 걸리면 K8s 가 SIGTERM 을 보낸다. 검증 행을 만든 뒤라면 어느 지점에서든
failed 로 저장하고 짧게 전송을 시도한다 (이미 completed 면 결과를 바꾸지 않는다). 처음 신호만 처리하고 이후 신호는 무시한다.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from c_replay import store as S  # noqa: E402
from c_replay.replay import validate as V  # noqa: E402

# B 가 Job 에 넣는 환경변수 (B 의 b_registry/verify/replay.py)
JOB_ENV = C.REGISTRY_JOB_ENV
# 결과 전송. 요청 하나는 5초 제한, 대기는 1·2·4… 초에서 30초까지 늘린다.
NOTIFY_TIMEOUT = 5
NOTIFY_MAX_WAIT = 30
NOTIFY_BUDGET = 600         # 평소: 10분 동안 다시 보낸다 (Job 시간 제한 3600초 안)
NOTIFY_BUDGET_ON_TERM = 20  # SIGTERM 뒤: 종료 유예(job-template.json 의 30초) 안에 끝나야 한다
NOTIFY_STOP = (409, 422)    # 다시 보내도 결과가 같은 응답 (ID 불일치·이미 끝난 검증·형식 오류)


class Terminated(BaseException):
    """SIGTERM 을 받음. BaseException 이라 Replay 의 거래별 except Exception 에 잡히지 않고 run 까지 올라온다."""


_signaled = False


def _on_sigterm(signum, frame):
    """첫 신호만 Terminated 로 올린다. 이후 신호는 무시한다 (정리·전송이 끊기지 않게)."""
    global _signaled
    if _signaled:
        return
    _signaled = True
    raise Terminated()


def _error(code: str, message: str, target_id: str | None) -> dict:
    return dict(zip(C.ERROR_FIELDS, (code, message, target_id)))


def _http(method: str, url: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def callback_body(v: dict) -> dict:
    """B 의 결과 수신 API 본문 (PR #17 의 ValidationCallback). B 는 지표 전체로 게이트를 판정한다.
    error 는 B 가 문자열로 받으므로 메시지만 보낸다. 달라진 거래(diffs)는 B 가 쓰지 않아 보내지 않는다 (C 의 /diffs 로 조회)."""
    return {"validation_id": v["validation_id"], "bundle_id": v["bundle_id"],
            "baseline_bundle_id": v["baseline_bundle_id"], "status": v["status"],
            "golden": v["golden"], "replay": v["replay"],
            "error": v["error"]["message"] if v["error"] else None}


def notifier(url: str, sleep=time.sleep, clock=time.monotonic):
    """notify(검증, budget초) -> B 가 200 으로 받았으면 True. 예외를 올리지 않는다 (Terminated 는 그대로 올라간다)."""
    def notify(v: dict, budget: float = NOTIFY_BUDGET) -> bool:
        body, end, attempt = callback_body(v), clock() + budget, 0
        while True:
            attempt += 1
            try:
                _http("POST", url, body, NOTIFY_TIMEOUT)
                V.log(event="callback_sent", attempt=attempt, validation_id=v["validation_id"], status=v["status"])
                return True
            except urllib.error.HTTPError as e:
                V.log(event="callback_failed", attempt=attempt, http_status=e.code, validation_id=v["validation_id"])
                if e.code in NOTIFY_STOP:
                    return False
            except Exception as e:  # noqa: BLE001  연결 실패·시간 초과는 다시 보낸다
                V.log(event="callback_failed", attempt=attempt, error=f"{type(e).__name__}: {e}",
                      validation_id=v["validation_id"])
            wait = min(2 ** (attempt - 1), NOTIFY_MAX_WAIT)
            if clock() + wait + NOTIFY_TIMEOUT > end:
                return False
            sleep(wait)
    return notify


def no_notify(v: dict, budget: float = 0) -> bool:
    """보낼 곳이 없을 때 (손으로 돌릴 때). run 은 이 경우 저장만으로 성공으로 본다."""
    return False


def _execute(vid: str, bundle_id: str, baseline: str, store, load) -> bool:
    """running -> 골든셋·Replay -> completed. 예외면 failed. SIGTERM(Terminated)은 run 에서 처리한다."""
    store.start(vid)
    V.log(event="validation_started", validation_id=vid, bundle_id=bundle_id, baseline_bundle_id=baseline)
    try:
        golden, past, process = load()
        other = sum(p["bundle_id"] != baseline for p in past)
        V.log(event="past_bundle_mismatch", baseline_bundle_id=baseline, count=other, total=len(past))
        g = V.golden_result(golden, process, baseline, bundle_id)
        r, diffs = V.replay_result(past, process, bundle_id, log=V.log)
    except Exception as e:  # noqa: BLE001  실행 자체의 실패 -> failed (지표가 나쁜 것은 completed). Terminated 는 run 이 처리
        if isinstance(e, V.EmptyGolden):
            err = _error(C.ERR_NOT_FOUND, "골든셋이 비어 있음", vid)
        elif isinstance(e, S.StoreUnavailable) or type(e).__name__ == "OperationalError":   # psycopg 연결 실패
            err = _error(C.ERR_UNAVAILABLE, f"{type(e).__name__}: {e}", vid)
        else:
            err = _error(C.ERR_INTERNAL, f"{type(e).__name__}: {e}", vid)
        store.fail(vid, err)
        V.log(event="validation_failed", validation_id=vid, error=err)
        return False
    done = store.complete(vid, g, r, diffs)
    V.log(event="validation_completed" if done else "validation_discarded", validation_id=vid, golden=g, replay=r)
    return done


def _validate(vid: str, bundle_id: str, baseline: str, store, load, b_url: str | None = None) -> None:
    def fail(err):
        store.fail(vid, err)
        V.log(event="validation_failed", validation_id=vid, error=err)
    if b_url:   # 번들 구성은 B 가 원본 (A 도 B 와 같은 키 model_id·list_id 를 쓴다)
        try:
            V.fetch_bundles(b_url, (baseline, bundle_id))
        except urllib.error.HTTPError as e:
            target = e.url.rstrip("/").rsplit("/", 1)[-1] if e.url else bundle_id
            return fail(_error(C.ERR_NOT_FOUND if e.code == 404 else C.ERR_UNAVAILABLE,
                               f"B 에서 번들을 읽지 못함 (HTTP {e.code})", urllib.parse.unquote(target)))
        except Exception as e:  # noqa: BLE001  연결 실패
            return fail(_error(C.ERR_UNAVAILABLE, f"B 에 연결할 수 없음: {type(e).__name__}: {e}", bundle_id))
    for bid in (baseline, bundle_id):
        try:
            V.bundle(bid)
        except LookupError:
            return fail(_error(C.ERR_NOT_FOUND, f"번들 {bid} 를 찾을 수 없음", bid))
    _execute(vid, bundle_id, baseline, store, load)


def run(job: dict, store, load, notify=no_notify) -> bool:
    """검증 한 번. job = {validation_id, bundle_id, baseline_bundle_id, requested_by, b_url(없어도 됨)}.
    load() -> (골든셋, 과거 거래, process), notify(검증, budget) -> B 가 받았으면 True.
    반환: completed 로 저장했고 B 가 받았으면 True (notify 가 no_notify 면 저장만으로 True)."""
    vid, bundle_id, baseline = job["validation_id"], job["bundle_id"], job["baseline_bundle_id"]
    # B 는 번들마다 검증 Job 을 하나만 돌린다. 새 Job 이 시작됐다면 같은 번들의 끝나지 않은 검증은 죽은 실행이다.
    n = store.fail_stale(bundle_id, timedelta(0), _error(
        C.ERR_INTERNAL, "중단된 검증을 정리함 (새 검증 Job 이 시작될 때까지 끝나지 않았음)", bundle_id))
    if n:
        V.log(event="stale_validations_failed", bundle_id=bundle_id, count=n)
    try:
        if store.get(vid) is None:
            try:
                store.create(bundle_id, baseline, job["requested_by"], validation_id=vid)
            except S.InProgressExists:
                V.log(event="validation_in_progress", bundle_id=bundle_id)   # 정리 직후 다른 실행이 끼어든 경우
                return False
            _validate(vid, bundle_id, baseline, store, load, job.get("b_url"))
        else:   # 같은 ID 가 이미 끝났다 (Job 이 다시 실행됨): 다시 계산하지 않고 그 결과를 다시 보낸다
            V.log(event="validation_already_finished", validation_id=vid, status=store.get(vid)["status"])
        budget = NOTIFY_BUDGET
    except Terminated:
        # 검증 행이 있으면 failed 로 (이미 completed 면 store.fail 이 아무것도 하지 않는다). 이후 신호는 무시된다.
        if store.get(vid) is None:
            V.log(event="terminated_before_validation_created", bundle_id=bundle_id)
            return False
        err = _error(C.ERR_INTERNAL, "중단됨 (SIGTERM: 시간 제한 초과 또는 파드 종료)", vid)
        if store.fail(vid, err):
            V.log(event="validation_failed", validation_id=vid, error=err)
        budget = NOTIFY_BUDGET_ON_TERM
    v = store.get(vid)
    try:
        sent = notify(v, budget)
    except Terminated:   # 전송 중 신호: 종료 유예 안에서 한 번 더 시도한다 (이후 신호는 무시된다)
        sent = notify(v, NOTIFY_BUDGET_ON_TERM)
    return v["status"] == S.COMPLETED and (sent or notify is no_notify)


def main(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    bundle_id, dsn = env.get("BUNDLE_ID"), env.get("DATABASE_URL")
    if not bundle_id or not dsn:
        print("환경변수 BUNDLE_ID 와 DATABASE_URL 이 필요함", file=sys.stderr)
        return 2
    job = {"validation_id": env.get("VALIDATION_ID") or S.new_validation_id(), "bundle_id": bundle_id,
           "baseline_bundle_id": env.get("BASELINE_BUNDLE_ID") or C.BASELINE_BUNDLE,
           "requested_by": env.get("REQUESTED_BY") or "manual", "b_url": env.get("B_URL")}
    url = env.get("VALIDATION_CALLBACK_URL")
    global _signaled
    _signaled = False
    signal.signal(signal.SIGTERM, _on_sigterm)
    from common.blob import make_blob_store
    with tempfile.TemporaryDirectory(prefix="c-validation-") as tmp:
        ok = run(job, S.make_store(dsn),
                 lambda: (V.read_golden(dsn), V.read_past(dsn), V.ocr_process(make_blob_store(), Path(tmp))),
                 notifier(url) if url else no_notify)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
