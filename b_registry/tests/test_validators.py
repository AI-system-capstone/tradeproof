import io
import json
import stat
import subprocess
import sys
import zipfile
import numpy as np
import pytest
from b_registry.verify.validation import ValidationFailure, safe_zip, validate, parse_synthetic_csv
from b_registry.signer.package_release import package_release
from .conftest import archive

@pytest.mark.parametrize("name", ["../x", "/absolute", "C:/x", "payload\\evil", "payload/../x", "./x"])
def test_unsafe_paths(cfg, name):
    raw = archive([(name.replace("\\", "~"), b"x")]).replace(b"~", b"\\")
    with pytest.raises(ValidationFailure) as error:
        safe_zip(raw, cfg)
    assert error.value.code == "ZIP_PATH"

def test_duplicate_link_and_expansion(cfg):
    with pytest.warns(UserWarning):
        raw = archive([("same", b"a"), ("same", b"b")])
    with pytest.raises(ValidationFailure, match="Duplicate"):
        safe_zip(raw, cfg)
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValidationFailure, match="Links"):
        safe_zip(archive([(link, b"/etc/passwd")]), cfg)
    with pytest.raises(ValidationFailure, match="expansion"):
        safe_zip(archive([("bomb", b"0" * 100000)]), cfg)

def test_size_count_limits(cfg):
    cfg.max_file_bytes = 2
    with pytest.raises(ValidationFailure):
        safe_zip(archive([("file", b"123")]), cfg)
    cfg.max_files = 1
    with pytest.raises(ValidationFailure):
        safe_zip(archive([("a", b"1"), ("b", b"2")]), cfg)
    cfg.max_zip_bytes = 2
    assert validate(b"123", cfg).error.code == "ZIP_SIZE_LIMIT"

@pytest.mark.parametrize("name, code", [
    ("model-v3.zip", None), ("model-v4.zip", None), ("list-v14.zip", None), ("list-v15.zip", None),
    ("model-v4-bad-signature.zip", "SIGNATURE_INVALID"),
    ("model-v4-incompatible.zip", "MODEL_INCOMPATIBLE"),
    ("list-v15-tampered.zip", "PAYLOAD_HASH_MISMATCH"), ("list-v15-bad-schema.zip", "CSV_SCHEMA"),
])
def test_supplied_packages(cfg, name, code, fixtures):
    report = validate((fixtures / "packages" / name).read_bytes(), cfg)
    assert (report.error.code if report.error else None) == code

def test_signature_names_and_raw_bytes(cfg, fixtures):
    with zipfile.ZipFile(fixtures / "packages/model-v3.zip") as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["signature.sig"] = files.pop("manifest.sig")
    assert validate(archive(files.items()), cfg).error is None
    files["manifest.sig"] = files["signature.sig"]
    assert validate(archive(files.items()), cfg).error.code == "MANIFEST_MISSING"
    del files["manifest.sig"]
    files["manifest.json"] += b"\n"
    assert validate(archive(files.items()), cfg).error.code == "SIGNATURE_INVALID"

def test_untrusted_embedded_key_and_manifest(cfg, signer):
    _, mutate = signer
    raw = mutate("model-v4.zip", lambda f, m: f.update({"public_key.pem": b"attacker"}))
    assert validate(raw, cfg).error.code == "UNDECLARED_FILE"
    assert validate(mutate("model-v4.zip", lambda f, m: m.update(item_id="../evil")), cfg).error.code == "MANIFEST_SCHEMA"

