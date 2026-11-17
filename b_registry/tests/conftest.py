import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from common.blob import LocalBlobStore
from b_registry.config import Settings
from b_registry.db import Base, Database
from b_registry.storage import Artifacts
from b_registry.verify.workflow import ImportService

from .generate_fixtures import generate


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("registry-samples") / "fixtures")

def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in files:
            z.writestr(name, raw)
    return buffer.getvalue()

@pytest.fixture
def cfg(tmp_path, fixtures):
    return Settings(storage=tmp_path / "data", database_url="sqlite:///" + (tmp_path / "registry.db").as_posix(),
                    trust_key=fixtures / "trusted.pub")

@pytest.fixture
def service(cfg):
    db = Database(cfg.database_url)
    create_test_schema(db)
    svc = ImportService(db, cfg, Artifacts(LocalBlobStore(cfg.storage / "blob")))
    yield svc
    db.engine.dispose()

@pytest.fixture
def signer(cfg, tmp_path, fixtures):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "vendor.key"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cfg.trust_key = tmp_path / "trusted.pub"
    cfg.trust_key.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    def mutate(name, change=lambda f, m: None):
        with zipfile.ZipFile(fixtures / "packages" / name) as z:
            files = {n: z.read(n) for n in z.namelist()}
        manifest = json.loads(files["manifest.json"])
        change(files, manifest)
        for entry in manifest["files"]:
            raw = files[entry["path"]]
            entry.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        files["manifest.json"] = json.dumps(manifest).encode()
        files["manifest.sig"] = base64.b64encode(key.sign(files["manifest.json"]))
        return archive(files.items())
    return private, mutate


def create_test_schema(db):
    """Only disposable unit-test databases may be generated from ORM metadata."""
    from sqlalchemy import text
    Base.metadata.create_all(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO registry_lock(id) VALUES(1)"))
