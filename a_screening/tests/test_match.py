"""screening/match.py 테스트 (rules-v2 동작 고정 + rules-v3 규칙).

  .venv/bin/python -m pytest screening/test_match.py -q
  TRADEPROOF_V2_SNAPSHOT=/path/v2_snapshot.json ...   # rules-v2 출력 전체를 파일과 바이트 단위로 비교

데이터(data/docs, data/ocr, data/sanctions)가 없으면 데이터 기반 테스트는 건너뛴다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from a_screening.screening import match as M  # noqa: E402
from a_screening.screening.match import ListIndex, load_index, normalize, ocr_key, screen  # noqa: E402

_SEV = {C.PASS: 0, C.REVIEW: 1, C.HOLD: 2}

# rules-v2 스냅샷 (11/08, rules-v3 작업 전 match.py 로 생성). 입력이 같을 때만 비교한다
#   입력 = list-v14/v15 항목 + truth fields + data/ocr, data/ocr/model-v4 추출 값 (Docker tesseract 5.5.0 산출물)
V2_INPUT_SHA256 = "a27e7fb229c2cc7f7581a5c357420f9229cd70a9e6638739647af1da6e615d95"
V2_OUTPUT_SHA256 = "d9b83778a777a35575cdb432b3465d24eab4cdffe9149b0c0c47500204a06ea3"

SAMPLE_LIST = {
    "list_version": "list-test",
    "entries": [
        {"entry_id": "T-1", "name": "ASTERIA HARBOR TRADING LLC", "aliases": ["ASTERIA HARBOUR TRADING"],
         "entity_type": C.ENTITY},
        {"entry_id": "T-2", "name": "LIMITED LIABILITY COMPANY MANAGEMENT COMPANY ELGA", "aliases": ["UK ELSI OOO"],
         "entity_type": C.ENTITY},
        {"entry_id": "T-3", "name": "JOINT STOCK COMPANY RUDNIK KARALVEEM", "aliases": ["OJSC KARALVEEM MINE"],
         "entity_type": C.ENTITY},
        {"entry_id": "T-4", "name": "G JADES", "aliases": [], "entity_type": C.VESSEL},
        {"entry_id": "T-5", "name": "OCEAN BLOSSOM", "aliases": [], "entity_type": C.VESSEL},
    ],
}
CLEAN = {"seller_name": "Valerian Labs Distribution", "buyer_name": "Fuji Coral Electronics LLC",
         "vessel_name": "EMERALD TRIUMPH"}


@pytest.fixture(scope="module")
def idx() -> ListIndex:
    return ListIndex(SAMPLE_LIST)


def _rules(res: dict) -> list[str]:
    return [r["rule"] for r in res["reasons"]]


def _ocr(conf: dict | None = None, conflicts: list | None = None) -> dict:
    base = {f: 95.0 for f in C.PARTY_FIELDS + C.VESSEL_FIELDS}
    base.update(conf or {})
    return {"confidence": base, "conflicts": conflicts or []}


# ---------------------------------------------------------------- 공통

def test_bad_rules_version(idx):
    with pytest.raises(ValueError):
        screen(CLEAN, idx, "rules-v9")


def test_rules_version_in_result(idx):
    assert screen(CLEAN, idx)["rules_version"] == C.RULES_BASE
    assert screen(CLEAN, idx, C.RULES_NEW)["rules_version"] == C.RULES_NEW
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr())["classification"] == C.PASS


def test_rules_version_not_from_module_state(idx, monkeypatch):
    """screen 기본 인자·v2 반환값은 C.RULES_BASE 고정 (match.RULES_VERSION 전역을 읽지 않음)."""
    monkeypatch.setattr(M, "RULES_VERSION", C.RULES_NEW)
    res = screen(dict(CLEAN, seller_name="Asteria Harbor Trading Lic"), idx)
    assert res["rules_version"] == C.RULES_BASE
    assert not set(_rules(res)) & set(C.RULES_V3_EXTRA)
    assert screen(CLEAN, idx, C.RULES_BASE)["rules_version"] == C.RULES_BASE


def test_pure_no_mutation(idx):
    fields = dict(CLEAN, seller_name="Asteria Harbor Trading Lic")
    ocr = _ocr({"buyer_name": 50.0}, [{"field": "vessel_name", "invoice": "G J ADES", "bl": "OCEAN"}])
    f0, o0 = copy.deepcopy(fields), copy.deepcopy(ocr)
    a = screen(fields, idx, C.RULES_NEW, ocr)
    b = screen(fields, idx, C.RULES_NEW, ocr)
    assert fields == f0 and ocr == o0 and a == b


def test_normalize_unchanged():
    assert normalize("Asteria Harbor Trading L.L.C.") == "ASTERIA HARBOR TRADING"
    assert normalize("MV Ocean Blossom", vessel=True) == "OCEAN BLOSSOM"
    assert normalize("Compañia & Co") == "COMPANIA AND"


# ---------------------------------------------------------------- rules-v3: 혼동 글자 감안 정확 일치

@pytest.mark.parametrize("field,value,entry,rule", [
    ("seller_name", "Asteria Harbor Trading Lic", "T-1", C.RULE_PARTY_EXACT_OCR),   # Llc -> Lic
    ("seller_name", "Asteria Harbor Trading LIc", "T-1", C.RULE_PARTY_EXACT_OCR),
    ("buyer_name", "UK ELS! OOO INC", "T-2", C.RULE_PARTY_EXACT_OCR),               # I -> !
    ("buyer_name", "UK ELS| OOO INC", "T-2", C.RULE_PARTY_EXACT_OCR),
    ("seller_name", "OJ SC KARALVEEM MINE", "T-3", C.RULE_PARTY_EXACT_OCR),         # 단어가 쪼개짐
    ("vessel_name", "G J ADES", "T-4", C.RULE_VESSEL_EXACT_OCR),
    ("vessel_name", "lV OCEAN BLOSSOM", "T-5", C.RULE_VESSEL_EXACT_OCR),            # MV -> lV
])
def test_exact_ocr_hold(idx, field, value, entry, rule):
    fields = dict(CLEAN, **{field: value})
    v2 = screen(fields, idx)
    v3 = screen(fields, idx, C.RULES_NEW, _ocr())
    assert v2["classification"] != C.HOLD          # v2 로는 못 잡던 경우
    assert v3["classification"] == C.HOLD
    r = next(r for r in v3["reasons"] if r["rule"] == rule)
    assert r["value"] == value and r["entry_id"] == entry and r["field"] == field   # 값은 원래 그대로
    assert r["matched_name"] and r["classification"] == C.HOLD
    assert M.FUZZY_THRESHOLD <= r["score"] <= 1.0   # score = 접기 전 v2 유사도


@pytest.mark.parametrize("field,value,entry,rule", [
    ("buyer_name", "UK ELSI 0OO INC", "T-2", C.RULE_PARTY_EXACT_OCR),    # 읽힌 값에 숫자 (0 -> O 로 접혀서 일치)
    ("vessel_name", "MV 0CEAN BL0SSOM", "T-5", C.RULE_VESSEL_EXACT_OCR),  # 숫자 + 접기 전 유사도 0.846 < 0.85
])
def test_exact_ocr_weak_is_review(idx, field, value, entry, rule):
    """접힌 정확 일치라도 HOLD 조건(유사도·길이·숫자 없음)을 못 채우면 같은 규칙 이름으로 REVIEW."""
    v3 = screen(dict(CLEAN, **{field: value}), idx, C.RULES_NEW, _ocr())
    r = next(r for r in v3["reasons"] if r["rule"] == rule)
    assert r["classification"] == C.REVIEW and r["entry_id"] == entry and r["value"] == value
    assert v3["classification"] == C.REVIEW


def test_exact_ocr_only_with_ocr(idx):
    """ocr=None(직접 입력한 필드)이면 관대한 비교를 하지 않는다 -> v3 근거 = v2 근거."""
    for value in ("Asteria Harbor Trading Lic", "UK ELS| OOO INC"):
        fields = dict(CLEAN, seller_name=value)
        v2, v3 = screen(fields, idx), screen(fields, idx, C.RULES_NEW)
        assert v3["reasons"] == v2["reasons"] and v3["classification"] == v2["classification"]


def test_exact_ocr_not_duplicated_when_v2_exact(idx):
    v3 = screen(dict(CLEAN, seller_name="ASTERIA HARBOR TRADING LLC"), idx, C.RULES_NEW, _ocr())
    assert _rules(v3) == [C.RULE_PARTY_EXACT]


def test_exact_ocr_no_false_hold(idx):
    for name in ("Asteria Harbor Logistics Lic", "UK ELSA OOO", "OCEAN BLOSSOMS", "Fuji Coral Electronics"):
        fields = dict(CLEAN, seller_name=name, vessel_name=name)
        assert C.RULE_PARTY_EXACT_OCR not in _rules(screen(fields, idx, C.RULES_NEW, _ocr()))
    assert ocr_key("Asteria Harbor Trading Lic") == ocr_key("ASTERIA HARBOR TRADING LLC")


# 독립 검증에서 나온 오탐 (list-v15 의 실제 항목 이름을 그대로 옮김): 정상 이름이 접기 때문에 HOLD 가 되면 안 됨
FP_LIST = {
    "list_version": "list-fp",
    "entries": [
        {"entry_id": "F-1", "name": "AIX COMPANY LIMITED", "aliases": [], "entity_type": C.ENTITY},
        {"entry_id": "F-2", "name": "IDEK", "aliases": [], "entity_type": C.ENTITY},
        {"entry_id": "F-3", "name": "HUJI-B", "aliases": [], "entity_type": C.ENTITY},
        {"entry_id": "F-4", "name": "PLM CONSORCIO, C.A.", "aliases": [], "entity_type": C.ENTITY},
        {"entry_id": "F-5", "name": "FLORA", "aliases": [], "entity_type": C.VESSEL},
        {"entry_id": "F-6", "name": "SABLE", "aliases": [], "entity_type": C.VESSEL},
        {"entry_id": "F-7", "name": "LUNA", "aliases": [], "entity_type": C.VESSEL},
        {"entry_id": "F-8", "name": "SIERRA", "aliases": [], "entity_type": C.VESSEL},
    ],
}
FP_PARTIES = ("ALX Ltd", "A1X Inc", "Ldek", "Huji 8", "PIRN CONSORCIO CA")
FP_VESSELS = ("FIORA", "SABIE", "IUNA", "IT SIERRA")


# list-v15 에 없는 이름 (CLEAN 의 'Valerian Labs Distribution' 은 실제 SDN 항목이라 list-v15 로는 HOLD)
FP_BASE = {"seller_name": "Fuji Coral Electronics LLC", "buyer_name": "Hanbit Trading Co",
           "vessel_name": "EMERALD TRIUMPH"}


def _fp_cases():
    for n in FP_PARTIES:
        yield dict(FP_BASE, seller_name=n)
    for n in FP_VESSELS:
        yield dict(FP_BASE, vessel_name=n)


@pytest.mark.parametrize("ocr", [None, {}, "full"])
def test_exact_ocr_known_false_positives_not_hold(ocr):
    index = ListIndex(FP_LIST)
    for fields in _fp_cases():
        o = _ocr() if ocr == "full" else ocr
        res = screen(fields, index, C.RULES_NEW, o)
        assert res["classification"] != C.HOLD, (fields, res["reasons"])


def test_vessel_prefix_it_not_stripped():
    """'IT' 접두어는 근거가 없어 떼지 않는다 ('MV'->'lV' 의 'IV' 만 뗌)."""
    assert ocr_key("IT SIERRA", vessel=True) == "ITSIERRA"
    assert ocr_key("lV SIERRA", vessel=True) == "SIERRA"


# ---------------------------------------------------------------- rules-v3: OCR 불확실성 -> 검토

def test_low_confidence(idx):
    res = screen(CLEAN, idx, C.RULES_NEW, _ocr({"buyer_name": 60.5}))
    assert res["classification"] == C.REVIEW
    r = res["reasons"][0]
    assert r["rule"] == C.RULE_LOW_CONFIDENCE and r["field"] == "buyer_name" and r["score"] == 0.605   # 신뢰도/100
    assert r["value"] == CLEAN["buyer_name"] and r["entry_id"] is None and r["matched_name"] is None
    # 경계값·스크리닝 외 필드는 검토 아님
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr({"buyer_name": C.OCR_CONFIDENCE_MIN}))["classification"] == C.PASS
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr({"amount": 10.0}))["classification"] == C.PASS
    # ocr 없으면 적용 안 함
    assert screen(CLEAN, idx, C.RULES_NEW)["classification"] == C.PASS


@pytest.mark.parametrize("cf", [None, "95", float("nan"), float("inf"), True, -1.0, 150.0, [95.0]])
def test_low_confidence_missing_or_bad(idx, cf):
    """값이 있는데 신뢰도가 없음/숫자 아님/NaN/범위 밖 -> low_confidence(score=None). 형식 이상이면 missing_field 도."""
    res = screen(CLEAN, idx, C.RULES_NEW, _ocr({"vessel_name": cf}))
    assert res["classification"] == C.REVIEW
    lc = [r for r in res["reasons"] if r["rule"] == C.RULE_LOW_CONFIDENCE]
    assert [(r["field"], r["score"], r["value"]) for r in lc] == [("vessel_name", None, CLEAN["vessel_name"])]
    mf = [r for r in res["reasons"] if r["rule"] == C.RULE_MISSING_FIELD]
    if cf is None:
        assert mf == []
    else:
        assert len(mf) == 1 and mf[0]["field"] == "vessel_name" and "confidence" in mf[0]["value"]
    # 신뢰도 키가 아예 없어도 low_confidence
    res2 = screen(CLEAN, idx, C.RULES_NEW, {"confidence": {}, "conflicts": []})
    assert sorted(r["field"] for r in res2["reasons"] if r["rule"] == C.RULE_LOW_CONFIDENCE) == sorted(C.SCREENING_FIELDS)


def test_doc_conflict(idx):
    c = [{"field": "buyer_name", "invoice": "Fuji Coral Electronics LLC", "bl": "Fuji Coral Industries LLC"}]
    res = screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c))
    assert res["classification"] == C.REVIEW and _rules(res) == [C.RULE_DOC_CONFLICT]
    # 스크리닝 필드가 아니면 무시
    c2 = [{"field": "goods_description", "invoice": "a", "bl": "b"}]
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c2))["classification"] == C.PASS
    # 한쪽에서만 읽힘 -> 불일치
    c3 = [{"field": "seller_name", "invoice": None, "bl": CLEAN["seller_name"]}]
    assert _rules(screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c3))) == [C.RULE_DOC_CONFLICT]


def test_doc_conflict_spacing_only_ignored(idx):
    c = [{"field": "buyer_name", "invoice": "Fuji Coral Electronics LLC", "bl": "Fuji C oral Electronics LLC"}]
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c))["classification"] == C.PASS
    # 법인 접미어 차이는 진짜 불일치
    c2 = [{"field": "buyer_name", "invoice": "Fuji Coral Electronics LLC", "bl": "Fuji Coral Electronics Ltd"}]
    assert screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c2))["classification"] == C.REVIEW


def test_doc_conflict_other_doc_value_screened(idx):
    """Invoice 값은 깨끗해도 B/L 값이 제재 대상이면 HOLD. 근거 규칙 이름은 *_match_other_doc."""
    fields = dict(CLEAN, seller_name="UK EL OOO INC")
    for bl, rule_cls in (("UK ELSI OOO INC", C.HOLD),     # v2 정확 일치
                         ("UK ELS! OOO INC", C.HOLD),     # 혼동 글자 감안 일치 (HOLD 조건 충족)
                         ("UK ELS!000 INC", C.REVIEW)):   # 숫자가 글자로 접힌 일치 -> REVIEW
        c = [{"field": "seller_name", "invoice": "UK EL OOO INC", "bl": bl}]
        res = screen(fields, idx, C.RULES_NEW, _ocr(conflicts=c))
        od = [r for r in res["reasons"] if r["rule"] == C.RULE_PARTY_OTHER_DOC]
        assert len(od) == 1, od                              # 같은 rule·field·value 는 한 번만
        assert od[0]["value"] == bl and od[0]["entry_id"] == "T-2" and od[0]["classification"] == rule_cls
        assert res["classification"] == rule_cls
        assert not {C.RULE_PARTY_EXACT, C.RULE_PARTY_FUZZY, C.RULE_PARTY_EXACT_OCR} & {
            r["rule"] for r in res["reasons"] if r["value"] == bl}   # 다른 서류 값은 v2 규칙 이름을 쓰지 않음


def test_other_doc_vessel_rule_name(idx):
    c = [{"field": "vessel_name", "invoice": CLEAN["vessel_name"], "bl": "MV OCEAN BLOSSOM"}]
    res = screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c))
    od = [r for r in res["reasons"] if r["rule"] == C.RULE_VESSEL_OTHER_DOC]
    assert len(od) == 1 and od[0]["classification"] == C.HOLD and od[0]["entry_id"] == "T-5"
    assert res["classification"] == C.HOLD


def test_no_duplicate_reasons(idx):
    """같은 conflict 가 여러 번 와도 (rule, field, value) 근거는 한 번만."""
    c = [{"field": "seller_name", "invoice": CLEAN["seller_name"], "bl": "UK ELSI OOO INC"}] * 3
    res = screen(CLEAN, idx, C.RULES_NEW, _ocr(conflicts=c))
    keys = [(r["rule"], r["field"], r["value"]) for r in res["reasons"]]
    assert len(keys) == len(set(keys))
    assert keys.count((C.RULE_DOC_CONFLICT, "seller_name", CLEAN["seller_name"])) == 1


def test_missing_field(idx):
    for empty in (None, "", "  ", ". :"):
        res = screen(dict(CLEAN, vessel_name=empty), idx, C.RULES_NEW, _ocr())
        assert res["classification"] == C.REVIEW and _rules(res) == [C.RULE_MISSING_FIELD]
    assert screen(dict(CLEAN, vessel_name=None), idx, C.RULES_NEW)["classification"] == C.PASS
    assert screen(dict(CLEAN, vessel_name=None), idx)["classification"] == C.PASS


def test_reason_fields_complete(idx):
    fields = {"seller_name": "Asteria Harbor Trading Lic", "buyer_name": "", "vessel_name": "G J ADES"}
    c = [{"field": "seller_name", "invoice": "Asteria Harbor Trading Lic", "bl": "ASTERIA HARBOUR TRADNG"}]
    res = screen(fields, idx, C.RULES_NEW, _ocr({"vessel_name": 40.0}, c))
    rules = set(_rules(res))
    assert {C.RULE_PARTY_EXACT_OCR, C.RULE_VESSEL_EXACT_OCR, C.RULE_MISSING_FIELD,
            C.RULE_LOW_CONFIDENCE, C.RULE_DOC_CONFLICT} <= rules
    for r in res["reasons"]:
        assert tuple(r) == C.REASON_FIELDS
        assert r["classification"] in C.CLASSIFICATIONS


def test_v3_ge_v2_random(idx):
    """무작위 이름·OCR 정보에서도 v3 등급 >= v2 등급."""
    rnd = random.Random(20261108)
    pool = [n for e in SAMPLE_LIST["entries"] for n in [e["name"], *e["aliases"]]] + list(CLEAN.values())
    noise = "IlO0|!8B 5S.,"
    for _ in range(400):
        fields = {}
        for f in C.PARTY_FIELDS + C.VESSEL_FIELDS:
            s = list(rnd.choice(pool))
            for _ in range(rnd.randint(0, 3)):
                s[rnd.randrange(len(s))] = rnd.choice(noise)
            fields[f] = "".join(s) if rnd.random() > 0.1 else None
        ocr = _ocr({f: rnd.uniform(50, 100) for f in fields}) if rnd.random() > 0.3 else None
        v2, v3 = screen(fields, idx), screen(fields, idx, C.RULES_NEW, ocr)
        assert _SEV[v3["classification"]] >= _SEV[v2["classification"]]
        assert v3["reasons"][:len(v2["reasons"])] == v2["reasons"]   # v2 근거는 그대로 앞에


# ---------------------------------------------------------------- rules-v3: ocr 인자 형식이 이상할 때

def test_malformed_ocr_examples(idx):
    cases = [
        ("not a dict", "ocr"),
        ([1, 2], "ocr"),
        ({"confidence": [95.0], "conflicts": []}, "ocr.confidence"),
        ({"confidence": None, "conflicts": "x"}, "ocr.conflicts"),
        ({"confidence": {}, "conflicts": [None, 3]}, "ocr.conflicts"),
        ({"confidence": {}, "conflicts": [{"field": ["seller_name"], "invoice": "a", "bl": "b"}]}, "ocr.conflicts"),
        ({"confidence": {}, "conflicts": [{"field": "seller_name", "invoice": 5, "bl": "x"}]}, "seller_name"),
    ]
    for ocr, field in cases:
        v2 = screen(CLEAN, idx)
        res = screen(CLEAN, idx, C.RULES_NEW, ocr)
        assert res["classification"] == C.REVIEW, ocr
        assert res["reasons"][:len(v2["reasons"])] == v2["reasons"]
        mf = [r for r in res["reasons"] if r["rule"] == C.RULE_MISSING_FIELD and r["field"] == field]
        assert mf and all(isinstance(r["value"], str) and r["value"] for r in mf), (ocr, res["reasons"])


def test_malformed_ocr_keeps_hold(idx):
    """형식이 이상해도 v2 근거(HOLD)는 그대로."""
    fields = dict(CLEAN, seller_name="ASTERIA HARBOR TRADING LLC")
    for ocr in ("x", {"confidence": "x", "conflicts": {"a": 1}}, {"conflicts": [{"field": None}]}):
        res = screen(fields, idx, C.RULES_NEW, ocr)
        assert res["classification"] == C.HOLD and res["reasons"][0]["rule"] == C.RULE_PARTY_EXACT


def _rand_obj(rnd: random.Random, depth: int = 0):
    atoms = [None, True, False, 0, -1, 1e309, float("nan"), float("-inf"), 42.5, 85.0, 120, "", "abc", "95",
             b"bytes", (1, 2), "UK ELSI OOO INC", "G JADES", "seller_name", "vessel_name", "invoice", "bl"]
    if depth > 2 or rnd.random() < 0.5:
        return rnd.choice(atoms)
    if rnd.random() < 0.5:
        return [_rand_obj(rnd, depth + 1) for _ in range(rnd.randint(0, 4))]
    keys = ["field", "invoice", "bl", "confidence", "conflicts", *C.SCREENING_FIELDS, 1, None, ("t",)]
    return {rnd.choice(keys): _rand_obj(rnd, depth + 1) for _ in range(rnd.randint(0, 4))}


def _rand_ocr(rnd: random.Random):
    r = rnd.random()
    if r < 0.15:
        return _rand_obj(rnd)
    conf = _rand_obj(rnd) if rnd.random() < 0.3 else {
        f: rnd.choice([None, 50.0, 95.0, "x", float("nan"), [1], {}, True, -5, 101]) for f in C.SCREENING_FIELDS
        if rnd.random() < 0.8}
    if rnd.random() < 0.3:
        conflicts = _rand_obj(rnd)
    else:
        conflicts = []
        for _ in range(rnd.randint(0, 4)):
            item = {"field": rnd.choice([*C.SCREENING_FIELDS, "amount", None, 3, ["seller_name"]])}
            for doc in C.DOC_TYPES:
                item[doc] = rnd.choice([None, "", "UK ELSI OOO INC", "UK ELS! OOO INC", "G J ADES", "OCEAN", 7,
                                        ["x"], {"a": 1}, float("nan")])
            conflicts.append(item if rnd.random() < 0.85 else _rand_obj(rnd))
    out = {"confidence": conf, "conflicts": conflicts}
    if rnd.random() < 0.1:
        out.pop(rnd.choice(["confidence", "conflicts"]))
    return out


def test_fuzz_malformed_ocr(idx):
    """임의 형식 ocr: 예외 없음, v3 >= v2, v2 근거는 그대로 앞에, 근거 키·분류 정상, JSON 직렬화 가능(NaN 없음)."""
    rnd = random.Random(1108)
    pool = [n for e in SAMPLE_LIST["entries"] for n in [e["name"], *e["aliases"]]] + list(CLEAN.values())
    for _ in range(1500):
        fields = {f: rnd.choice(pool + [None, "", "lV OCEAN BLOSSOM", "UK ELS| OOO INC"]) for f in C.SCREENING_FIELDS}
        ocr = _rand_ocr(rnd)
        snapshot = copy.deepcopy(ocr) if not isinstance(ocr, float) else ocr
        v2, v3 = screen(fields, idx), screen(fields, idx, C.RULES_NEW, ocr)
        assert _SEV[v3["classification"]] >= _SEV[v2["classification"]]
        assert v3["reasons"][:len(v2["reasons"])] == v2["reasons"]
        for r in v3["reasons"]:
            assert tuple(r) == C.REASON_FIELDS and r["classification"] in C.CLASSIFICATIONS
            assert r["score"] is None or (isinstance(r["score"], float) and math.isfinite(r["score"]))
        json.dumps(v3, allow_nan=False)
        keys = [(r["rule"], r["field"], repr(r["value"])) for r in v3["reasons"]]
        assert len(keys) == len(set(keys))
        if isinstance(ocr, (dict, list)):
            assert repr(ocr) == repr(snapshot)   # 입력을 바꾸지 않음 (NaN 때문에 == 대신 repr)


# ---------------------------------------------------------------- 데이터 기반

def _data_inputs():
    """(키, fields, ocr) 목록. 스냅샷 스크립트와 같은 순서."""
    out = []
    for p in sorted(C.DOCS_DIR.glob("TX-*/truth.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(("truth/" + d["tx_id"], d["fields"], None, d.get("case_type")))
    for src, base in (("ocr", C.OCR_DIR), ("ocr-v4", C.OCR_DIR / C.MODEL_NEW)):
        for p in sorted(base.glob("TX-*/extracted.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            fields = {f: (d["fields"].get(f) or {}).get("value") for f in C.TX_FIELDS}
            ocr = {"confidence": {f: (v or {}).get("confidence") for f, v in d["fields"].items()},
                   "conflicts": d.get("conflicts") or []}
            out.append((f"{src}/{d['tx_id']}", fields, ocr, None))
    return out


def _have_data() -> bool:
    return all(C.list_path(v).exists() for v in (C.LIST_BASE, C.LIST_NEW)) and any(C.DOCS_DIR.glob("TX-*"))


needs_data = pytest.mark.skipif(not _have_data(), reason="data/ 없음")


@pytest.fixture(scope="module")
def data():
    inputs = _data_inputs()
    idxs = {v: load_index(v) for v in (C.LIST_BASE, C.LIST_NEW)}
    return inputs, idxs


def _input_digest(inputs) -> str:
    lists = {}
    for v in (C.LIST_BASE, C.LIST_NEW):
        d = json.loads(C.list_path(v).read_text(encoding="utf-8"))
        lists[v] = d["entries"]          # created_at 은 실행 시각이라 제외
    blob = json.dumps({"lists": lists, "inputs": [(k, f) for k, f, _, _ in inputs]}, ensure_ascii=False,
                      sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@needs_data
def test_rules_v2_snapshot(data):
    inputs, idxs = data
    out = {}
    for lv, index in idxs.items():
        for k, f, _, _ in inputs:
            out[f"{lv}/{k}"] = screen(f, index)
    text = json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1)
    snap = os.environ.get("TRADEPROOF_V2_SNAPSHOT")
    if snap and Path(snap).exists():
        assert text == Path(snap).read_text(encoding="utf-8")
        return
    if _input_digest(inputs) != V2_INPUT_SHA256:
        pytest.skip("데이터가 스냅샷 때와 다름 (재생성·다른 tesseract). TRADEPROOF_V2_SNAPSHOT 으로 비교")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == V2_OUTPUT_SHA256


@needs_data
def test_data_v3_ge_v2_and_clean_truth_not_hold(data):
    inputs, idxs = data
    for lv, index in idxs.items():
        for k, f, ocr, case in inputs:
            v2, v3 = screen(f, index), screen(f, index, C.RULES_NEW, ocr)
            assert _SEV[v3["classification"]] >= _SEV[v2["classification"]], (lv, k)
            if case == C.CASE_CLEAN:
                assert v3["classification"] != C.HOLD, (lv, k, v3["reasons"])
            if case is not None:   # 정답 필드는 기대 분류와 같아야 함 (v3 가 정답을 바꾸지 않음)
                assert v3["classification"] == C.EXPECTED[case][lv], (lv, k, v3["reasons"])


@needs_data
def test_data_v3_no_miss(data):
    """OCR 추출 값(rules-v3, ocr 정보 포함)이 정답 기준 HOLD/REVIEW 인 거래를 PASS 로 내리지 않는다 (미탐 0)."""
    inputs, idxs = data
    truth = {k.split("/", 1)[1]: f for k, f, _, case in inputs if k.startswith("truth/")}
    for lv, index in idxs.items():
        for k, f, ocr, _ in inputs:
            if k.startswith("truth/"):
                continue
            tx = k.split("/", 1)[1]
            if tx not in truth:
                continue
            ct = screen(truth[tx], index, C.RULES_NEW)["classification"]
            ce = screen(f, index, C.RULES_NEW, ocr)["classification"]
            assert not (ct != C.PASS and ce == C.PASS), (lv, k, ct, ce)


@needs_data
def test_data_list_v15_known_false_positives(data):
    _, idxs = data
    index = idxs[C.LIST_NEW]
    assert screen(FP_BASE, index)["classification"] == C.PASS
    for fields in _fp_cases():
        assert screen(fields, index)["classification"] != C.HOLD
        for ocr in (None, {}, _ocr()):
            assert screen(fields, index, C.RULES_NEW, ocr)["classification"] != C.HOLD, fields