@pytest.mark.parametrize("kind", ["nan", "dtype", "shape", "object", "extra"])
def test_npz_invalid_arrays(cfg, signer, kind):
    def change(files, manifest):
        weights = {"encoder": np.ones((16, 16), dtype="float32"), "decoder": np.ones((16, 8), dtype="float32")}
        if kind == "nan": weights["encoder"][0, 0] = np.nan
        if kind == "dtype": weights["encoder"] = weights["encoder"].astype("float64")
        if kind == "shape": weights["encoder"] = np.ones((8, 8), dtype="float32")
        if kind == "object": weights["encoder"] = np.array([{"unsafe": True}], dtype=object)
        if kind == "extra": weights["extra"] = np.ones(1)
        buf = io.BytesIO()
        np.savez(buf, **weights)
        files["payload/weights.npz"] = buf.getvalue()
    report = validate(signer[1]("model-v4.zip", change), cfg)
    assert report.error.code in {"WEIGHTS_INVALID", "WEIGHTS_NAMES"}

@pytest.mark.parametrize("change", [
    lambda rows: rows.append(rows[1]),
    lambda rows: rows.__setitem__(1, rows[1].replace("ORGANIZATION", "UNKNOWN")),
    lambda rows: rows.__setitem__(1, rows[1].replace("2026-01-01", "2026-02-30")),
    lambda rows: rows.__setitem__(1, rows[1] + ",extra"),
])
def test_csv_invalid(change, fixtures):
    rows = (fixtures / "raw_samples/list-v14.csv").read_text().splitlines()
    change(rows)
    with pytest.raises(ValidationFailure):
        parse_synthetic_csv("\n".join(rows).encode(), 20)

def test_signer_cli_and_validator(cfg, signer, tmp_path, fixtures):
    output = tmp_path / "release.zip"
    result = subprocess.run([sys.executable, "-m", "b_registry.signer.package_release",
        "--type", "sanctions_list", "--item-id", "list-v15",
        "--source", str(fixtures / "raw_samples/list-v15.csv"), "--output", str(output),
        "--private-key", str(signer[0])], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, "-m", "b_registry.cli", "validate", str(output),
        "--trust-key", str(cfg.trust_key)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "APPROVED", "reason": None}
    with zipfile.ZipFile(output) as z:
        assert "signature.sig" in z.namelist()
        assert not any("key" in n for n in z.namelist())
    for command in ("tamper", "corrupt"):
        bad = tmp_path / (command + ".zip")
        result = subprocess.run([sys.executable, "-m", "b_registry.signer", command, str(output), str(bad)], capture_output=True)
        assert result.returncode == 0
        assert validate(bad.read_bytes(), cfg).error

def test_tesseract_and_tradeproof_json(cfg, signer, tmp_path):
    folder = tmp_path / "model"
    folder.mkdir()
    config = {"engine": "tesseract", "lang": "eng", "config": "--oem 1 --psm 6", "preprocess": {"scale": 0.7, "resample": "bilinear"}}
    (folder / "model_config.json").write_text(json.dumps(config))
    output = tmp_path / "model.zip"
    package_release("ocr_model", "model-v3", folder, output, signer[0])
    report = validate(output.read_bytes(), cfg)
    assert report.error is None
    assert report.steps[-1]["details"]["inference_performed"] is False
    config["config"] += " --tessdata-dir /untrusted"
    (folder / "model_config.json").write_text(json.dumps(config))
    with pytest.raises(ValidationFailure):
        package_release("ocr_model", "model-v3", folder, tmp_path / "bad.zip", signer[0])
    from common import contracts as C
    source = tmp_path / "list.json"
    data = {"list_version": "list-v14", "source": "demo", "source_fetched_at": "2026-11-12",
            "created_at": "2026-11-12T00:00:00+00:00", "entry_count": 1, "entries": [C.NEW_LISTING]}
    source.write_text(json.dumps(data))
    output = tmp_path / "list.zip"
    package_release("sanctions_list", "list-v14", source, output, signer[0])
    assert validate(output.read_bytes(), cfg).rows == {C.NEW_LISTING["entry_id"]: C.NEW_LISTING}
    data["entries"].append(C.NEW_LISTING)
    data["entry_count"] = 2
    source.write_text(json.dumps(data))
    with pytest.raises(ValidationFailure):
        package_release("sanctions_list", "list-v14", source, tmp_path / "bad-list.zip", signer[0])
