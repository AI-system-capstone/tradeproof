"""검증 Job 진입점: 골든셋 검증(VAL-01) + Replay(VAL-02·03) 한 번.

B 가 반입 검증을 통과한 번들(validated)마다 C 의 Job 템플릿(c_replay/job/job-template.json)에
BUNDLE_ID 를 넣어 K8s Job 을 만든다. 같은 명령을 일반 프로세스로도 실행할 수 있다 (compose·로컬).

  BUNDLE_ID=B-9 DATABASE_URL=... B_URL=http://b-registry:8100 python -m c_replay.job

환경변수
  BUNDLE_ID     검증할 후보 번들 (필수, B 가 채운다)
  DATABASE_URL  C 의 validations + A 의 거래·판정·golden_labels (필수)
  B_URL         B 주소. 운영 번들 조회(GET /bundles/active)와 완료 알림에 쓴다.
                없으면 운영 번들 = contracts.BASELINE_BUNDLE 이고 완료 알림을 보내지 않는다 (B 없이 돌릴 때)
  BLOB_*        업로드 원문 (common.blob)
종료 코드: 0 = completed, 1 = failed 또는 시작 못 함, 2 = 필수 환경변수 없음

흐름: 운영 번들 확인 -> 오래된 진행 중 검증 정리 -> 검증 행 생성(queued) -> 번들 확인 -> running -> 골든셋·Replay
      -> completed 또는 failed -> B 에 완료 알림
시간 제한(activeDeadlineSeconds)에 걸리면 K8s 가 SIGTERM 을 보낸다. 검증 행을 만든 뒤라면 어느 지점에서든
failed 로 저장하고 알림을 보낸다 (이미 completed 면 결과를 바꾸지 않는다). 처음 신호만 처리하고 이후 신호는 무시한다.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from c_replay import store as S  # noqa: E402
from c_replay.replay import validate as V  # noqa: E402

REQUESTED_BY = "b_registry"   # 검증 Job 은 B 가 만든다
# 완료 알림 전체 시간 = 요청 제한 5초 x 3번 + 대기 1초·2초 = 최대 18초. Job 양식의 종료 유예(30초) 안에 들어와야 한다.
NOTIFY_TRIES = 3
NOTIFY_TIMEOUT = 5
# 진행 중으로 남은 검증을 죽은 것으로 보는 기준. Job 시간 제한(job-template.json 의 activeDeadlineSeconds 3600초)
# 보다 길어야 한다. 아직 돌고 있는 Job 의 검증을 끊으면 안 되기 때문이다.
STALE_AFTER = timedelta(seconds=3600 + 600)


class Terminated(BaseException):
    """SIGTERM 을 받음. BaseException 이라 Replay 의 거래별 except Exception 에 잡히지 않고 run 까지 올라온다."""


_signaled = False


def _on_sigterm(signum, frame):
    """첫 신호만 Terminated 로 올린다. 이후 신호는 무시한다 (정리·알림이 끊기지 않게)."""
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


def active_bundle(b_url: str | None) -> str:
    """운영(active) 번들 ID. B 없이 돌릴 때는 contracts.BASELINE_BUNDLE."""
    if not b_url:
        return C.BASELINE_BUNDLE
    return _http("GET", f"{b_url.rstrip('/')}/bundles/active")["bundle_id"]


def notifier(b_url: str | None, sleep=time.sleep):
    """완료 알림을 보내는 함수를 만든다. 실패해도 예외를 올리지 않는다
    (결과는 C 의 DB 에 있으니 B 는 GET /validations?bundle_id= 로 확인할 수 있다).
    가정: B 가 만들 게이트 판정 요청 API 는 POST /bundles/{bundle_id}/validation-results {validation_id, bundle_id, status}."""
    def notify(v: dict) -> bool:
        if not b_url:
            return False
        body = {"validation_id": v["validation_id"], "bundle_id": v["bundle_id"], "status": v["status"]}
        url = f"{b_url.rstrip('/')}/bundles/{urllib.parse.quote(v['bundle_id'], safe='')}/validation-results"
        for attempt in range(1, NOTIFY_TRIES + 1):
            try:
                _http("POST", url, body, NOTIFY_TIMEOUT)
                V.log(event="notify_sent", **body)
                return True
            except Exception as e:  # noqa: BLE001
                V.log(event="notify_failed", attempt=attempt, error=f"{type(e).__name__}: {e}", **body)
                if attempt < NOTIFY_TRIES:
                    sleep(2 ** (attempt - 1))
        return False
    return notify


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


def run(bundle_id: str, get_baseline, store, load, notify=lambda v: False) -> bool:
    """검증 한 번. 반환: completed 로 저장했으면 True.
    get_baseline() -> 운영 번들 ID, load() -> (골든셋, 과거 거래, process), notify(검증) -> 완료 알림."""
    try:
        baseline, err = get_baseline(), None
    except Exception as e:  # noqa: BLE001  B 에 못 닿아도 검증 행은 남겨서 실패가 보이게 한다
        baseline = ""
        err = _error(C.ERR_UNAVAILABLE, f"B 에서 운영 번들을 가져올 수 없음: {type(e).__name__}: {e}", bundle_id)
    n = store.fail_stale(bundle_id, STALE_AFTER, _error(
        C.ERR_INTERNAL, "중단된 검증을 정리함 (Job 이 끝났다는 기록 없이 시간 제한보다 오래 진행 중이었음)", bundle_id))
    if n:
        V.log(event="stale_validations_failed", bundle_id=bundle_id, count=n)
    vid = None
    try:
        vid = store.create(bundle_id, baseline, REQUESTED_BY)["validation_id"]
        _validate(vid, bundle_id, baseline, err, store, load)
    except S.InProgressExists:
        V.log(event="validation_in_progress", bundle_id=bundle_id)   # 같은 번들 검증이 이미 진행 중
        return False
    except Terminated:
        # 검증 행이 있으면 failed 로 (이미 completed 면 store.fail 이 아무것도 하지 않는다). 이후 신호는 무시된다.
        if vid is None:
            V.log(event="terminated_before_validation_created", bundle_id=bundle_id)
            return False
        err = _error(C.ERR_INTERNAL, "중단됨 (SIGTERM: 시간 제한 초과 또는 파드 종료)", vid)
        if store.fail(vid, err):
            V.log(event="validation_failed", validation_id=vid, error=err)
    try:
        notify(store.get(vid))
    except Terminated:   # 알림 중 신호: 한 번 더 보낸다 (이후 신호는 무시, 알림 시간 상한은 NOTIFY_* 로 유예 안에 들어온다)
        notify(store.get(vid))
    return store.get(vid)["status"] == S.COMPLETED


def _validate(vid, bundle_id, baseline, err, store, load) -> None:
    for bid in (() if err else (baseline, bundle_id)):
        try:
            V.bundle(bid)
        except LookupError:
            err = _error(C.ERR_NOT_FOUND, f"번들 {bid} 를 찾을 수 없음", bid)
            break
    if err:
        store.fail(vid, err)
        V.log(event="validation_failed", validation_id=vid, error=err)
    else:
        _execute(vid, bundle_id, baseline, store, load)


def main(env: dict | None = None) -> int:
    env = os.environ if env is None else env
    bundle_id, dsn, b_url = env.get("BUNDLE_ID"), env.get("DATABASE_URL"), env.get("B_URL")
    if not bundle_id or not dsn:
        print("환경변수 BUNDLE_ID 와 DATABASE_URL 이 필요함", file=sys.stderr)
        return 2
    global _signaled
    _signaled = False
    signal.signal(signal.SIGTERM, _on_sigterm)
    from common.blob import make_blob_store
    with tempfile.TemporaryDirectory(prefix="c-validation-") as tmp:
        ok = run(bundle_id, lambda: active_bundle(b_url), S.make_store(dsn),
                 lambda: (V.read_golden(dsn), V.read_past(dsn), V.ocr_process(make_blob_store(), Path(tmp))),
                 notifier(b_url))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
