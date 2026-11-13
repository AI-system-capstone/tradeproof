"""서류 이미지(PNG) -> OCR -> 필드 추출 -> 거래별 extracted.json.

AI(OCR 엔진)는 run_ocr() 한 곳에서만 부른다. 그 뒤(줄 묶기, 라벨 매칭, 병합)는 전부 일반 코드라서
엔진을 PaddleOCR 등으로 바꿔도 run_ocr()만 같은 모양(image_to_data DICT)으로 돌려주면 된다.

출력 (contracts.OCR_DIR 기준, model-v3가 아닌 모델은 하위 폴더 <model>/):
  <tx_id>/<doc>.raw.json   OCR 원본 (image_to_data + 엔진/이미지 정보)
  <tx_id>/extracted.json   {tx_id, model_version, ocr_engine, extracted_at,
                            fields:{field:{value, source_doc, bbox, confidence}}, conflicts:[...]}
  bbox는 원본 PNG 픽셀 좌표 [x0, y0, x1, y1] (열화 모델이 축소해서 읽어도 원본 좌표로 되돌려 저장).

사용:
  python ocr/extract.py                                  # data/docs 전체, model-v3 -> data/ocr/
  python ocr/extract.py --model model-v4                 # -> data/ocr/model-v4/
  python ocr/extract.py --docs-dir /tmp/ocr_sample --out-dir /tmp/ocr_out --tx TX-9001 TX-9002
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zlib
from datetime import datetime, timezone
from difflib import SequenceMatcher
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pytesseract
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402

DEFAULT_MODEL = C.MODEL_BASE

# ---------------------------------------------------------------- 모델 버전 -> OCR 설정
# 새 버전은 여기에 한 줄 추가하면 된다. preprocess는 OCR 직전에 이미지에 적용하는 전처리/열화.
#   psm 6 (단일 텍스트 블록): 왼쪽 정렬된 줄 목록인 이 서류에서 psm 3/4와 정확도가 같고 조금 더 빠르다.
#   scale 0.7 (200dpi -> 약 140dpi): 원본 해상도 그대로면 Helvetica의 '11'을 '1 1'로 끊어 읽는 일이 잦다.
#     합성 155건 기준 필드 정확도 1.0배 98.5% -> 0.55~0.8배 구간 99.8~99.9%. 그 구간 가운데인 0.7을 쓴다.
MODELS: dict[str, dict] = {
    C.MODEL_BASE: {
        "engine": "tesseract", "lang": "eng", "config": "--oem 1 --psm 6",
        "preprocess": {"scale": 0.7, "resample": "bilinear"},
    },
    # 롤백 시연용으로 일부러 나쁘게 만든 버전: 저해상도 스캔 + 흐림 + 잡음 흉내
    C.MODEL_NEW: {
        "engine": "tesseract", "lang": "eng", "config": "--oem 1 --psm 6",
        # 합성 서류 기준 필드 정확도 약 65~70%, 당사자 이름 약 20%가 틀리게 나오도록 맞춘 값
        "preprocess": {"scale": 0.34, "blur": 0.4, "noise_std": 10.0, "salt_pepper": 0.002},
    },
}

LABEL_THRESHOLD = 0.8          # 라벨 퍼지 매칭 기준 (':' 앞부분 vs 라벨)
NOCOLON_THRESHOLD = 0.85       # ':'가 없을 때 앞 단어들 vs 라벨 (조금 더 엄격)
# 2차(완화) 매칭: 1차에서 못 찾은 필드에 한해, ':'가 있는 줄의 앞부분이 한 라벨에만 뚜렷하게 가까우면 인정
# (예: 'Mossel:' -> Vessel, 'Bi No:' -> B/L No). 라벨은 서류마다 한 번씩만 나온다는 약속을 이용한다.
RELAXED_THRESHOLD = 0.65
RELAXED_MARGIN = 0.1
COLON_CHARS = ":;"             # OCR이 ':'를 ';'로 읽는 경우가 있다

# 병합 우선순위: bl_no·항구는 B/L, 나머지(당사자/선박/품목/금액/통화/날짜)는 Invoice
BL_PREFERRED = {"bl_no", "port_of_loading", "port_of_discharge"}


def field_preference(field: str) -> list[str]:
    return ["bl", "invoice"] if field in BL_PREFERRED else ["invoice", "bl"]


def out_dir_for(base: Path | str | None, model_version: str) -> Path:
    """model-v3는 base 그대로, 다른 모델은 base/<model>/ (모델끼리 결과가 섞이지 않게)."""
    return C.ocr_dir(model_version, base)


def norm_value(s: str | None) -> str:
    """비교용 가벼운 정규화: 대문자, 공백 하나로, 끝 문장부호 제거. (evaluate.py도 같은 함수를 쓴다)"""
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s).upper()).strip()
    return re.sub(r"[\s.,;:]+$", "", s)


# ---------------------------------------------------------------- OCR 엔진 (유일한 호출 지점)
def _preprocess(img: Image.Image, spec: dict, seed: int) -> Image.Image:
    img = img.convert("L")
    scale = spec.get("scale", 1.0)
    if scale != 1.0:
        resample = getattr(Image.Resampling, spec.get("resample", "BILINEAR").upper())
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), resample)
    if spec.get("blur"):
        img = img.filter(ImageFilter.GaussianBlur(spec["blur"]))
    rng = np.random.default_rng(seed)
    a = np.asarray(img, dtype=np.float32)
    if spec.get("noise_std"):
        a = a + rng.normal(0.0, spec["noise_std"], a.shape)
    if spec.get("salt_pepper"):
        m = rng.random(a.shape)
        p = spec["salt_pepper"]
        a[m < p / 2] = 0
        a[m > 1 - p / 2] = 255
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def engine_name() -> str:
    return f"tesseract-{pytesseract.get_tesseract_version()}"


def run_ocr(image_path: str | Path, model_version: str = DEFAULT_MODEL) -> dict:
    """이미지 한 장을 OCR한다. 반환 = image_to_data(DICT) + 엔진/이미지/모델 정보.

    좌표(left/top/width/height)는 항상 원본 이미지 픽셀 기준이다.
    엔진을 바꿀 때는 이 함수만 같은 모양으로 돌려주면 된다.
    """
    if model_version not in MODELS:
        raise ValueError(f"unknown model_version {model_version!r}; known: {sorted(MODELS)}")
    spec = MODELS[model_version]
    image_path = Path(image_path)
    raw_bytes = image_path.read_bytes()
    img = Image.open(io.BytesIO(raw_bytes))
    orig_size = img.size
    work = img.convert("L")
    pre = spec.get("preprocess")
    if pre:
        # 잡음 시드는 이미지 내용으로 정한다 -> 같은 서류는 몇 번을 돌려도 같은 결과 (재현성)
        work = _preprocess(work, pre, seed=zlib.crc32(raw_bytes))
    t0 = time.perf_counter()
    data = pytesseract.image_to_data(work, lang=spec["lang"], config=spec["config"],
                                     output_type=pytesseract.Output.DICT)
    elapsed = time.perf_counter() - t0
    sx, sy = orig_size[0] / work.width, orig_size[1] / work.height
    if (sx, sy) != (1.0, 1.0):
        for k, s in (("left", sx), ("width", sx), ("top", sy), ("height", sy)):
            data[k] = [round(v * s) for v in data[k]]
    data["conf"] = [float(c) for c in data["conf"]]
    return {
        "engine": spec["engine"],
        "engine_version": str(pytesseract.get_tesseract_version()),
        "engine_config": {"lang": spec["lang"], "config": spec["config"], "preprocess": pre},
        "model_version": model_version,
        "image": _rel_data(image_path),
        "image_size": list(orig_size),
        "ocr_image_size": [work.width, work.height],
        "ocr_seconds": round(elapsed, 3),
        "data": data,
    }


# ---------------------------------------------------------------- 줄 묶기
def group_lines(raw: dict) -> list[dict]:
    """단어를 (block, par, line)으로 묶는다. 각 줄 = {text, words:[{text,bbox,conf}], bbox, confidence}."""
    d = raw["data"]
    lines: dict[tuple, list] = {}
    for i, txt in enumerate(d["text"]):
        if d["level"][i] != 5 or not str(txt).strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        lines.setdefault(key, []).append(
            {"text": str(txt).strip(), "bbox": [x, y, x + w, y + h], "conf": d["conf"][i]})
    out = []
    for key in sorted(lines):
        words = lines[key]
        out.append({"key": list(key), "text": " ".join(w["text"] for w in words), "words": words,
                    "bbox": union_bbox(words), "confidence": mean_conf(words)})
    out.sort(key=lambda ln: (ln["bbox"][1], ln["bbox"][0]))
    return out


def union_bbox(words: list[dict]) -> list[int] | None:
    if not words:
        return None
    return [min(w["bbox"][0] for w in words), min(w["bbox"][1] for w in words),
            max(w["bbox"][2] for w in words), max(w["bbox"][3] for w in words)]


def mean_conf(words: list[dict]) -> float | None:
    cs = [w["conf"] for w in words if w["conf"] is not None and w["conf"] >= 0]
    return round(sum(cs) / len(cs), 2) if cs else None


# ---------------------------------------------------------------- 라벨 매칭 / 필드 파싱
def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _label_score(text: str, label: str) -> float:
    a, b = _norm_label(text), _norm_label(label)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _labels_longest_first(doc_type: str) -> list[tuple[str, str]]:
    return sorted(C.DOC_LABELS[doc_type], key=lambda lf: (-len(lf[0].split()), -len(lf[0])))


def _split_colon(words: list[dict]) -> tuple[str, list[dict]] | None:
    """첫 콜론 기준으로 (앞부분 텍스트, 값 단어들)을 나눈다. 콜론이 없으면 None."""
    for wi, w in enumerate(words):
        pos = next((i for i, ch in enumerate(w["text"]) if ch in COLON_CHARS), -1)
        if pos < 0:
            continue
        head = " ".join([x["text"] for x in words[:wi]] + [w["text"][:pos]])
        rest = w["text"][pos + 1:].strip()
        return head, ([{**w, "text": rest}] if rest else []) + words[wi + 1:]
    return None


def match_line_relaxed(line: dict, doc_type: str, missing: set[str]) -> dict | None:
    """2차 매칭: 아직 못 찾은 필드(missing)의 라벨 중 하나에만 뚜렷하게 가까운 콜론 줄."""
    sp = _split_colon(line["words"])
    if not sp:
        return None
    head, value_words = sp
    scored = sorted(((_label_score(head, lb), lb, f) for lb, f in C.DOC_LABELS[doc_type]), reverse=True)
    (s1, lb, f), s2 = scored[0], (scored[1][0] if len(scored) > 1 else 0.0)
    if f in missing and s1 >= RELAXED_THRESHOLD and s1 - s2 >= RELAXED_MARGIN:
        return {"field": f, "label": lb, "label_score": round(s1, 3), "value_words": value_words,
                "relaxed": True}
    return None


def match_line(line: dict, doc_type: str) -> dict | None:
    """줄 하나를 라벨에 맞춘다. 반환 {field, label, label_score, value_words} 또는 None."""
    words = line["words"]
    labels = _labels_longest_first(doc_type)

    # 1) ':'(또는 ';')가 있으면 첫 콜론 앞부분을 라벨 후보로 본다
    sp = _split_colon(words)
    if sp:
        head, value_words = sp
        best = None
        for label, field in labels:   # 길이순이라 동점이면 긴 라벨이 먼저 남는다
            sc = _label_score(head, label)
            if sc >= LABEL_THRESHOLD and (best is None or sc > best[2]):
                best = (label, field, sc)
        if best:
            return {"field": best[1], "label": best[0], "label_score": round(best[2], 3),
                    "value_words": value_words}
        # 첫 콜론 앞이 라벨이 아니면 콜론 없는 경우로 넘어간다

    # 2) 콜론이 없거나 깨진 경우: 앞 k단어(k = 라벨 단어 수)를 라벨과 비교, 긴 라벨부터
    for label, field in labels:
        k = len(label.split())
        if len(words) <= k:
            continue
        head = " ".join(x["text"] for x in words[:k])
        sc = _label_score(head, label)
        if sc >= NOCOLON_THRESHOLD:
            return {"field": field, "label": label, "label_score": round(sc, 3),
                    "value_words": words[k:]}
    return None


def _candidate(m: dict, line: dict) -> dict:
    vw = m["value_words"]
    value = re.sub(r"\s+", " ", " ".join(w["text"] for w in vw)).strip() or None
    return {"value": value, "bbox": union_bbox(vw), "confidence": mean_conf(vw),
            "label_score": m["label_score"], "relaxed_label": m.get("relaxed", False),
            "line_text": line["text"]}


def parse_document(raw: dict, doc_type: str) -> dict:
    """OCR 원본 -> {doc_type, fields:{field:{value,bbox,confidence,label_score,line_text}}, unmatched_lines}"""
    fields: dict[str, dict] = {}
    unmatched = []
    for line in group_lines(raw):
        m = match_line(line, doc_type)
        if not m:
            unmatched.append(line)
            continue
        cand = _candidate(m, line)
        prev = fields.get(m["field"])
        # 같은 라벨이 두 번 잡히면 라벨이 더 확실한 줄을 쓴다
        if prev is None or (prev["value"] is None and cand["value"]) or cand["label_score"] > prev["label_score"]:
            fields[m["field"]] = cand
    # 2차: 못 찾은 필드만 완화된 기준으로 (먼저 나온 줄 우선)
    still = []
    for line in unmatched:
        missing = {f for _, f in C.DOC_LABELS[doc_type] if f not in fields}
        m = match_line_relaxed(line, doc_type, missing) if missing else None
        if m:
            fields[m["field"]] = _candidate(m, line)
        else:
            still.append(line["text"])
    return {"doc_type": doc_type, "fields": fields, "unmatched_lines": still}


# ---------------------------------------------------------------- 거래 단위 병합
def merge_documents(tx_id: str, parsed: dict[str, dict], model_version: str, ocr_engine: str) -> dict:
    fields = {}
    for f in C.TX_FIELDS:
        chosen = None
        for doc in field_preference(f):
            v = parsed.get(doc, {}).get("fields", {}).get(f)
            if v and v["value"]:
                chosen = {"value": v["value"], "source_doc": doc, "bbox": v["bbox"],
                          "confidence": v["confidence"]}
                break
        fields[f] = chosen or {"value": None, "source_doc": None, "bbox": None, "confidence": None}

    conflicts = []
    shared = [f for f in C.TX_FIELDS
              if all(f in {fld for _, fld in C.DOC_LABELS[d]} for d in ("invoice", "bl"))]
    for f in shared:
        iv = parsed.get("invoice", {}).get("fields", {}).get(f, {}).get("value")
        bv = parsed.get("bl", {}).get("fields", {}).get(f, {}).get("value")
        if iv and bv and norm_value(iv) != norm_value(bv):
            conflicts.append({"field": f, "invoice": iv, "bl": bv})
        elif bool(iv) != bool(bv) and (iv or bv):
            conflicts.append({"field": f, "invoice": iv, "bl": bv})   # 한쪽에서만 읽힘

    return {
        "tx_id": tx_id,
        "model_version": model_version,
        "ocr_engine": ocr_engine,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": fields,
        "conflicts": conflicts,
        "documents": {d: {"image": p.get("image"), "image_size": p.get("image_size"),
                          "unmatched_lines": p.get("unmatched_lines", [])}
                      for d, p in parsed.items()},
    }


def _extract(tx_dir: Path, model_version: str) -> tuple[dict, dict]:
    tx_dir = Path(tx_dir)
    raws, parsed = {}, {}
    for doc in C.DOC_TYPES:
        png = tx_dir / f"{doc}.png"
        if not png.exists():
            continue
        raw = run_ocr(png, model_version)
        raws[doc] = raw
        p = parse_document(raw, doc)
        p["image"], p["image_size"] = _rel_data(png), raw["image_size"]
        parsed[doc] = p
    if not raws:
        raise FileNotFoundError(f"no document images in {tx_dir}")
    engine = next(iter(raws.values()))
    result = merge_documents(tx_dir.name, parsed, model_version,
                             f"{engine['engine']}-{engine['engine_version']}")
    result["missing_docs"] = [d for d in C.DOC_TYPES if d not in raws]
    result["ocr_seconds"] = {d: r["ocr_seconds"] for d, r in raws.items()}
    return result, raws


def _rel_data(p: Path) -> str:
    """data/ 아래면 DATA 기준 상대경로(truth.json과 같은 규칙), 아니면 절대경로."""
    p = Path(p).resolve()
    try:
        return str(p.relative_to(C.DATA.resolve()))
    except ValueError:
        return str(p)


def extract_transaction(tx_dir: str | Path, model_version: str = DEFAULT_MODEL,
                        out_dir: str | Path | None = None) -> dict:
    """거래 폴더(<tx_id>/{invoice,bl}.png) 하나를 처리해 extracted dict를 돌려준다.

    out_dir를 주면 out_dir/<tx_id>/extracted.json 과 <doc>.raw.json 도 저장한다. (향후 POST /screenings용)
    """
    result, raws = _extract(Path(tx_dir), model_version)
    if out_dir is not None:
        d = Path(out_dir) / result["tx_id"]
        d.mkdir(parents=True, exist_ok=True)
        for doc, raw in raws.items():
            (d / f"{doc}.raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        (d / "extracted.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------------------------------------------------------- CLI
def _init_worker() -> None:
    os.environ["OMP_THREAD_LIMIT"] = "1"   # 프로세스 여러 개일 때 tesseract 스레드 과다 방지


def _job(args: tuple) -> tuple[str, dict | None, str | None, float]:
    tx_dir, model, out = args
    t0 = time.perf_counter()
    try:
        r = extract_transaction(tx_dir, model, out)
        return Path(tx_dir).name, {"n_found": sum(1 for v in r["fields"].values() if v["value"]),
                                   "n_conflicts": len(r["conflicts"]),
                                   "pages": len(r["ocr_seconds"])}, None, time.perf_counter() - t0
    except Exception as e:  # 한 건 실패가 전체를 멈추지 않게
        return Path(tx_dir).name, None, f"{type(e).__name__}: {e}", time.perf_counter() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OCR + field extraction per transaction")
    ap.add_argument("--docs-dir", type=Path, default=C.DOCS_DIR)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help=f"base output dir (default {C.OCR_DIR}); non-default models go to <out-dir>/<model>/")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(MODELS))
    ap.add_argument("--tx", nargs="*", help="subset of tx_ids (default: all)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args(argv)

    out = out_dir_for(a.out_dir, a.model)
    if a.tx:
        tx_dirs = [a.docs_dir / t for t in a.tx]
    else:
        tx_dirs = sorted(p for p in a.docs_dir.iterdir()
                         if p.is_dir() and any((p / f"{d}.png").exists() for d in C.DOC_TYPES))
    if not tx_dirs:
        print(f"no transactions found in {a.docs_dir}", file=sys.stderr)
        return 1
    jobs = [(str(t), a.model, str(out)) for t in tx_dirs]
    t0 = time.perf_counter()
    ok, failed, pages = 0, [], 0
    workers = max(1, min(a.workers, len(jobs)))
    if workers == 1:
        results = map(_job, jobs)
        pool = None
    else:
        pool = Pool(workers, initializer=_init_worker)
        results = pool.imap_unordered(_job, jobs)
    for tx, info, err, _dt in results:
        if err:
            failed.append((tx, err))
            print(f"  FAIL {tx}: {err}", file=sys.stderr)
        else:
            ok += 1
            pages += info["pages"]
    if pool:
        pool.close()
        pool.join()
    wall = time.perf_counter() - t0
    print(f"[{a.model}] {ok} tx ok, {len(failed)} failed, {pages} pages in {wall:.1f}s "
          f"({wall / max(pages, 1):.2f}s/page wall, workers={workers}) -> {out}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
