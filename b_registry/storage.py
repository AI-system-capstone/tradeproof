import hashlib
import os
from common.blob import LocalBlobStore, MinioBlobStore

class StorageUnavailable(Exception):
    pass

def make_store(cfg):
    endpoint = os.getenv("BLOB_ENDPOINT")
    if endpoint:
        return MinioBlobStore(endpoint, os.getenv("BLOB_ACCESS_KEY", "tradeproof"),
            os.getenv("BLOB_SECRET_KEY", ""), bucket=os.getenv("REGISTRY_BLOB_BUCKET", "tradeproof-registry"),
            secure=os.getenv("BLOB_SECURE", "false").lower() in ("1", "true"))
    return LocalBlobStore(os.getenv("BLOB_DIR", str(cfg.storage / "blob")))

class Artifacts:
    def __init__(self, store):
        self.store = store

    def put(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        key = "registry/" + digest
        # Content-addressed immutable objects; callers cannot supply a key.
        uri = "s3://" + self.store.bucket + "/" + key if self.store.kind == "minio" else "file://" + key
        try:
            if self.store.exists(uri):
                if hashlib.sha256(self.store.get(uri)).hexdigest() != digest:
                    raise StorageUnavailable("Stored artifact hash mismatch")
                return uri
            return self.store.put(key, raw)
        except Exception as error:
            raise StorageUnavailable("Artifact storage unavailable") from error

    def get(self, uri):
        try:
            return self.store.get(uri)
        except Exception as error:
            raise StorageUnavailable("Artifact storage unavailable") from error
