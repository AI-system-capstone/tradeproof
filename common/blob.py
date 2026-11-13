"""원문 서류 저장소. BLOB_ENDPOINT 가 있으면 MinIO(S3 호환), 없으면 로컬 폴더(BLOB_DIR, 기본 data/blob).

URI 형식 (transactions.doc_*_uri 에 그대로 저장):
  s3://<bucket>/<key>      MinIO
  file://<key>             로컬 폴더 (BLOB_DIR 기준 상대 키)
키는 contracts.blob_key(tx_id, doc_type, ext).

  store = make_blob_store()            # 환경변수로 고름
  uri = store.put(key, data, "application/pdf")
  data = store.get(uri)
  store.exists(uri)
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from common import contracts as C


class LocalBlobStore:
    kind = "local"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else C.DATA / "blob"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"file://{key}"

    def _path(self, uri: str) -> Path:
        if not uri.startswith("file://"):
            raise ValueError(f"로컬 저장소 URI 아님: {uri}")
        return self.root / uri[len("file://"):]

    def get(self, uri: str) -> bytes:
        return self._path(uri).read_bytes()

    def exists(self, uri: str) -> bool:
        return self._path(uri).exists()

    def ping(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


class MinioBlobStore:
    kind = "minio"

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str = C.BLOB_BUCKET,
                 secure: bool = False):
        from minio import Minio  # 로컬 폴더만 쓸 때는 minio 패키지 없이도 동작
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if not self._bucket_ready:
            if not self.client.bucket_exists(self.bucket):
                try:
                    self.client.make_bucket(self.bucket)
                except Exception:  # noqa: BLE001  다른 복제본이 먼저 만든 경우
                    if not self.client.bucket_exists(self.bucket):
                        raise
            self._bucket_ready = True

    def _split(self, uri: str) -> tuple[str, str]:
        prefix = "s3://"
        if not uri.startswith(prefix):
            raise ValueError(f"MinIO URI 아님: {uri}")
        bucket, _, key = uri[len(prefix):].partition("/")
        return bucket, key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._ensure_bucket()
        self.client.put_object(self.bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)
        return f"s3://{self.bucket}/{key}"

    def get(self, uri: str) -> bytes:
        bucket, key = self._split(uri)
        resp = self.client.get_object(bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def exists(self, uri: str) -> bool:
        bucket, key = self._split(uri)
        try:
            self.client.stat_object(bucket, key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ping(self) -> None:
        self._ensure_bucket()


def make_blob_store():
    endpoint = os.environ.get("BLOB_ENDPOINT")
    if endpoint:
        return MinioBlobStore(endpoint, os.environ.get("BLOB_ACCESS_KEY", "tradeproof"),
                              os.environ.get("BLOB_SECRET_KEY", "tradeproof-secret"),
                              os.environ.get("BLOB_BUCKET", C.BLOB_BUCKET),
                              secure=os.environ.get("BLOB_SECURE", "0") == "1")
    return LocalBlobStore(os.environ.get("BLOB_DIR"))
