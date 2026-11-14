"""Vendor-only release packager. No private key is ever included in output."""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import zipfile
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from b_registry.verify.schemas import Manifest


def package_release(kind, item_id, source, output, private_key):
    source, output = Path(source), Path(output)
    key = load_pem_private_key(Path(private_key).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('Ed25519 private key required')
    manifest = {'schema_version': '1.0', 'package_type': kind, 'item_id': item_id,
                'publisher': 'DEMO_VENDOR_NOT_REAL', 'issued_at': datetime.now(timezone.utc).isoformat()}
    if kind == 'ocr_model':
        files = {'payload/model_config.json': (source / 'model_config.json').read_bytes()}
        config = json.loads(files['payload/model_config.json'])
        if config.get('engine') == 'tesseract':
            manifest.update(model_format='TESSERACT_CONFIG', runtime='tesseract', minimum_service_version='1.0')
        else:
            files['payload/weights.npz'] = (source / 'weights.npz').read_bytes()
            manifest.update(model_format='MOCK_NPZ', runtime=config['runtime'], minimum_service_version='1.0')
    elif kind == 'sanctions_list':
        raw = source.read_bytes()
        if source.suffix.lower() == '.json':
            data = json.loads(raw)
            files = {'payload/sanctions.json': raw}
            manifest.update(list_source='TRADEPROOF_JSON', record_count=len(data['entries']))
        else:
            files = {'payload/sanctions.csv': raw}
            count = len(list(csv.DictReader(io.StringIO(raw.decode('utf-8')))))
            manifest.update(list_source='SYNTHETIC_TEST', record_count=count, test_only=True)
    else:
        raise ValueError('Unsupported package type')
    manifest['files'] = [{'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                         for name, raw in sorted(files.items())]
    validated = Manifest.model_validate(manifest)
    # Vendor preflight uses the same bank content profile before signing a release.
    from b_registry.config import Settings
    from b_registry.verify.validation import MockNPZValidator
    from b_registry.verify.validation import parse_synthetic_csv
    if kind == 'ocr_model':
        if validated.model_format == 'TESSERACT_CONFIG':
            from b_registry.verify.profiles import validate_tesseract
            validate_tesseract(files['payload/model_config.json'], validated)
        else:
            MockNPZValidator().validate(files, validated, Settings())
    else:
        if validated.list_source == 'TRADEPROOF_JSON':
            from b_registry.verify.profiles import parse_tradeproof_list
            parse_tradeproof_list(files['payload/sanctions.json'], validated)
        else:
            parse_synthetic_csv(files['payload/sanctions.csv'], validated.record_count)
    raw_manifest = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
    # Exclusive creation: never silently replace an existing release.
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', raw_manifest)
        archive.writestr('signature.sig', base64.b64encode(key.sign(raw_manifest)))
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', choices=['ocr_model', 'sanctions_list'], required=True)
    parser.add_argument('--item-id', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--private-key', type=Path, default=os.getenv('VENDOR_PRIVATE_KEY'))
    args = parser.parse_args()
    if not args.private_key:
        parser.error('Pass --private-key or VENDOR_PRIVATE_KEY from the vendor environment')
    print(package_release(args.type, args.item_id, args.source, args.output, args.private_key))


if __name__ == '__main__':
    main()
