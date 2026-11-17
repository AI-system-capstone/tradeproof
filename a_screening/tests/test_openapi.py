"""API 명세서(docs/openapi/a_screening.json)가 코드·실제 응답과 같은지 확인한다.

코드를 바꾸고 명세를 안 고치면 실패한다 → python -m a_screening.api.export_openapi 로 다시 만든다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from common.blob import LocalBlobStore  # noqa: E402
from a_screening.store import MemoryStore  # noqa: E402
import a_screening.api.main as A  # noqa: E402
from a_screening.api import export_openapi as E, schemas as S  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def memory(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "STORE", MemoryStore())
    monkeypatch.setattr(A, "BLOB", LocalBlobStore(tmp_path / "blob"))


def test_spec_file_is_up_to_date():
    assert E.OUT.is_file(), "docs/openapi/a_screening.json 없음 → python -m a_screening.api.export_openapi"
    assert E.OUT.read_text(encoding="utf-8") == E.render(), \
        "명세 파일이 코드와 다름 → python -m a_screening.api.export_openapi"


def test_422_uses_common_error_format():
    spec = A.app.openapi()
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            r = op["responses"].get("422")
            if r:
                assert r["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}, (method, path)
    assert "HTTPValidationError" not in spec["components"]["schemas"]


@pytest.mark.parametrize("model, example", [
    (S.Accepted, S.EX_ACCEPTED), (S.ScreeningDetail, S.EX_DETAIL), (S.ReviewResult, S.EX_REVIEW),
    (S.ListResult, S.EX_LIST), (S.ListItem, S.EX_LIST["items"][0]), (S.Health, S.EX_HEALTH),
    *[(S.Error, e) for e in S._ERR_EX.values()],
])
def test_examples_match_models(model, example):
    assert set(example) == set(model.model_fields)
    model.model_validate(example)


def test_real_responses_match_models():
    """실제 응답 키 == 명세 모델 키 (응답에 키를 더하거나 빼면 명세도 고쳐야 함)."""
    fields = {f: "SEA STAR TRADING" for f in C.SCREENING_FIELDS}
    r = client.post("/screenings", json={"fields": fields})
    assert r.status_code == 202 and set(r.json()) == set(S.Accepted.model_fields)
    tx = r.json()["tx_id"]
    d = client.get(f"/screenings/{tx}").json()
    assert set(d) == set(S.ScreeningDetail.model_fields)
    S.ScreeningDetail.model_validate(d)
    lst = client.get("/screenings").json()
    assert set(lst) == set(S.ListResult.model_fields) and set(lst["items"][0]) == set(S.ListItem.model_fields)
    h = client.get("/healthz").json()
    assert set(h) == set(S.Health.model_fields) and h["rules_version"] == C.RULES_FIXED
    e = client.get("/screenings/TX-NOPE").json()
    assert set(e) == set(S.Error.model_fields)
