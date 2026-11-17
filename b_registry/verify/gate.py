"""Validate C result integrity before evaluating B's gate; no C implementation imports."""
import math
from common import contracts as C


def policy():
    return {"min_accuracy": C.REGISTRY_GATE_MIN_ACCURACY, "require_complete_replay": True}


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {name}")
    return value


def count(value, name, positive=False):
    value = number(value, name)
    if value != int(value) or value < (1 if positive else 0):
        raise ValueError(f"Invalid {name}")
    return value


def evaluate(result, snapshot):
    golden, replay = result["golden"], result["replay"]
    if result.get("error") is not None or not golden.get("golden_version"):
        raise ValueError("Missing golden dataset identity or nonempty execution error")
    total = count(golden["total"], "golden.total", positive=True)
    for name in ("accuracy", "false_negative", "false_positive"):
        metric = golden[name]
        baseline = number(metric["baseline"], name)
        candidate = number(metric["candidate"], name)
        delta = number(metric["diff"], name)
        maximum = 1 if name == "accuracy" else total
        if not (0 <= baseline <= maximum and 0 <= candidate <= maximum):
            raise ValueError(f"Out of range {name}")
        if name != "accuracy":
            count(baseline, name)
            count(candidate, name)
        if not math.isclose(candidate - baseline, delta, abs_tol=1e-8):
            raise ValueError(f"Inconsistent {name}.diff")
    total = count(replay["total"], "replay.total", positive=True)
    processed = count(replay["processed"], "replay.processed")
    changed = count(replay["changed"], "replay.changed")
    down = count(replay["changed_down"], "replay.changed_down")
    if not (down <= changed <= processed <= total) or type(replay["count_match"]) is not bool:
        raise ValueError("Inconsistent replay counts")
    reasons = []
    if golden["accuracy"]["candidate"] < snapshot["min_accuracy"]:
        reasons.append("Golden accuracy is below the 90% gate")
    if processed != total or not replay["count_match"]:
        reasons.append("Replay did not process all target transactions")
    return {"decision": C.REGISTRY_GATE_BLOCKED if reasons else C.REGISTRY_GATE_PASSED,
            "reasons": reasons, "policy": snapshot,
            "observed": {"accuracy":golden["accuracy"]["candidate"],
                         "false_negative":golden["false_negative"], "false_positive":golden["false_positive"],
                         "replay_total":total,"replay_processed":processed}}


def has_passed_validation(bundle):
    v = bundle.validation or {}
    return (bundle.validation_id is not None and v.get("validation_id") == bundle.validation_id
            and v.get("status") == C.REGISTRY_VALIDATION_COMPLETED
            and v.get("gate", {}).get("decision") == C.REGISTRY_GATE_PASSED
            and v.get("model_id") == bundle.model_id and v.get("list_id") == bundle.list_id
            and v.get("baseline_bundle_id") == bundle.base_bundle_id)
