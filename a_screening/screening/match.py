"""기본 제재 스크리닝 로직 (라이브러리).

입력: 추출 필드(dict) + 제재 리스트(JSON dict)  ->  출력: 분류 + 근거.
외부 상태를 읽지 않는 순수 함수라서, 심사 서비스와 C의 Replay가 같이 쓴다.

규칙 (rules-v2 가정):
  - 당사자 이름/별칭이 정규화 후 정확히 일치        -> HIGH_RISK_HOLD
  - 선박 이름이 제재 선박과 정규화 후 정확히 일치     -> HIGH_RISK_HOLD
  - 유사도 >= FUZZY_THRESHOLD                      -> MANUAL_REVIEW
  - 그 외                                          -> PASS_CANDIDATE
유사도는 표준 라이브러리 difflib을 쓴다 (환경마다 같은 점수가 나오도록).

rules-v3 = rules-v2 근거 전부 + 아래 추가 근거 (등급은 근거 중 최고라서 v3 >= v2 가 항상 성립).
추가 근거는 OCR 입력(ocr 인자가 있음)에서만 붙는다. ocr=None(직접 입력한 필드)이면 v3 근거 = v2 근거.
  - party_exact_ocr / vessel_exact_ocr  OCR 혼동 글자·띄어쓰기를 같게 보고 비교하면 정확 일치
      HOLD  : 접기 전 v2 유사도 >= FUZZY_THRESHOLD 이고 비교 키 > _OCR_KEY_MIN 자이고 숫자가 없을 때
      REVIEW: 그 밖 (짧은 이름·숫자가 글자로 접힌 경우의 오탐 방지. 예: 'ALX Ltd'->AIX, 'FIORA'->FLORA)
    (값은 그대로 두고 비교 키만 관대하게 만든다)
  - party_match_other_doc / vessel_match_other_doc  Invoice/B-L 이 다르게 읽혔을 때 다른 서류 쪽 값으로
      v2 정확/유사 + 혼동 글자 감안 일치를 검사. 등급은 매칭 결과대로
  - low_confidence   스크리닝 필드 OCR 신뢰도 < OCR_CONFIDENCE_MIN, 또는 값이 있는데 신뢰도가 없음/숫자 아님 -> REVIEW
                     score = 신뢰도/100 (없으면 None)
  - doc_conflict     스크리닝 필드가 Invoice/B-L 에서 다름 (띄어쓰기·혼동 글자 차이만이면 제외) -> REVIEW
  - missing_field    스크리닝 필드를 못 읽음, 또는 ocr 부가 정보 형식이 이상함(value 에 무엇이 이상한지) -> REVIEW
같은 (rule, field, value) 근거는 한 번만 남긴다 (겹치면 더 높은 등급).
"""
from __future__ import annotations

import json
import math
import numbers
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402

RULES_VERSION = C.RULES_BASE   # 하위 호환용 상수 (screen 은 이 전역을 읽지 않고 C.RULES_BASE 를 직접 쓴다)
FUZZY_THRESHOLD = 0.85
_OCR_KEY_MIN = 5               # 혼동 글자 감안 일치를 HOLD 로 볼 최소 비교 키 길이 (이 길이 이하는 REVIEW)

# rules-v3 관대한 비교용 OCR 혼동 글자 묶음 (대문자로 바꾼 뒤 적용). 근거: data/ocr 정답 대조
#   I/l/1/!/|  : 'Llc'->'LIc', 'ELSI'->'ELS|', 'Steel'->'Stee!'      O/0/Q : 'OAO'->'O0AO', 'OOO'->'Q00'
#   B/8        : 'B.V.'->'8.V.' (model-v4 6건)    S/5/$ : 'Summit'->'$ umstit'    RN->M : 'Furniture'->'Fumiture' (5건)
_OCR_FOLD = str.maketrans({"L": "I", "1": "I", "!": "I", "|": "I", "0": "O", "Q": "O", "8": "B", "5": "S", "$": "S"})
_OCR_MULTI = (("RN", "M"),)
# 'MV' 의 M 이 'l' 로 읽혀 'lV' 가 되는 경우 (CLAUDE.md 알려진 이슈) -> 접두어로 같이 뗀다.
# ('MT'->'lT' 용 'IT' 는 data/ocr·model-v4 어디에도 나온 적 없고 'IT SIERRA'->SIERRA 같은 오탐만 늘려서 뺐다)
_OCR_VESSEL_PREFIX_EXTRA = {"IV"}
# 추출 값 쪽에서만 시도하는 대체 읽기: 'J' 가 ')' 로 읽힘 ('River) uniper', 'OJSC'->'0) SC').
# 리스트 이름의 괄호는 그대로 지워야 하므로 접기(_OCR_FOLD)가 아니라 값 쪽 후보로만 쓴다
_OCR_VALUE_VARIANTS = ((")", "J"),)

