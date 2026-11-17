"""번들 구성 가져오기: 번들 ID → {bundle_id, model_id, list_id} + 모델 설정·제재 리스트.

두 가지 방식 (REGISTRY_MODE, D 의 d_platform/api/registry.py 와 같은 이름):
  local (기본)  common/contracts.BUNDLES 표 + 로컬 data/ (테스트·compose·B 없이 개발할 때)
  http          B 레지스트리 GET {REGISTRY_URL}/bundles/{id} (b_registry/README.md, 11/17 main 기준)
                  -> {bundle_id, status, model_id, list_id, model:{model_id, uri}, list:{list_id, uri}, ...}
                uri 는 B 가 반입 검증을 마친 원본 ZIP (s3://tradeproof-registry/registry/<sha256>).
                  모델 ZIP  payload/model_config.json = {engine, lang, config, preprocess}  (ocr/extract.py MODELS 한 줄과 같은 모양)
                  리스트 ZIP payload/sanctions.json  = {list_version, source, ..., entries}  (sanctions/build_lists.py 출력과 같은 모양)

번들 구성(어떤 모델·리스트인지)은 한 번 만들어지면 바뀌지 않으므로 번들 ID 별로 한 번만 조회해 캐시한다.
ZIP 은 내용 주소(sha256)로 저장돼 있어서 받은 바이트의 sha256 이 uri 끝과 같은지 확인한다 (B 가 서명은 이미 확인함).
OCR 패키지(pytesseract)를 import 하지 않는다 → 심사 API 이미지(OCR 없음)에서도 쓸 수 있다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
import zipfile

from common import contracts as C

MODEL_FILE = "payload/model_config.json"
LIST_FILE = "payload/sanctions.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RegistryUnavailable(Exception):
    """B 또는 원본 저장소에 연결할 수 없음 → 나중에 다시 시도하면 될 수 있다 (워커는 재시도, /healthz 는 503)."""


class BundleNotFound(ValueError):
    """없는 번들 ID (B 404 또는 contracts.BUNDLES 에 없음) → 다시 시도해도 같다."""


class BadArtifact(ValueError):
    """ZIP 이 손상됐거나 약속한 파일이 없음 → 다시 시도해도 같다."""


def mode() -> str:
    return os.environ.get("REGISTRY_MODE", "local")


def is_http() -> bool:
    return mode() == "http"


# ---------------------------------------------------------------- 캐시 (프로세스당)
_LOCK = threading.Lock()
_BUNDLES: dict[str, dict] = {}     # 번들 ID -> {bundle_id, model_id, list_id}
_URIS: dict[tuple[str, str], str] = {}   # ("model"|"list", id) -> ZIP uri
_MODELS: dict[str, dict] = {}      # model_id -> OCR 설정
_LISTS: dict[str, dict] = {}       # list_id -> 리스트 JSON
_blob = None                       # 원본 저장소 (테스트에서 set_blob_store 로 바꾼다)


def reset() -> None:
    """캐시 비우기 (테스트용)."""
    global _blob
    with _LOCK:
        for d in (_BUNDLES, _URIS, _MODELS, _LISTS, _INDEXES):
            d.clear()
        _blob = None


def set_blob_store(store) -> None:
    global _blob
    _blob = store


def _blob_store():
    global _blob
    if _blob is None:
        from common.blob import make_blob_store   # minio 패키지는 http 모드에서만 필요
        _blob = make_blob_store()
    return _blob


# ---------------------------------------------------------------- B API
def _get_json(path: str) -> dict:
    base = os.environ.get("REGISTRY_URL")
    if not base:
        raise RegistryUnavailable("REGISTRY_MODE=http 인데 REGISTRY_URL 이 없음")
    url = base.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=float(os.environ.get("REGISTRY_TIMEOUT", "5"))) as resp:  # noqa: S310  내부 주소
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BundleNotFound(f"B 에 없는 번들: {path}") from e
        raise RegistryUnavailable(f"B 응답 {e.code}: {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise RegistryUnavailable(f"B 연결 실패: {url} ({e})") from e


def _from_registry(bundle_id: str) -> dict:
    d = _get_json(f"/bundles/{urllib.request.quote(bundle_id, safe='')}")
    try:
        model_id, list_id = d["model_id"], d["list_id"]
        model_uri, list_uri = d["model"]["uri"], d["list"]["uri"]
    except (KeyError, TypeError) as e:
        raise RegistryUnavailable(f"B 번들 응답 형식이 다름: {sorted(d) if isinstance(d, dict) else d!r}") from e
    if d.get("bundle_id", bundle_id) != bundle_id or not model_id or not list_id:
        raise RegistryUnavailable(f"B 번들 응답이 요청과 맞지 않음: {d}")
    _URIS[("model", model_id)] = model_uri
    _URIS[("list", list_id)] = list_uri
    return {"bundle_id": bundle_id, "model_id": model_id, "list_id": list_id}


def bundle(bundle_id: str) -> dict:
    """번들 ID → {bundle_id, model_id, list_id}. 없는 번들이면 BundleNotFound."""
    with _LOCK:
        if bundle_id in _BUNDLES:
            return dict(_BUNDLES[bundle_id])
    if is_http():
        b = _from_registry(bundle_id)
    else:
        if bundle_id not in C.BUNDLES:
            raise BundleNotFound(f"bundle_id={bundle_id!r} 는 contracts.BUNDLES 에 없음 ({list(C.BUNDLES)})")
        src = C.BUNDLES[bundle_id]
        b = {"bundle_id": bundle_id, "model_id": src["model_id"], "list_id": src["list_id"]}
    with _LOCK:
        _BUNDLES[bundle_id] = b
    return dict(b)


def known_bundles() -> dict[str, dict]:
    """화면용 번들 표: local 이면 contracts.BUNDLES 전부, http 면 지금까지 조회한 번들."""
    if not is_http():
        return {bid: dict(b) for bid, b in C.BUNDLES.items()}
    with _LOCK:
        return {bid: dict(b) for bid, b in _BUNDLES.items()}


# ---------------------------------------------------------------- ZIP 안 파일
def _artifact_file(kind: str, item_id: str, name: str) -> bytes:
    uri = _URIS.get((kind, item_id))
    if uri is None:
        raise RegistryUnavailable(f"{kind} {item_id!r} 의 ZIP 위치를 모름 (그 번들을 먼저 조회해야 함)")
    try:
        raw = _blob_store().get(uri)
    except Exception as e:  # noqa: BLE001
        raise RegistryUnavailable(f"원본 저장소에서 {uri} 를 못 받음 ({e})") from e
    tail = uri.rsplit("/", 1)[-1]
    if _SHA256.match(tail) and hashlib.sha256(raw).hexdigest() != tail:
        raise BadArtifact(f"{uri}: 받은 파일의 sha256 이 다름 (손상 또는 변조)")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return z.read(name)
    except (zipfile.BadZipFile, KeyError) as e:
        raise BadArtifact(f"{uri}: ZIP 에 {name} 없음 ({e})") from e


def model_config(model_id: str) -> dict | None:
    """http 모드: B 모델 ZIP 의 OCR 설정. local 모드: None (ocr/extract.py 의 MODELS 를 그대로 쓴다)."""
    if not is_http():
        return None
    with _LOCK:
        if model_id in _MODELS:
            return _MODELS[model_id]
    spec = json.loads(_artifact_file("model", model_id, MODEL_FILE))
    if not isinstance(spec, dict) or not {"engine", "lang", "config"} <= set(spec):
        raise BadArtifact(f"{model_id}: {MODEL_FILE} 형식이 다름")
    with _LOCK:
        _MODELS[model_id] = spec
    return spec


def sanctions_list(list_id: str) -> dict | None:
    """http 모드: B 리스트 ZIP 의 리스트 JSON. local 모드: None (data/sanctions/<list_id>.json 을 그대로 쓴다)."""
    if not is_http():
        return None
    with _LOCK:
        if list_id in _LISTS:
            return _LISTS[list_id]
    data = json.loads(_artifact_file("list", list_id, LIST_FILE))
    if not isinstance(data, dict) or data.get("list_version") != list_id or not isinstance(data.get("entries"), list):
        raise BadArtifact(f"{list_id}: {LIST_FILE} 형식이 다르거나 버전이 맞지 않음")
    with _LOCK:
        _LISTS[list_id] = data
    return data


# ---------------------------------------------------------------- 제재 리스트 색인 (API·워커·C 가 같이 씀)
_INDEXES: dict = {}


def list_index(list_id: str):
    """리스트 ID → screening.match.ListIndex (리스트당 한 번 만들고 캐시). http 면 B ZIP, local 이면 data/sanctions/."""
    from a_screening.screening.match import ListIndex, load_index
    with _LOCK:
        if list_id in _INDEXES:
            return _INDEXES[list_id]
    data = sanctions_list(list_id)
    index = ListIndex(data) if data is not None else load_index(list_id)
    with _LOCK:
        return _INDEXES.setdefault(list_id, index)
