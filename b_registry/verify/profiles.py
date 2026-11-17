"""Profiles matching A's data shapes, without importing A's implementation."""
import math
from datetime import date, datetime
from common import contracts as C
from b_registry.verify.validation import fail, unique_json

def validate_tesseract(raw, manifest):
    try:
        value = unique_json(raw)
        if set(value) != {"engine", "lang", "config", "preprocess"}:
            raise ValueError()
        if (value["engine"], value["lang"], value["config"]) != ("tesseract", "eng", "--oem 1 --psm 6"):
            raise ValueError()
        if manifest.runtime != "tesseract" or manifest.minimum_service_version != "1.0":
            raise ValueError()
        options = value["preprocess"]
        limits = {"scale": (0.1, 4), "blur": (0, 5), "noise_std": (0, 50), "salt_pepper": (0, 0.1)}
        if not isinstance(options, dict) or set(options) - {*limits, "resample"}:
            raise ValueError()
        for name, number in options.items():
            if name == "resample":
                if number not in ("bilinear", "bicubic", "lanczos", "nearest"):
                    raise ValueError()
            elif type(number) not in (float, int) or not math.isfinite(number) or not limits[name][0] <= number <= limits[name][1]:
                raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        fail("content", "MODEL_CONFIG", "Unsupported Tesseract configuration")
    return {"profile": "TESSERACT_CONFIG", "inference_performed": False}

def parse_tradeproof_list(raw, manifest):
    try:
        value = unique_json(raw)
        if set(value) != {"list_version", "source", "source_fetched_at", "created_at", "entry_count", "entries"}:
            raise ValueError()
        if value["list_version"] != manifest.item_id or not isinstance(value["source"], str) or not value["source"].strip():
            raise ValueError()
        date.fromisoformat(value["source_fetched_at"])
        if datetime.fromisoformat(value["created_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
        entries = value["entries"]
        if not isinstance(entries, list) or type(value["entry_count"]) is not int or value["entry_count"] != len(entries) or len(entries) != manifest.record_count:
            raise ValueError()
        result = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != set(C.ENTRY_FIELDS):
                raise ValueError()
            if any(not isinstance(entry[k], str) or not entry[k].strip() for k in ("entry_id", "name")):
                raise ValueError()
            if entry["entry_id"] in result or entry["entity_type"] not in (C.ENTITY, C.VESSEL) or type(entry["synthetic"]) is not bool:
                raise ValueError()
            for k in ("aliases", "programs"):
                if not isinstance(entry[k], list) or any(not isinstance(v, str) or not v.strip() for v in entry[k]):
                    raise ValueError()
            for k in ("country", "call_sign", "vessel_flag"):
                if entry[k] is not None and not isinstance(entry[k], str):
                    raise ValueError()
            result[entry["entry_id"]] = entry
    except (ValueError, KeyError, TypeError, UnicodeError):
        fail("content", "LIST_SCHEMA", "List does not match common.contracts ENTRY_FIELDS")
    return result