_LEGAL_SUFFIXES = {
    "LLC", "LTD", "LIMITED", "CO", "COMPANY", "INC", "CORP", "CORPORATION", "JSC", "OJSC",
    "PJSC", "CJSC", "SA", "GMBH", "FZE", "FZCO", "PLC", "LLP", "AG", "BV", "PTE", "SRL", "SPA",
}
_VESSEL_PREFIXES = {"MV", "MT", "SS"}
_SEVERITY = {C.PASS: 0, C.REVIEW: 1, C.HOLD: 2}


def _join_initials(tokens: list[str]) -> list[str]:
    """'L L C' 처럼 한 글자 토큰이 이어지면 'LLC' 로 붙인다 (L.L.C., S.A. 대응)."""
    out, run = [], []
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(t)
    if run:
        out.append("".join(run))
    return out


def normalize(name: str | None, vessel: bool = False) -> str:
    if not name:
        return ""
    # 악센트 제거: COMPAÑIA -> COMPANIA
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    tokens = _join_initials(s.split())
    if vessel:
        while tokens and tokens[0] in _VESSEL_PREFIXES:
            tokens = tokens[1:]
    else:
        while tokens and tokens[-1] in _LEGAL_SUFFIXES:
            tokens = tokens[:-1]
    return " ".join(tokens)


