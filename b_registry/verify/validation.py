import base64
import csv
import hashlib
import io
import json
import math
import re
import stat
import zipfile
import numpy as np
from pathlib import PurePosixPath
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from b_registry.verify.schemas import Manifest, ModelConfig
from typing import Protocol
from datetime import date
from dataclasses import dataclass, field


class ValidationFailure(Exception):
    def __init__(self, stage: str, code: str, message: str):
        self.stage, self.code, self.message = stage, code, message
        super().__init__(message)


def fail(stage, code, message):
    raise ValidationFailure(stage, code, message)


def safe_zip(raw: bytes, cfg, *, nested=False) -> dict[str, bytes]:
    stage = 'content' if nested else 'intake'
    if len(raw) > cfg.max_zip_bytes:
        fail(stage, 'ZIP_SIZE_LIMIT', 'Archive exceeds the configured limit.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            infos = z.infolist()
            if len(infos) > cfg.max_files:
                fail(stage, 'ZIP_FILE_LIMIT', 'Too many archive entries.')
            seen, total = set(), 0
            for i in infos:
                name = i.filename
                parts = PurePosixPath(name).parts
                if (not name or i.orig_filename != name or '\\' in name or ':' in name or '\x00' in name
                        or name.startswith('/') or any(p in ('..', '.') for p in name.split('/'))
                        or any(p.endswith((' ', '.')) for p in parts)):
                    fail(stage, 'ZIP_PATH', 'Unsafe archive path.')
                normalized = name.casefold().rstrip('/')
                if normalized in seen:
                    fail(stage, 'ZIP_DUPLICATE', 'Duplicate archive entry.')
                seen.add(normalized)
                mode = i.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    fail(stage, 'ZIP_LINK', 'Links and special files are prohibited.')
                if i.flag_bits & 1 or i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    fail(stage, 'ZIP_ENCODING', 'Unsupported archive encoding.')
                total += i.file_size
                if (i.file_size > cfg.max_file_bytes or total > cfg.max_total_bytes
                        or i.file_size / max(i.compress_size, 1) > cfg.max_ratio):
                    fail(stage, 'ZIP_EXPANSION_LIMIT', 'Archive expansion exceeds policy.')
            # No archive entry is extracted to a filesystem path.
            return {i.filename: z.read(i) for i in infos if not i.is_dir()}
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError, ValueError):
        fail(stage, 'ZIP_INVALID', 'Archive cannot be parsed.')


