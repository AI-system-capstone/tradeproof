"""OFAC SDN 레거시 CSV(SDN.CSV, ALT.CSV, ADD.CSV)에서 제재 리스트 list-v14 / list-v15 JSON을 만든다.

- 개인(individual)과 항공기(aircraft)는 버리고, 단체(SDN_Type이 -0-)와 선박(vessel)만 남긴다.
- 후보를 ent_num 순으로 정렬한 뒤 random.Random(seed).sample 로 뽑는다 (단체 먼저, 그 다음 선박).
  같은 입력 + 같은 seed 이면 항상 같은 결과가 나온다.
- created_at 을 안 주면 실행 시각이 아니라 source_fetched_at 기반 고정값(<fetched_at>T00:00:00+00:00)을 쓴다.
  같은 입력이면 JSON 파일 바이트(sha256)가 매번 같다 (B의 해시 검증·결정성 규칙).
- list-v15 = list-v14 항목 + contracts.NEW_LISTING (가상 제재 대상 'A사').

사용법:
  python sanctions/build_lists.py [--src DIR] [--out DIR] [--entities 250] [--vessels 50] [--seed 7]
                                  [--fetched-at YYYY-MM-DD] [--created-at ISO8601]

다른 스크립트에서는 build(...) 를 import 해서 쓴다.
표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import random
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from a_screening.screening.match import FUZZY_THRESHOLD, normalize  # noqa: E402

DEFAULT_SRC = Path(__file__).resolve().parent / "fixtures"
SOURCE_LABEL = "OFAC SDN (sampled)"
MAX_ALIASES = 5
DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"   # fetched_at 도 없을 때의 고정값

SDN_COLS = ("ent_num", "SDN_Name", "SDN_Type", "Program", "Title", "Call_Sign",
            "Vess_type", "Tonnage", "GRT", "Vess_flag", "Vess_owner", "Remarks")
ALT_COLS = ("ent_num", "alt_num", "alt_type", "alt_name", "alt_remarks")
ADD_COLS = ("Ent_num", "Add_num", "Address", "City", "Country", "Add_remarks")

# 국가/국적 칸에 들어 있지만 실제로는 '모름'을 뜻하는 값
_UNKNOWN_PLACES = {"NONE IDENTIFIED", "UNKNOWN"}


# ---------------------------------------------------------------- CSV 읽기
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")   # latin-1 은 실패하지 않는다


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return None if v in ("", "-0-") else v


def _find(src: Path, filename: str) -> Path | None:
    """대소문자 상관없이 파일을 찾는다 (SDN.CSV / sdn.csv)."""
    exact = src / filename
    if exact.is_file():
        return exact
    for p in src.iterdir() if src.is_dir() else []:
        if p.is_file() and p.name.lower() == filename.lower():
            return p
    return None


def read_rows(path: Path, cols: tuple[str, ...]) -> list[dict]:
    """헤더 없는 OFAC CSV 한 개를 dict 리스트로 읽는다. -0- 는 None 으로 바꾼다."""
    text = _decode(path.read_bytes()).replace("\x1a", "")
    rows = []
    for rec in csv.reader(io.StringIO(text, newline="")):
        if not rec or all(not f.strip() for f in rec):
            continue
        rec = (rec + [""] * len(cols))[:len(cols)]
        row = {c: _clean(v) for c, v in zip(cols, rec)}
        key = row[cols[0]]
        if key is None or not key.isdigit():
            continue   # 깨진 줄 / 머리말 방어
        row[cols[0]] = int(key)
        rows.append(row)
    return rows


def split_programs(program: str | None) -> list[str]:
    """'SDGT] [IRGC', '[SDGT] [IRGC]', 'CUBA' -> ['SDGT', 'IRGC'] / ['CUBA']"""
    if not program:
        return []
    out = []
    for p in re.split(r"\]\s*\[", program.strip()):
        p = p.strip(" []")
        if p and p not in out:
            out.append(p)
    return out


def _place(v: str | None) -> str | None:
    if v is None or v.upper() in _UNKNOWN_PLACES:
        return None
    return v


# ---------------------------------------------------------------- 항목 만들기
def _natural_key(entry_id: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", entry_id)]


def sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: _natural_key(e["entry_id"]))


def _aliases(name: str, alt_rows: list[dict], vessel: bool) -> list[str]:
    seen = {normalize(name, vessel=vessel)}
    out = []
    for r in sorted(alt_rows, key=lambda r: int(r["alt_num"]) if (r["alt_num"] or "").isdigit() else 0):
        a = r["alt_name"]
        n = normalize(a, vessel=vessel)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(a)
        if len(out) >= MAX_ALIASES:
            break
    return out


def make_entry(sdn: dict, alt_rows: list[dict], add_rows: list[dict]) -> dict:
    vessel = (sdn["SDN_Type"] or "").lower() == "vessel"
    name = sdn["SDN_Name"]
    if vessel:
        country = _place(sdn["Vess_flag"])
    else:
        country = None
        for r in sorted(add_rows, key=lambda r: int(r["Add_num"]) if (r["Add_num"] or "").isdigit() else 0):
            country = _place(r["Country"])
            if country:
                break
    entry = {
        "entry_id": f"OFAC-{sdn['ent_num']}",
        "name": name,
        "aliases": _aliases(name, alt_rows, vessel),
        "entity_type": C.VESSEL if vessel else C.ENTITY,
        "country": country,
        "programs": split_programs(sdn["Program"]),
        "call_sign": sdn["Call_Sign"] if vessel else None,
        "vessel_flag": _place(sdn["Vess_flag"]) if vessel else None,
        "synthetic": False,
    }
    assert tuple(entry) == C.ENTRY_FIELDS, "entry fields drifted from contracts.ENTRY_FIELDS"
    return entry


def _sample(cands: list[dict], n: int, rng: random.Random) -> list[dict]:
    cands = sorted(cands, key=lambda r: r["ent_num"])
    if n >= len(cands):
        return cands
    return rng.sample(cands, n)


def _check_new_listing(entries: list[dict]) -> list[str]:
    """NEW_LISTING 이름이 v14 이름/별칭과 겹치면 실패. 유사도가 퍼지 임계값 이상이면 경고 목록을 돌려준다."""
    new = C.NEW_LISTING
    new_names = {normalize(n) for n in [new["name"], *new["aliases"]]} - {""}
    if any(e["entry_id"] == new["entry_id"] for e in entries):
        raise SystemExit(f"[build_lists] entry_id 충돌: {new['entry_id']} 가 이미 v14에 있음")
    warnings = []
    for e in entries:
        for raw in [e["name"], *e["aliases"]]:
            n = normalize(raw)
            if n in new_names:
                raise SystemExit(f"[build_lists] NEW_LISTING 이름 충돌: '{raw}' ({e['entry_id']}) == '{n}'")
            for nn in new_names:
                sc = SequenceMatcher(None, nn, n).ratio()
                if sc >= FUZZY_THRESHOLD:
                    warnings.append(f"NEW_LISTING '{nn}' ~ '{raw}' ({e['entry_id']}) score={sc:.3f} "
                                    f"(>= {FUZZY_THRESHOLD}: v14에서 new_listing 케이스가 PASS가 아닐 수 있음)")
    return warnings


def _dump(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def default_created_at(fetched_at: str | None) -> str:
    """created_at 기본값. 실행 시각을 쓰면 다시 만들 때마다 파일 해시가 바뀌므로 fetched_at 날짜 자정(UTC)으로 고정한다."""
    return f"{fetched_at}T00:00:00+00:00" if fetched_at else DEFAULT_CREATED_AT


def build(src, out, entities: int = 250, vessels: int = 50, seed: int = 7,
          fetched_at: str | None = None, created_at: str | None = None) -> tuple[dict, dict]:
    """CSV를 읽어 (v14_dict, v15_dict)를 돌려준다. out 이 None 이 아니면 파일도 쓴다."""
    src = Path(src)
    sdn_path = _find(src, "SDN.CSV")
    if sdn_path is None:
        raise SystemExit(f"[build_lists] SDN.CSV 가 없음: {src}")
    alt_path, add_path = _find(src, "ALT.CSV"), _find(src, "ADD.CSV")
    for label, p in (("ALT.CSV", alt_path), ("ADD.CSV", add_path)):
        if p is None:
            print(f"[build_lists] 경고: {label} 가 없음 ({src}) - 해당 정보 없이 진행", file=sys.stderr)

    sdn_rows = read_rows(sdn_path, SDN_COLS)
    alt_by, add_by = {}, {}
    for r in read_rows(alt_path, ALT_COLS) if alt_path else []:
        if r["alt_name"]:
            alt_by.setdefault(r["ent_num"], []).append(r)
    for r in read_rows(add_path, ADD_COLS) if add_path else []:
        add_by.setdefault(r["Ent_num"], []).append(r)

    ent_cands = [r for r in sdn_rows if r["SDN_Type"] is None and r["SDN_Name"]]
    ves_cands = [r for r in sdn_rows if (r["SDN_Type"] or "").lower() == "vessel" and r["SDN_Name"]]
    rng = random.Random(seed)
    picked = _sample(ent_cands, entities, rng) + _sample(ves_cands, vessels, rng)
    entries = sort_entries([make_entry(r, alt_by.get(r["ent_num"], []), add_by.get(r["ent_num"], []))
                            for r in picked])

    warnings = _check_new_listing(entries)
    for w in warnings:
        print(f"[build_lists] 경고: {w}", file=sys.stderr)

    if fetched_at is None:
        fetched_at = datetime.fromtimestamp(sdn_path.stat().st_mtime).date().isoformat()
    if created_at is None:
        created_at = default_created_at(fetched_at)

    def wrap(version: str, items: list[dict]) -> dict:
        return {"list_version": version, "source": SOURCE_LABEL, "source_fetched_at": fetched_at,
                "created_at": created_at, "entry_count": len(items), "entries": items}

    v14 = wrap(C.LIST_BASE, entries)
    v15 = wrap(C.LIST_NEW, sort_entries(copy.deepcopy(entries) + [copy.deepcopy(C.NEW_LISTING)]))

    stats = {"sdn_rows": len(sdn_rows), "entity_candidates": len(ent_cands),
             "vessel_candidates": len(ves_cands), "warnings": warnings}
    if out is not None:
        out = Path(out)
        stats["paths"] = [out / f"{C.LIST_BASE}.json", out / f"{C.LIST_NEW}.json"]
        _dump(stats["paths"][0], v14)
        _dump(stats["paths"][1], v15)
    build.last_stats = stats
    return v14, v15


def _summary(v14: dict, v15: dict, args) -> str:
    def count(d, t):
        return sum(1 for e in d["entries"] if e["entity_type"] == t)
    st = getattr(build, "last_stats", {})
    lines = [
        f"src: {Path(args.src).resolve()}  (SDN rows {st.get('sdn_rows')}, "
        f"entity candidates {st.get('entity_candidates')}, vessel candidates {st.get('vessel_candidates')})",
        f"seed={args.seed} requested entities={args.entities} vessels={args.vessels}",
        f"{v14['list_version']}: {v14['entry_count']} entries "
        f"(entity {count(v14, C.ENTITY)}, vessel {count(v14, C.VESSEL)})",
        f"{v15['list_version']}: {v15['entry_count']} entries "
        f"(entity {count(v15, C.ENTITY)}, vessel {count(v15, C.VESSEL)}; + {C.NEW_LISTING['entry_id']})",
        f"with aliases: {sum(1 for e in v14['entries'] if e['aliases'])}, "
        f"without country: {sum(1 for e in v14['entries'] if not e['country'])}",
    ]
    for p in st.get("paths", []):
        lines.append(f"wrote {p}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OFAC SDN CSV -> list-v14.json / list-v15.json")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="SDN.CSV/ALT.CSV/ADD.CSV 가 있는 폴더 (기본: sanctions/fixtures)")
    ap.add_argument("--out", default=str(C.SANCTIONS_DIR), help="출력 폴더 (기본: data/sanctions)")
    ap.add_argument("--entities", type=int, default=250, help="뽑을 단체 수")
    ap.add_argument("--vessels", type=int, default=50, help="뽑을 선박 수")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fetched-at", default=None, help="원본 내려받은 날짜 (기본: SDN.CSV 수정 시각)")
    ap.add_argument("--created-at", default=None, help="생성 시각 ISO8601 (기본: <fetched-at>T00:00:00+00:00 고정값, 재현 가능)")
    args = ap.parse_args(argv)
    if args.entities < 0 or args.vessels < 0:
        ap.error("--entities/--vessels 는 0 이상")
    v14, v15 = build(args.src, args.out, args.entities, args.vessels, args.seed, args.fetched_at, args.created_at)
    print(_summary(v14, v15, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
