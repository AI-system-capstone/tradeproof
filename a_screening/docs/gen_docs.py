"""합성 무역 거래 + 정답(truth) 생성기, 그리고 OCR용 서류(PDF/PNG) 렌더러.

입력:  <lists-dir>/list-v14.json, list-v15.json  (contracts.py 의 리스트 JSON 형태)
출력:  <out>/<tx_id>/{invoice.pdf, invoice.png, bl.pdf, bl.png, truth.json}
       <out>/manifest.json  (거래별 요약 + 케이스 집계)

모든 거래는 만들 때 screening.match.screen() 으로 두 리스트 버전 모두에서
contracts.EXPECTED[case][version] 과 같은 분류가 나오는지 확인한다.
같은 seed 이면 같은 결과가 나온다 (PDF도 reportlab invariant 모드로 고정).

사용법:
  python docs/gen_docs.py                          # 기본: count=100, data/sanctions -> data/docs
  python docs/gen_docs.py --count 20 --lists-dir /tmp/lists --out /tmp/docs
  python docs/gen_docs.py --no-render              # truth.json / manifest.json 만
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common import contracts as C  # noqa: E402
from a_screening.screening import match as M  # noqa: E402

CLEAN_MAX_SCORE = 0.75          # 정상 이름은 두 리스트 모두에서 이 점수 미만이어야 한다
MAX_RETRIES = 500
GOODS_MAX_LEN = 45
KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------- 단어 목록 (전부 가상)
NAME_WORDS = [
    "Blue", "Ridge", "Silver", "Harbor", "Golden", "Maple", "Summit", "Cedar", "Bright", "River",
    "Eastern", "Northgate", "Sunrise", "Pine", "Crystal", "Granite", "Coral", "Willow", "Hanbit", "Daehan",
    "Seorin", "Mirae", "Nordlicht", "Elbe", "Rhein", "Linden", "Kestrel", "Falcon", "Orchid", "Lotus",
    "Sakura", "Fuji", "Delta", "Prairie", "Atlas", "Beacon", "Clover", "Harmony", "Juniper", "Meadow",
    "Oakwood", "Polaris", "Redwood", "Saffron", "Tidewater", "Unity", "Vista", "Westfield", "Yellowstone", "Zenith",
]
INDUSTRIES = [
    "Steel", "Electronics", "Textiles", "Chemicals", "Foods", "Machinery", "Plastics", "Paper", "Auto Parts",
    "Logistics", "Solar", "Furniture", "Ceramics", "Cosmetics", "Pharma", "Semiconductor", "Rubber", "Glass",
]
SUFFIXES = ["Co., Ltd.", "LLC", "GmbH", "Inc.", "Corp.", "Ltd.", "Pte. Ltd.", "B.V.", "AG", "PLC"]

# 실제 항구 이름 -> (국가, B/L 번호용 3글자 코드)
PORTS = {
    "Busan": ("South Korea", "PUS"), "Incheon": ("South Korea", "INC"), "Gwangyang": ("South Korea", "KWY"),
    "Rotterdam": ("Netherlands", "RTM"), "Antwerp": ("Belgium", "ANR"), "Hamburg": ("Germany", "HAM"),
    "Singapore": ("Singapore", "SIN"), "Shanghai": ("China", "SHA"), "Ningbo": ("China", "NGB"),
    "Qingdao": ("China", "TAO"), "Jebel Ali": ("United Arab Emirates", "JEA"), "Los Angeles": ("United States", "LAX"),
    "Long Beach": ("United States", "LGB"), "Yokohama": ("Japan", "YOK"), "Kaohsiung": ("Taiwan", "KHH"),
    "Port Klang": ("Malaysia", "PKG"), "Ho Chi Minh City": ("Vietnam", "SGN"), "Felixstowe": ("United Kingdom", "FXT"),
    "Valencia": ("Spain", "VLC"), "Santos": ("Brazil", "SSZ"),
}

# (설명 형식, 수량 범위, 단위, 단가 범위 USD)
GOODS = [
    ("Hot-rolled steel coils, {q} MT", (40, 600), 650, 900),
    ("Cold-rolled steel sheets, {q} MT", (20, 400), 800, 1100),
    ("Polyethylene resin pellets, {q} MT", (20, 300), 1000, 1400),
    ("Lithium-ion battery cells, {q} pcs", (5000, 80000), 3, 9),
    ("LCD display panels, {q} pcs", (500, 9000), 40, 160),
    ("Cotton knit fabric, {q} rolls", (200, 3000), 90, 220),
    ("Frozen squid, {q} cartons", (500, 4000), 30, 70),
    ("Instant noodles, {q} cartons", (1000, 12000), 8, 15),
    ("Automotive brake pads, {q} sets", (1000, 20000), 6, 18),
    ("Industrial centrifugal pumps, {q} units", (10, 200), 900, 4500),
    ("Kraft paper rolls, {q} MT", (30, 500), 500, 750),
    ("Ceramic floor tiles, {q} pallets", (40, 400), 350, 700),
    ("Solar PV modules, {q} pcs", (500, 6000), 90, 160),
    ("Synthetic rubber, {q} MT", (20, 250), 1500, 2200),
    ("Office chairs, {q} pcs", (200, 3000), 35, 110),
    ("Cosmetic skin care sets, {q} boxes", (500, 8000), 12, 40),
    ("Memory semiconductor modules, {q} pcs", (2000, 50000), 15, 60),
    ("Float glass sheets, {q} crates", (50, 600), 180, 400),
]
CURRENCIES = [("USD", 1.0, 55), ("EUR", 0.92, 20), ("JPY", 150.0, 10), ("KRW", 1380.0, 15)]  # (코드, 환율, 가중치)

VESSEL_A = ["OCEAN", "PACIFIC", "SEA", "MORNING", "GRAND", "EVER", "SILVER", "BLUE", "NORTH", "GLORY",
            "HANA", "STELLAR", "CORAL", "EMERALD", "SUN", "BRAVE", "NOBLE", "ORIENT", "SPRING", "ROYAL"]
VESSEL_B = ["PIONEER", "GRACE", "VOYAGER", "HARMONY", "FORTUNE", "BREEZE", "SPIRIT", "LEGEND", "TRIUMPH",
            "MAJESTY", "UNITY", "PROMISE", "DAWN", "PEARL", "ODYSSEY", "COURAGE", "HOPE", "LIBERTY", "BRIDGE", "WAVE"]


# ---------------------------------------------------------------- 공통 도우미
def load_lists(lists_dir: Path) -> dict:
    out = {}
    for v in (C.LIST_BASE, C.LIST_NEW):
        p = Path(lists_dir) / f"{v}.json"
        if not p.exists():
            raise SystemExit(f"sanctions list not found: {p} (run sanctions/build_lists.py first)")
        with open(p, encoding="utf-8") as fh:
            lst = json.load(fh)
        if lst.get("list_version") != v:
            raise SystemExit(f"{p}: list_version is {lst.get('list_version')!r}, expected {v!r}")
        out[v] = lst
    return out


def case_counts(total: int) -> dict:
    """CASE_MIX 비율대로 total 을 나눈다. 합계는 total, 각 케이스 >= 1 (최대 잉여 방식)."""
    mix = C.CASE_MIX
    if total < len(mix):
        raise SystemExit(f"--count must be >= {len(mix)} so every case type gets at least one transaction")
    weight = sum(mix.values())
    exact = {k: total * w / weight for k, w in mix.items()}
    counts = {k: max(1, int(x)) for k, x in exact.items()}
    order = list(mix)  # dict 순서 = 동점일 때 우선순위
    while sum(counts.values()) < total:
        k = max(order, key=lambda c: (exact[c] - counts[c], -order.index(c)))
        counts[k] += 1
    while sum(counts.values()) > total:
        cands = [c for c in order if counts[c] > 1]
        k = max(cands, key=lambda c: (counts[c] - exact[c], order.index(c)))
        counts[k] -= 1
    return counts


def _legal_tail(tokens: list[str]) -> int:
    """이름 끝에서 normalize() 가 떼어내는 법인 접미사 토큰 수."""
    n = 0
    for t in reversed(tokens):
        parts = re.sub(r"[^A-Z0-9 ]+", " ", t.upper().replace("&", " AND ")).split()
        if parts and all(p in M._LEGAL_SUFFIXES for p in parts):
            n += 1
        else:
            break
    return n


# ---------------------------------------------------------------- 생성기
MIN_TARGET_LEN = 12  # exact/fuzzy 대상 제재 이름의 최소 길이 (정규화 후)


class Generator:
    def __init__(self, lists: dict, seed: int):
        self.rng = random.Random(seed)
        self.lists = lists
        self.idx = {v: M.ListIndex(lst) for v, lst in lists.items()}
        self.base = self.idx[C.LIST_BASE]
        self.new = self.idx[C.LIST_NEW]
        v14_entries = lists[C.LIST_BASE]["entries"]
        new_id = C.NEW_LISTING["entry_id"]
        # 정확 일치 후보: v14 개체 중 v15 에서도 같은 이름으로 남아 있는 것
        self.entity_pool = [e for e in v14_entries
                            if e.get("entity_type") != C.VESSEL and e["entry_id"] != new_id
                            and M.normalize(e["name"]) in self.new.exact_parties
                            # 실제 SDN의 짧은 이름은 오타 하나로 유사도가 크게 흔들려서 제외
                            and len(M.normalize(e["name"])) >= MIN_TARGET_LEN]
        self.vessel_pool = [e for e in v14_entries
                            if e.get("entity_type") == C.VESSEL
                            and M.normalize(e["name"], vessel=True) in self.new.exact_vessels
                            and len(M.normalize(e["name"], vessel=True)) >= 5]
        if not self.entity_pool:
            raise SystemExit("list-v14 has no entity entries usable for exact/fuzzy cases")
        if not self.vessel_pool:
            raise SystemExit("list-v14 has no vessel entries usable for sanctioned_vessel cases")
        self.rng.shuffle(self.entity_pool)
        self.rng.shuffle(self.vessel_pool)
        self._used_entities: set[str] = set()
        self._used_vessels: set[str] = set()
        self._used_names: set[str] = set()
        self._used_vessel_names: set[str] = set()
        self._used_inv: set[int] = set()
        self._used_bl: set[str] = set()

    # ---- 정상 이름
    def _clean_ok(self, name: str, vessel: bool = False) -> bool:
        f = M.best_vessel_score if vessel else M.best_party_score
        return all(f(name, ix) < CLEAN_MAX_SCORE for ix in self.idx.values())

    def clean_company(self) -> str:
        r = self.rng
        for _ in range(MAX_RETRIES):
            w1, w2 = r.sample(NAME_WORDS, 2)
            name = f"{w1} {w2} {r.choice(INDUSTRIES)} {r.choice(SUFFIXES)}"
            key = M.normalize(name)
            if key in self._used_names or not self._clean_ok(name):
                continue
            self._used_names.add(key)
            return name
        raise RuntimeError(f"could not generate a clean company name (best score < {CLEAN_MAX_SCORE}) "
                           f"after {MAX_RETRIES} tries")

    def clean_vessel(self) -> str:
        r = self.rng
        for _ in range(MAX_RETRIES):
            name = f"{r.choice(VESSEL_A)} {r.choice(VESSEL_B)}"
            if name in self._used_vessel_names and len(self._used_vessel_names) < 150:
                continue   # 처음 150척까지는 이름이 겹치지 않게
            if not self._clean_ok(name, vessel=True):
                continue
            self._used_vessel_names.add(name)
            return name
        raise RuntimeError(f"could not generate a clean vessel name (best score < {CLEAN_MAX_SCORE}) "
                           f"after {MAX_RETRIES} tries")

    # ---- 제재 대상 고르기 (가능하면 거래마다 다른 항목)
    def _pick(self, pool: list, used: set) -> dict:
        fresh = [e for e in pool if e["entry_id"] not in used]
        e = fresh[0] if fresh else self.rng.choice(pool)
        used.add(e["entry_id"])
        return e

    def _surface_variant(self, raw: str) -> str:
        """normalize() 결과는 그대로 두고 표기만 살짝 바꾼다."""
        r = self.rng
        tokens = raw.split()
        tail = _legal_tail(tokens)
        choice = r.random()
        if tail and choice < 0.35:                      # 법인 접미사 제거
            out = " ".join(tokens[:-tail])
        elif not tail and choice < 0.35:                # 법인 접미사 추가
            out = f"{raw} {r.choice(['LTD', 'LLC', 'CO', 'INC', 'CORP'])}"
        elif choice < 0.7:                              # 단어 첫 글자만 대문자
            out = raw.title()
        else:
            out = raw
        return out if M.normalize(out) == M.normalize(raw) else raw

    def exact_party(self) -> tuple[str, dict]:
        e = self._pick(self.entity_pool, self._used_entities)
        raw = self.rng.choice([e["name"], *(e.get("aliases") or [])])
        return self._surface_variant(raw), e

    def _misspell(self, raw: str) -> str | None:
        r = self.rng
        tokens = raw.split()
        core = len(tokens) - _legal_tail(tokens)
        if core <= 0:
            return None
        chars = list(" ".join(tokens[:core]))
        tail = " ".join(tokens[core:])
        for _ in range(r.choice([1, 1, 2])):
            letters = [i for i, ch in enumerate(chars) if ch.isalpha()]
            if len(letters) < 4:
                return None
            op = r.choice(["swap", "drop", "sub"])
            i = r.choice(letters)
            if op == "swap":
                j = i + 1
                if j >= len(chars) or not chars[j].isalpha() or chars[j].upper() == chars[i].upper():
                    continue
                chars[i], chars[j] = chars[j], chars[i]
            elif op == "drop":
                # 단어가 한 글자만 남지 않게
                if (i == 0 or not chars[i - 1].isalpha()) and (i + 1 >= len(chars) or not chars[i + 1].isalpha()):
                    continue
                del chars[i]
            else:
                pool = "AEIOU" if chars[i].upper() in "AEIOU" else "BCDFGHKLMNPRSTVZ"
                new = r.choice([c for c in pool if c != chars[i].upper()])
                chars[i] = new if chars[i].isupper() else new.lower()
        out = "".join(chars) + (f" {tail}" if tail else "")
        return out if out != raw else None

    def _fuzzy_ok(self, cand: str, entry: dict) -> bool:
        n = M.normalize(cand)
        if not n or any(n in ix.exact_parties for ix in self.idx.values()):
            return False
        for ix in self.idx.values():
            res = M._check(cand, "seller_name", ix.exact_parties, ix.parties, vessel=False)
            if not res or res["classification"] != C.REVIEW or res["entry_id"] != entry["entry_id"]:
                return False
        return True

    def fuzzy_party(self) -> tuple[str, dict]:
        # 아직 안 쓴 항목부터 시도하고, 항목마다 오타를 여러 번 만들어 본다
        fresh = [e for e in self.entity_pool if e["entry_id"] not in self._used_entities]
        used = [e for e in self.entity_pool if e["entry_id"] in self._used_entities]
        self.rng.shuffle(used)
        for e in fresh + used:
            for _ in range(30):
                cand = self._misspell(e["name"])
                if cand and self._fuzzy_ok(cand, e):
                    self._used_entities.add(e["entry_id"])
                    return cand, e
        raise RuntimeError("could not build a fuzzy_match misspelling (1-2 edits) that screens MANUAL_REVIEW on "
                           f"both lists for any of the {len(self.entity_pool)} v14 entities "
                           f"(names too short for similarity >= {M.FUZZY_THRESHOLD}?)")

    def new_listing_party(self) -> tuple[str, dict]:
        raw = self.rng.choice([C.NEW_LISTING["name"], *C.NEW_LISTING["aliases"]])
        return self._surface_variant(raw), C.NEW_LISTING

    def sanctioned_vessel(self) -> tuple[str, dict]:
        e = self._pick(self.vessel_pool, self._used_vessels)
        name = e["name"]
        if self.rng.random() < 0.4:
            name = f"MV {name}"
        return name, e

    # ---- 나머지 필드
    def base_fields(self) -> dict:
        r = self.rng
        pol, pod = r.sample(sorted(PORTS), 2)
        while PORTS[pol][0] == PORTS[pod][0]:
            pod = r.choice(sorted(PORTS))
        fmt, (qlo, qhi), plo, phi = r.choice(GOODS)
        q = r.randint(qlo, qhi)
        if qhi >= 1000:
            q = round(q, -2) or qlo
        goods = fmt.format(q=f"{q:,}")
        assert len(goods) <= GOODS_MAX_LEN, goods
        cur, fx, _ = r.choices(CURRENCIES, weights=[c[2] for c in CURRENCIES])[0]
        amount = q * r.uniform(plo, phi) * fx
        amount = round(amount, -3 if cur in ("JPY", "KRW") else 0) + (0 if cur in ("JPY", "KRW") else r.choice([0, 0.5, 0.25]))
        while True:
            inv = r.randint(1, 9999)
            if inv not in self._used_inv:
                self._used_inv.add(inv)
                break
        while True:
            bl = f"BL{PORTS[pol][1]}26{r.randint(0, 9999):04d}"
            if bl not in self._used_bl:
                self._used_bl.add(bl)
                break
        return {
            "invoice_no": f"INV-2026-{inv:04d}",
            "invoice_date": None,   # received_at 정한 뒤 채운다
            "bl_no": bl,
            "seller_name": None, "seller_country": PORTS[pol][0],
            "buyer_name": None, "buyer_country": PORTS[pod][0],
            "vessel_name": None,
            "port_of_loading": pol, "port_of_discharge": pod,
            "goods_description": goods,
            "currency": cur,
            "amount": f"{amount:,.2f}",
        }

    def make_tx(self, case: str) -> dict:
        r = self.rng
        for _ in range(MAX_RETRIES):
            f = self.base_fields()
            f["seller_name"] = self.clean_company()
            f["buyer_name"] = self.clean_company()
            f["vessel_name"] = self.clean_vessel()
            entry = None
            if case in (C.CASE_EXACT, C.CASE_FUZZY, C.CASE_NEW):
                name, entry = {C.CASE_EXACT: self.exact_party, C.CASE_FUZZY: self.fuzzy_party,
                               C.CASE_NEW: self.new_listing_party}[case]()
                side = r.choice(["seller", "buyer"])
                f[f"{side}_name"] = name
                if entry.get("country"):
                    f[f"{side}_country"] = entry["country"]
            elif case == C.CASE_VESSEL:
                f["vessel_name"], entry = self.sanctioned_vessel()
            elif case != C.CASE_CLEAN:
                raise ValueError(f"unknown case type {case!r}")
            expected = {v: C.EXPECTED[case][v] for v in (C.LIST_BASE, C.LIST_NEW)}
            got = {v: M.screen(f, self.idx[v]) for v in expected}
            if all(got[v]["classification"] == expected[v] for v in expected) and self._reasons_ok(case, entry, got):
                return {"case_type": case, "matched_entry_id": entry["entry_id"] if entry else None,
                        "expected": expected, "fields": f}
        detail = {v: got[v]["classification"] for v in got}
        raise RuntimeError(f"case {case!r}: could not satisfy expected {expected} after {MAX_RETRIES} tries "
                           f"(last screen result {detail}, fields {f})")

    @staticmethod
    def _reasons_ok(case: str, entry: dict | None, got: dict) -> bool:
        """사유(reason)가 정확히 한 건이고, 의도한 항목을 가리키는지."""
        for v, res in got.items():
            reasons = res["reasons"]
            if case == C.CASE_CLEAN or (case == C.CASE_NEW and v == C.LIST_BASE):
                if reasons:
                    return False
                continue
            if len(reasons) != 1 or reasons[0]["entry_id"] != entry["entry_id"]:
                return False
        return True


def assign_times(rng: random.Random, txs: list[dict]) -> None:
    # 2026-08-01 ~ 2026-10-31, 매일 08:00~18:59 (업무 시간) 중 분 단위로 겹치지 않게 뽑는다
    first = date(2026, 8, 1)
    days = (date(2026, 10, 31) - first).days + 1
    per_day = 11 * 60
    slots = sorted(rng.sample(range(days * per_day), len(txs)))
    for i, (tx, s) in enumerate(zip(txs, slots), 1):
        d, m = divmod(s, per_day)
        received = datetime.combine(first + timedelta(days=d), datetime.min.time(), tzinfo=KST) \
            + timedelta(hours=8, minutes=m, seconds=rng.randint(0, 59))
        tx["tx_id"] = f"TX-{i:04d}"
        tx["received_at"] = received.isoformat()
        inv = received.date() - timedelta(days=rng.randint(0, 6))
        tx["fields"]["invoice_date"] = max(inv, date(2026, 8, 1)).isoformat()


def build_truth(txs: list[dict], out: Path) -> list[dict]:
    # 경로는 DATA 기준 상대경로 ("docs/TX-0001/invoice.png").
    # DATA 밖으로 출력할 때(테스트)는 out 의 상위 폴더를 기준으로 같은 모양을 만든다.
    out = out.resolve()
    try:
        rel_base = out.relative_to(C.DATA.resolve())
    except ValueError:
        rel_base = Path(out.name)
    truths = []
    for tx in txs:
        prefix = (rel_base / tx["tx_id"]).as_posix()
        docs = {d: {"pdf": f"{prefix}/{d}.pdf", "png": f"{prefix}/{d}.png"} for d in C.DOC_TYPES}
        truths.append({
            "tx_id": tx["tx_id"],
            "received_at": tx["received_at"],
            "case_type": tx["case_type"],
            "matched_entry_id": tx["matched_entry_id"],
            "expected": tx["expected"],
            "fields": {k: tx["fields"][k] for k in C.TX_FIELDS},
            "documents": docs,
        })
    return truths


def generate(lists: dict, count: int, seed: int) -> list[dict]:
    gen = Generator(lists, seed)
    counts = case_counts(count)
    # 제재 대상이 들어가는 케이스를 먼저 만들어 서로 다른 항목을 고르게 한다
    order = [C.CASE_EXACT, C.CASE_FUZZY, C.CASE_NEW, C.CASE_VESSEL, C.CASE_CLEAN]
    order += [c for c in counts if c not in order]
    txs = [gen.make_tx(c) for c in order for _ in range(counts[c])]
    gen.rng.shuffle(txs)
    assign_times(gen.rng, txs)
    # 최종 확인 (invoice_date 를 채운 뒤 다시 한 번)
    for tx in txs:
        for v, want in tx["expected"].items():
            got = M.screen(tx["fields"], gen.idx[v])["classification"]
            if got != want:
                raise RuntimeError(f"{tx['tx_id']} ({tx['case_type']}): {v} screens {got}, expected {want}")
    return txs


# ---------------------------------------------------------------- 렌더링
TITLE_FONT, TITLE_SIZE = "Helvetica-Bold", 16
BODY_FONT, BODY_SIZE = "Helvetica", 11
FOOTER_FONT, FOOTER_SIZE = "Helvetica", 8
MARGIN = 72
LINE_GAP = 22
TITLE_TO_BODY = 40
FOOTER_TEXT = "Synthetic document - TradeProof demo"


def render_pdf(path: Path, doc: str, fields: dict) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
    c.setTitle(f"{C.DOC_TITLES[doc]} {fields.get('invoice_no') if doc == 'invoice' else fields.get('bl_no')}")
    c.setAuthor("TradeProof synthetic generator")
    y = h - MARGIN
    c.setFont(TITLE_FONT, TITLE_SIZE)
    c.drawString(MARGIN, y, C.DOC_TITLES[doc])
    c.setLineWidth(0.6)
    c.line(MARGIN, y - 10, w - MARGIN, y - 10)
    y -= TITLE_TO_BODY
    c.setFont(BODY_FONT, BODY_SIZE)
    for label, key in C.DOC_LABELS[doc]:
        c.drawString(MARGIN, y, f"{label}: {fields[key]}")
        y -= LINE_GAP
    c.setFont(FOOTER_FONT, FOOTER_SIZE)
    c.drawString(MARGIN, 36, FOOTER_TEXT)
    c.showPage()
    c.save()


def render_tx(args: tuple) -> tuple[str, float]:
    tx_dir, tx_id, fields = args
    t0 = time.perf_counter()
    tx_dir = Path(tx_dir)
    for d in C.DOC_TYPES:
        pdf = tx_dir / f"{d}.pdf"
        render_pdf(pdf, d, fields)
        subprocess.run(["pdftoppm", "-r", str(C.PNG_DPI), "-png", "-singlefile", str(pdf), str(tx_dir / d)],
                       check=True, capture_output=True)
    return tx_id, time.perf_counter() - t0


def check_line_widths(truths: list[dict]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    limit = A4[0] - 2 * MARGIN
    for t in truths:
        for d in C.DOC_TYPES:
            for label, key in C.DOC_LABELS[d]:
                text = f"{label}: {t['fields'][key]}"
                if stringWidth(text, BODY_FONT, BODY_SIZE) > limit:
                    print(f"WARNING {t['tx_id']} {d}: line wider than text area: {text!r}", file=sys.stderr)


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic trade transactions + invoice/B/L documents.")
    ap.add_argument("--lists-dir", type=Path, default=C.SANCTIONS_DIR)
    ap.add_argument("--out", type=Path, default=C.DOCS_DIR)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-render", action="store_true", help="write truth.json / manifest.json only")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args(argv)

    lists = load_lists(a.lists_dir)
    t0 = time.perf_counter()
    txs = generate(lists, a.count, a.seed)
    truths = build_truth(txs, a.out)
    t_gen = time.perf_counter() - t0

    a.out.mkdir(parents=True, exist_ok=True)
    for old in a.out.iterdir():   # 이전 실행의 거래 폴더 정리 (TX-#### 형식만)
        if old.is_dir() and re.fullmatch(r"TX-\d{4}", old.name):
            shutil.rmtree(old)
    for t in truths:
        d = a.out / t["tx_id"]
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "truth.json", "w", encoding="utf-8") as fh:
            json.dump(t, fh, ensure_ascii=False, indent=2)

    summary = {c: 0 for c in C.CASE_MIX}
    for t in truths:
        summary[t["case_type"]] += 1
    exp_summary = {v: {cls: sum(1 for t in truths if t["expected"][v] == cls) for cls in C.CLASSIFICATIONS}
                   for v in (C.LIST_BASE, C.LIST_NEW)}
    manifest = {
        "generated_by": "docs/gen_docs.py",
        "seed": a.seed,
        "count": len(truths),
        "list_versions": [lists[v]["list_version"] for v in (C.LIST_BASE, C.LIST_NEW)],
        "rules_version": M.RULES_VERSION,
        "rendered": not a.no_render,
        "summary": {"case_type": summary, "expected": exp_summary},
        "transactions": [{"tx_id": t["tx_id"], "case_type": t["case_type"], "expected": t["expected"],
                          "matched_entry_id": t["matched_entry_id"]} for t in truths],
    }
    with open(a.out / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"generated {len(truths)} transactions in {t_gen:.2f}s: {summary}")

    if not a.no_render:
        check_line_widths(truths)
        jobs = [(str(a.out / t["tx_id"]), t["tx_id"], t["fields"]) for t in truths]
        t1 = time.perf_counter()
        workers = max(1, a.workers)
        if workers == 1:
            times = [render_tx(j) for j in jobs]
        else:
            with Pool(workers) as pool:
                times = list(pool.imap_unordered(render_tx, jobs, chunksize=1))
        wall = time.perf_counter() - t1
        per = sum(s for _, s in times) / len(times)
        print(f"rendered {len(times)} transactions ({len(times) * len(C.DOC_TYPES)} PDFs + PNGs) "
              f"in {wall:.1f}s wall with {workers} worker(s); {per:.2f}s per transaction (single worker)")
    print(f"output: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
