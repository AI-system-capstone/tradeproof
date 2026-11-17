"""Generate disposable import samples; no package or signing key is committed."""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    (root / "packages").mkdir()
    (root / "raw_samples").mkdir()
    key = Ed25519PrivateKey.generate()
    (root / "trusted.pub").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    def package(name, kind, item_id, payload, metadata, mutation=None):
        manifest = dict(schema_version="1.0", package_type=kind, item_id=item_id,
                        publisher="DEMO_VENDOR_NOT_REAL", issued_at="2026-01-01T00:00:00+00:00",
                        files=[dict(path=p, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                               for p, raw in payload.items()], **metadata)
        raw = json.dumps(manifest).encode()
        files = {**payload, "manifest.json": raw, "manifest.sig": base64.b64encode(key.sign(raw))}
        if mutation:
            mutation(files)
        with zipfile.ZipFile(root / "packages" / name, "x", zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(path, content)

    rng = np.random.default_rng(42)
    weights = io.BytesIO()
    np.savez(weights, encoder=rng.normal(size=(16, 16)).astype("float32"),
             decoder=rng.normal(size=(16, 8)).astype("float32"))
    for name in ("model-v3", "model-v4", "model-v4-incompatible", "model-v4-bad-signature"):
        item_id = "model-v3" if name == "model-v3" else "model-v4"
        config = dict(format="MOCK_NPZ", model_id=item_id, runtime="mock-ocr-v1",
                      service_compatibility=["2.0" if name.endswith("incompatible") else "1.0"],
                      weights_schema={"encoder": [16, 16], "decoder": [16, 8]})
        mutation = (lambda f: f.update({"manifest.sig": base64.b64encode(bytes(64))})) if name.endswith("bad-signature") else None
        package(name + ".zip", "ocr_model", item_id,
                {"payload/model_config.json": json.dumps(config).encode(), "payload/weights.npz": weights.getvalue()},
                dict(model_format="MOCK_NPZ", runtime="mock-ocr-v1", minimum_service_version="1.0"), mutation)

    columns = ["sanction_id", "entity_name", "entity_type", "country_code", "aliases", "source", "listed_on", "last_updated"]
    for name, count in (("list-v14", 20), ("list-v15", 21), ("list-v16-large-change", 30),
                        ("list-v15-tampered", 21), ("list-v15-bad-schema", 21)):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(["invalid", *columns[1:]] if name.endswith("bad-schema") else columns)
        for i in range(1, count + 1):
            writer.writerow([f"SYN-{i:04}", f"DEMO_ENTITY_{i:04}", "ORGANIZATION", "ZZ",
                             f"DEMO_ALIAS_{i:04}", "SYNTHETIC_TEST", "2026-01-01", "2026-01-01"])
        raw = text.getvalue().encode()
        if name in ("list-v14", "list-v15"):
            (root / "raw_samples" / (name + ".csv")).write_bytes(raw)
        mutation = (lambda f: f.update({"payload/sanctions.csv": f["payload/sanctions.csv"] + b"tampered"})) if name.endswith("tampered") else None
        item_id = "list-v16" if "v16" in name else "list-v14" if name == "list-v14" else "list-v15"
        package(name + ".zip", "sanctions_list", item_id, {"payload/sanctions.csv": raw},
                dict(list_source="SYNTHETIC_TEST", record_count=count, test_only=True), mutation)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "fixtures")
    print(generate(parser.parse_args().output))