def unique_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def verify_signature(files, trust_key):
    signatures = {'manifest.sig', 'signature.sig'} & files.keys()
    if 'manifest.json' not in files or len(signatures) != 1:
        fail('signature', 'MANIFEST_MISSING', 'Manifest or detached signature is missing.')
    try:
        key = load_pem_public_key(trust_key.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError('Wrong key type')
        sig = base64.b64decode(files[next(iter(signatures))].strip(), validate=True)
        key.verify(sig, files['manifest.json'])
    except (InvalidSignature, ValueError, TypeError):
        fail('signature', 'SIGNATURE_INVALID', 'Trusted publisher signature verification failed.')


def verify_manifest(files):
    try:
        manifest = Manifest.model_validate(unique_json(files['manifest.json']))
    except (ValidationError, ValueError, UnicodeError):
        fail('manifest', 'MANIFEST_SCHEMA', 'Manifest does not match the accepted schema.')
    if set(files) != {'manifest.json', *({'manifest.sig', 'signature.sig'} & files.keys()), *(f.path for f in manifest.files)}:
        fail('hash', 'UNDECLARED_FILE', 'Archive file set differs from the signed manifest.')
    for entry in manifest.files:
        raw = files[entry.path]
        if len(raw) != entry.size or hashlib.sha256(raw).hexdigest() != entry.sha256:
            fail('hash', 'PAYLOAD_HASH_MISMATCH', 'Payload size or digest differs from the signed manifest.')
    return manifest


class ModelValidator(Protocol):
    def validate(self, files: dict[str, bytes], manifest, cfg) -> dict: ...


class MockNPZValidator:
    def validate(self, files, manifest, cfg):
        try:
            config = ModelConfig.model_validate(unique_json(files['payload/model_config.json']))
        except (ValueError, ValidationError, UnicodeError):
            fail('content', 'MODEL_CONFIG', 'Invalid mock model configuration.')
        if (config.runtime != 'mock-ocr-v1' or manifest.runtime != config.runtime
                or '1.0' not in config.service_compatibility or manifest.minimum_service_version != '1.0'):
            fail('content', 'MODEL_INCOMPATIBLE', 'Runtime or service compatibility is unsupported.')
        if config.model_id != manifest.item_id:
            fail('content', 'MODEL_ID', 'Model identity differs from the manifest.')
        schema = config.weights_schema
        # The bank owns the accepted MOCK_NPZ profile; the publisher cannot relax it.
        if schema != {'encoder': [16, 16], 'decoder': [16, 8]}:
            fail('content', 'WEIGHTS_SCHEMA', 'Unsupported mock weights schema.')
        raw = files['payload/weights.npz']
        entries = safe_zip(raw, cfg, nested=True)
        if set(entries) != {f'{name}.npy' for name in schema}:
            fail('content', 'WEIGHTS_NAMES', 'Unexpected array names.')
        try:
            # Validate NPY headers before numpy can allocate based on untrusted shapes.
            for name, shape in schema.items():
                buf = io.BytesIO(entries[name + '.npy'])
                version = np.lib.format.read_magic(buf)
                if version == (1, 0):
                    actual_shape, _, dtype = np.lib.format.read_array_header_1_0(buf)
                elif version == (2, 0):
                    actual_shape, _, dtype = np.lib.format.read_array_header_2_0(buf)
                else:
                    raise ValueError('Unsupported NPY version')
                if list(actual_shape) != shape or dtype != np.dtype('float32'):
                    raise ValueError('Unexpected array schema')
                if len(buf.getbuffer()) - buf.tell() != math.prod(shape) * 4:
                    raise ValueError('Unexpected array size')
            with np.load(io.BytesIO(raw), allow_pickle=False) as arrays:
                for name in schema:
                    if not np.isfinite(arrays[name]).all():
                        raise ValueError('Nonfinite weights')
        except (ValueError, TypeError, OSError, EOFError):
            fail('content', 'WEIGHTS_INVALID', 'Mock weights contain invalid arrays.')
        return {'profile': 'MOCK_NPZ', 'inference_performed': False}


class ONNXValidator:
    """Extension point: checker, IO schema, OpSet and bank runtime compatibility."""
    def validate(self, files, manifest, cfg):
        fail('content', 'ONNX_NOT_ENABLED', 'ONNX validation requires a separately approved plugin.')


COLUMNS = ['sanction_id', 'entity_name', 'entity_type', 'country_code', 'aliases', 'source', 'listed_on', 'last_updated']


def parse_synthetic_csv(raw, count=None):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8'), newline=''), strict=True)
        if reader.fieldnames != COLUMNS:
            raise ValueError('Columns')
        rows = {}
        for row in reader:
            if None in row or any(not isinstance(v, str) or not v.strip() for v in row.values()):
                raise ValueError('Missing value')
            sid = row['sanction_id']
            if sid in rows or sid != sid.strip():
                raise ValueError('Duplicate or invalid ID')
            if row['entity_type'] not in {'ORGANIZATION', 'INDIVIDUAL', 'VESSEL', 'AIRCRAFT'}:
                raise ValueError('Entity enum')
            if row['country_code'] != 'ZZ' or row['source'] != 'SYNTHETIC_TEST':
                raise ValueError('Synthetic profile only')
            for field in ('listed_on', 'last_updated'):
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row[field]):
                    raise ValueError('Date format')
                date.fromisoformat(row[field])
            if row['last_updated'] < row['listed_on']:
                raise ValueError('Date order')
            rows[sid] = row
        if count is not None and len(rows) != count:
            raise ValueError('Record count')
        return rows
    except (UnicodeError, csv.Error, ValueError, TypeError):
        fail('content', 'CSV_SCHEMA', 'Synthetic CSV schema, values, dates or record count are invalid.')


def changes(current, prior):
    a, b = set(current), set(prior)
    added, removed = len(a - b), len(b - a)
    modified = sum(current[k] != prior[k] for k in a & b)
    return {'added': added, 'removed': removed, 'modified': modified,
            'prior_count': len(prior), 'change_ratio': (added + removed + modified) / max(1, len(prior))}


@dataclass
class ValidationReport:
    steps: list = field(default_factory=list)
    manifest: object = None
    rows: dict | None = None
    error: ValidationFailure | None = None


def validate(raw, cfg):
    report = ValidationReport()
    try:
        files = safe_zip(raw, cfg)
        report.steps.append({'stage': 'intake', 'status': 'PASSED'})
        verify_signature(files, cfg.trust_key)
        report.steps.append({'stage': 'signature', 'status': 'PASSED'})
        report.manifest = verify_manifest(files)
        report.steps.extend([{'stage': s, 'status': 'PASSED'} for s in ('manifest', 'hash')])
        if report.manifest.package_type == 'ocr_model':
            if report.manifest.model_format == 'TESSERACT_CONFIG':
                from b_registry.verify.profiles import validate_tesseract
                details = validate_tesseract(files['payload/model_config.json'], report.manifest)
            else:
                details = MockNPZValidator().validate(files, report.manifest, cfg)
        else:
            if report.manifest.list_source == 'TRADEPROOF_JSON':
                from b_registry.verify.profiles import parse_tradeproof_list
                report.rows = parse_tradeproof_list(files['payload/sanctions.json'], report.manifest)
            else:
                report.rows = parse_synthetic_csv(files['payload/sanctions.csv'], report.manifest.record_count)
            details = {'record_count': len(report.rows), 'profile': report.manifest.list_source}
        report.steps.append({'stage': 'content', 'status': 'PASSED', 'details': details})
    except ValidationFailure as error:
        report.error = error
        report.steps.append({'stage': error.stage, 'status': 'FAILED', 'code': error.code, 'message': error.message})
    return report
