"""Vendor utilities. Never mount a signing private key in the Registry container."""
import argparse
import json
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from b_registry.config import Settings
from b_registry.verify.validation import safe_zip

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keygen")
    keys.add_argument("--private-key", type=Path, required=True)
    keys.add_argument("--public-key", type=Path, required=True)
    for name in ("tamper", "corrupt"):
        p = sub.add_parser(name)
        p.add_argument("source", type=Path)
        p.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "keygen":
        if args.private_key.exists() or args.public_key.exists():
            parser.error("Key paths must not exist")
        key = Ed25519PrivateKey.generate()
        with args.private_key.open("xb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with args.public_key.open("xb") as f:
            f.write(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        args.private_key.chmod(0o600)
    else:
        cfg = Settings()
        with args.source.open("rb") as f:
            raw = f.read(cfg.max_zip_bytes + 1)
        files = safe_zip(raw, cfg)
        if args.command == "corrupt":
            with args.output.open("xb") as f:
                f.write(b"CORRUPTED-ZIP")
        else:
            manifest = json.loads(files["manifest.json"])
            name = manifest["files"][0]["path"]
            files[name] += b"tampered"
            with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED) as z:
                for name, data in files.items():
                    z.writestr(name, data)

if __name__ == "__main__":
    main()