def _ocr_fold(name: str | None) -> str:
    """대문자로 바꾸고 OCR 혼동 글자를 대표 글자로 접는다 (비교 전용, 값 자체는 바꾸지 않음)."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.upper().replace("&", " AND ")
    for a, b in _OCR_MULTI:
        s = s.replace(a, b)
    s = s.translate(_OCR_FOLD)
    return re.sub(r"[^A-Z0-9 ]+", " ", s)


_OCR_SUFFIXES = {_ocr_fold(x).strip() for x in _LEGAL_SUFFIXES}
_OCR_VESSEL_PREFIXES = {_ocr_fold(x).strip() for x in _VESSEL_PREFIXES} | _OCR_VESSEL_PREFIX_EXTRA


def ocr_key(name: str | None, vessel: bool = False) -> str:
    """rules-v3 관대한 비교 키: 혼동 글자 접기 + 법인 접미어/선박 접두어 제거 + 띄어쓰기 무시.
    'Asteria Harbor Trading LIc' 와 'ASTERIA HARBOR TRADING LLC' 가 같은 키가 된다."""
    tokens = _join_initials(_ocr_fold(name).split())
    if vessel:
        while tokens and tokens[0] in _OCR_VESSEL_PREFIXES:
            tokens = tokens[1:]
    else:
        while tokens and tokens[-1] in _OCR_SUFFIXES:
            tokens = tokens[:-1]
    return "".join(tokens)


def _conflict_key(value: str | None) -> str:
    """Invoice/B-L 값 비교 키: 혼동 글자·띄어쓰기·문장부호만 무시 (법인 접미어 차이는 진짜 불일치로 본다)."""
    return _ocr_fold(value).replace(" ", "")


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


class ListIndex:
    """제재 리스트를 정규화해 둔 색인. 리스트 한 버전당 한 번 만든다."""

    def __init__(self, sanction_list: dict):
        self.list_version = sanction_list["list_version"]
        self.parties: list[tuple[str, str, dict]] = []   # (정규화 이름, 원래 이름, 항목)
        self.vessels: list[tuple[str, str, dict]] = []
        for e in sanction_list["entries"]:
            is_vessel = e.get("entity_type") == C.VESSEL
            bucket = self.vessels if is_vessel else self.parties
            for raw in [e["name"], *(e.get("aliases") or [])]:
                n = normalize(raw, vessel=is_vessel)
                if n:
                    bucket.append((n, raw, e))
        self.exact_parties = {n: (raw, e) for n, raw, e in self.parties}
        self.exact_vessels = {n: (raw, e) for n, raw, e in self.vessels}
        # rules-v3 관대한 비교용 (키가 겹치면 먼저 나온 항목)
        self.ocr_parties: dict[str, tuple[str, dict]] = {}
        self.ocr_vessels: dict[str, tuple[str, dict]] = {}
        for e in sanction_list["entries"]:
            is_vessel = e.get("entity_type") == C.VESSEL
            bucket = self.ocr_vessels if is_vessel else self.ocr_parties
            for raw in [e["name"], *(e.get("aliases") or [])]:
                k = ocr_key(raw, vessel=is_vessel)
                if k:
                    bucket.setdefault(k, (raw, e))


def _check(value: str, field: str, exact: dict, pool: list, vessel: bool) -> dict | None:
    n = normalize(value, vessel=vessel)
    if not n:
        return None
    if n in exact:
        raw, e = exact[n]
        return {"rule": C.RULE_VESSEL_EXACT if vessel else C.RULE_PARTY_EXACT, "field": field, "value": value,
                "entry_id": e["entry_id"], "matched_name": raw, "score": 1.0, "classification": C.HOLD}
    best = (0.0, None, None)
    for cand, raw, e in pool:
        sc = similarity(n, cand)
        if sc > best[0]:
            best = (sc, raw, e)
    if best[0] >= FUZZY_THRESHOLD:
        return {"rule": C.RULE_VESSEL_FUZZY if vessel else C.RULE_PARTY_FUZZY, "field": field, "value": value,
                "entry_id": best[2]["entry_id"], "matched_name": best[1], "score": round(best[0], 4),
                "classification": C.REVIEW}
    return None


def screen(fields: dict, index: ListIndex, rules_version: str = C.RULES_BASE, ocr: dict | None = None) -> dict:
    """거래 필드 하나를 판정한다. 반환: {classification, reasons, list_version, rules_version}

    rules_version: 번들의 rules_version (contracts.RULES_VERSIONS). 기본값 rules-v2 = 기존 동작 그대로.
    ocr: rules-v3 용 OCR 부가 정보 (없으면 None: 직접 입력한 필드 등)
         {"confidence": {field: float | None}, "conflicts": [{"field", "invoice", "bl"}, ...]}
         형식이 이상해도 예외를 내지 않고 missing_field(REVIEW) 근거로 남긴다.
    """
    if rules_version not in C.RULES_VERSIONS:
        raise ValueError(f"rules_version={rules_version!r} 는 지원하지 않음 ({C.RULES_VERSIONS})")
    if rules_version == C.RULES_NEW:
        return _screen_v3(fields, index, ocr)
    reasons = []
    for f in C.PARTY_FIELDS:
        r = _check(fields.get(f), f, index.exact_parties, index.parties, vessel=False)
        if r:
            reasons.append(r)
    for f in C.VESSEL_FIELDS:
        r = _check(fields.get(f), f, index.exact_vessels, index.vessels, vessel=True)
        if r:
            reasons.append(r)
    cls = C.PASS
    for r in reasons:
        if _SEVERITY[r["classification"]] > _SEVERITY[cls]:
            cls = r["classification"]
    return {"classification": cls, "reasons": reasons,
            "list_version": index.list_version, "rules_version": C.RULES_BASE}


def _reason(rule: str, field: str, value, cls: str, score=None, entry: dict | None = None,
            matched_name: str | None = None) -> dict:
    """근거 한 건 (contracts.REASON_FIELDS 키를 모두 가짐)."""
    return {"rule": rule, "field": field, "value": value,
            "entry_id": entry["entry_id"] if entry else None, "matched_name": matched_name,
            "score": score, "classification": cls}


def _check_ocr(value, field: str, index: ListIndex, vessel: bool, rule: str | None = None) -> dict | None:
    """혼동 글자를 감안한 정확 일치 (rules-v3, OCR 입력 전용).

    HOLD  : 접기 전 v2 유사도 >= FUZZY_THRESHOLD 이고 비교 키 > _OCR_KEY_MIN 자이고 숫자가 없을 때
            (숫자는 비교 키와 읽힌 값(접기 전 정규화) 둘 다 본다: 'A1X', 'Huji 8' 처럼 숫자가 글자로 접힌 일치는 약함)
    REVIEW: 그 밖의 접힌 정확 일치. score = 접기 전 v2 유사도."""
    if not isinstance(value, str) or not value:
        return None
    cands = [value] + [value.replace(a, b) for a, b in _OCR_VALUE_VARIANTS if a in value]
    book = index.ocr_vessels if vessel else index.ocr_parties
    hit = None
    for cand in cands:
        k = ocr_key(cand, vessel=vessel)
        if k and k in book:
            hit = (k, *book[k])
            break
    if not hit:
        return None
    k, raw, e = hit
    n_value = normalize(value, vessel=vessel)
    sim = similarity(n_value, normalize(raw, vessel=vessel))
    strong = sim >= FUZZY_THRESHOLD and len(k) > _OCR_KEY_MIN and not re.search(r"\d", k + n_value)
    if rule is None:
        rule = C.RULE_VESSEL_EXACT_OCR if vessel else C.RULE_PARTY_EXACT_OCR
    return _reason(rule, field, value, C.HOLD if strong else C.REVIEW, round(sim, 4), e, raw)


def _other_doc_reasons(value: str, field: str, index: ListIndex, vessel: bool) -> list[dict]:
    """다른 서류 쪽 값의 매칭 근거: v2 정확/유사 + (v2 정확 일치가 아니면) 혼동 글자 감안 일치.
    규칙 이름은 *_match_other_doc 로 바꾸고 등급·entry_id·score 는 매칭 결과 그대로 둔다."""
    rule = C.RULE_VESSEL_OTHER_DOC if vessel else C.RULE_PARTY_OTHER_DOC
    exact, pool = (index.exact_vessels, index.vessels) if vessel else (index.exact_parties, index.parties)
    out = []
    r = _check(value, field, exact, pool, vessel=vessel)
    if r:
        out.append(dict(r, rule=rule))
    if not r or r["classification"] != C.HOLD:
        r2 = _check_ocr(value, field, index, vessel, rule=rule)
        if r2:
            out.append(r2)
    return out


def _is_blank(value) -> bool:
    return not any(ch.isalnum() for ch in str(value or ""))


def is_blank(value) -> bool:
    """스크리닝에 쓸 수 없는 값인지 (정규화하면 아무것도 안 남음: None, '', '-', '.', 폭 없는 공백 등).
    API 접수·검토 정정·워커가 같은 기준을 쓴다. 판정 결과(rules-v2/v3)에는 영향 없음."""
    return not isinstance(value, str) or normalize(value) == ""


def _short(x, n: int = 60) -> str:
    s = repr(x)
    return s if len(s) <= n else s[: n - 3] + "..."


def _conf_value(cf) -> tuple[float | None, bool]:
    """신뢰도 값 -> (숫자 또는 None, 형식이 이상한지). None 은 '없음'(이상 아님), 0~100 유한 실수만 정상."""
    if cf is None:
        return None, False
    if isinstance(cf, bool) or not isinstance(cf, numbers.Real):
        return None, True
    try:
        x = float(cf)
    except (TypeError, ValueError, OverflowError):
        return None, True
    if not math.isfinite(x) or not 0.0 <= x <= 100.0:
        return None, True
    return x, False


def _parse_ocr(ocr) -> tuple[dict, dict, list[tuple[str, str]]]:
    """ocr 부가 정보 -> (신뢰도 dict, {스크리닝 필드: [(invoice, bl), ...]}, [(field, 무엇이 이상한지), ...]).
    어떤 형식이 와도 예외를 내지 않는다. 쓸 수 없는 부분은 버리고 이상하다고 기록한다."""
    bad: list[tuple[str, str]] = []
    if not isinstance(ocr, dict):
        bad.append(("ocr", f"ocr 부가 정보가 dict 가 아님: {type(ocr).__name__}"))
        return {}, {}, bad
    conf = ocr.get("confidence")
    if conf is None:
        conf = {}
    elif not isinstance(conf, dict):
        bad.append(("ocr.confidence", f"confidence 가 dict 가 아님: {type(conf).__name__}"))
        conf = {}
    raw = ocr.get("conflicts")
    conflicts: dict[str, list[tuple]] = {}
    if raw is None:
        raw = []
    elif not isinstance(raw, (list, tuple)):
        bad.append(("ocr.conflicts", f"conflicts 가 list 가 아님: {type(raw).__name__}"))
        raw = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            bad.append(("ocr.conflicts", f"conflicts[{i}] 가 dict 가 아님: {_short(c)}"))
            continue
        f = c.get("field")
        if not isinstance(f, str):
            bad.append(("ocr.conflicts", f"conflicts[{i}].field 가 문자열이 아님: {_short(f)}"))
            continue
        if f not in C.SCREENING_FIELDS:
            continue
        vals = []
        for doc in C.DOC_TYPES:
            v = c.get(doc)
            if v is not None and not isinstance(v, str):
                bad.append((f, f"conflicts[{i}].{doc} 가 문자열이 아님: {_short(v)}"))
                v = None      # 그 서류 쪽은 못 읽은 것으로 본다
            vals.append(v)
        conflicts.setdefault(f, []).append(tuple(vals))
    return conf, conflicts, bad


def _screen_v3(fields: dict, index: ListIndex, ocr) -> dict:
    """rules-v3: rules-v2 근거를 그대로 앞에 두고 추가 근거만 붙인다 (그래서 등급이 v2 보다 낮아질 수 없음)."""
    base = screen(fields, index, C.RULES_BASE)
    reasons = list(base["reasons"])
    def _key(r: dict) -> tuple:
        v = r["value"]
        return r["rule"], r["field"], v if isinstance(v, (str, type(None))) else _short(v, 200)

    pos = {_key(r): i for i, r in enumerate(reasons)}

    def add(r: dict) -> None:
        """같은 (rule, field, value) 는 한 번만. 겹치면 더 높은 등급을 남긴다 (v2 근거는 규칙 이름이 달라 안 겹침)."""
        key = _key(r)
        i = pos.get(key)
        if i is None:
            pos[key] = len(reasons)
            reasons.append(r)
        elif _SEVERITY[r["classification"]] > _SEVERITY[reasons[i]["classification"]]:
            reasons[i] = r

    if ocr is not None:
        conf, conflicts, bad = _parse_ocr(ocr)
        for f, what in bad:
            add(_reason(C.RULE_MISSING_FIELD, f, what, C.REVIEW))
        targets = [(f, False) for f in C.PARTY_FIELDS] + [(f, True) for f in C.VESSEL_FIELDS]
        # 1) 혼동 글자 감안 정확 일치 (추출 값). v2 정확 일치가 이미 있으면 생략
        held = {r["field"] for r in reasons if r["classification"] == C.HOLD}
        for f, vessel in targets:
            if f not in held:
                r = _check_ocr(fields.get(f), f, index, vessel)
                if r:
                    add(r)
        for f, vessel in targets:
            value = fields.get(f)
            # 2) Invoice/B-L 불일치: 다른 서류의 값도 매칭 규칙으로 검사하고, 진짜로 다르면 검토
            for vals in conflicts.get(f, []):
                for alt in vals:
                    if not _is_blank(alt) and alt != value:
                        for r in _other_doc_reasons(alt, f, index, vessel):
                            add(r)
                if len({_conflict_key(v) for v in vals}) > 1:
                    add(_reason(C.RULE_DOC_CONFLICT, f, value, C.REVIEW))
            # 3) 못 읽음
            if _is_blank(value):
                add(_reason(C.RULE_MISSING_FIELD, f, value, C.REVIEW))
                continue
            # 4) 신뢰도 낮음 / 없음 / 형식 이상
            raw_cf = conf.get(f)
            cf, malformed = _conf_value(raw_cf)
            if malformed:
                add(_reason(C.RULE_MISSING_FIELD, f, f"confidence[{f}] 값이 0~100 숫자가 아님: {_short(raw_cf)}",
                            C.REVIEW))
            if cf is None or cf < C.OCR_CONFIDENCE_MIN:
                add(_reason(C.RULE_LOW_CONFIDENCE, f, value, C.REVIEW, None if cf is None else round(cf / 100, 4)))
    cls = C.PASS
    for r in reasons:
        if _SEVERITY[r["classification"]] > _SEVERITY[cls]:
            cls = r["classification"]
    return {"classification": cls, "reasons": reasons,
            "list_version": index.list_version, "rules_version": C.RULES_NEW}


def best_party_score(name: str, index: ListIndex) -> float:
    """생성기에서 '정상 이름이 우연히 제재 대상과 비슷한지' 확인할 때 쓴다."""
    n = normalize(name)
    return max((similarity(n, cand) for cand, _, _ in index.parties), default=0.0)


def best_vessel_score(name: str, index: ListIndex) -> float:
    n = normalize(name, vessel=True)
    return max((similarity(n, cand) for cand, _, _ in index.vessels), default=0.0)


def load_index(version: str) -> ListIndex:
    with open(C.list_path(version), encoding="utf-8") as fh:
        return ListIndex(json.load(fh))
