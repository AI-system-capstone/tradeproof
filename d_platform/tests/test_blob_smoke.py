"""SeaweedFS(-s3.port=9000) + minio 파이썬 클라이언트 호환 smoke test (결정 0001).

실행 전:  docker compose up -d postgres minio minio-init
실행:     pytest d_platform/tests/test_blob_smoke.py -v

호스트에서 돌리므로 기본 주소는 localhost:9000 이다 (컨테이너 안에서는 BLOB_ENDPOINT=minio:9000).
저장소가 떠 있지 않으면 건너뛴다 → CI(저장소 없음)에서는 skip.
common/blob.py 가 쓰는 호출(bucket_exists, put_object, get_object, stat_object)과 같은 것만 확인한다.
"""
from __future__ import annotations

import io
import os
import socket
import uuid

import pytest

minio = pytest.importorskip("minio")
from minio.error import S3Error  # noqa: E402

ENDPOINT = os.environ.get("BLOB_SMOKE_ENDPOINT", "localhost:9000")
ACCESS_KEY = os.environ.get("BLOB_ACCESS_KEY", "tradeproof")
SECRET_KEY = os.environ.get("BLOB_SECRET_KEY", "tradeproof-dev-secret")
BUCKETS = ("tradeproof-docs", "tradeproof-registry")  # minio-init 이 만드는 버킷


def _reachable(endpoint: str) -> bool:
    host, _, port = endpoint.partition(":")
    try:
        with socket.create_connection((host, int(port or 9000)), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(ENDPOINT), reason=f"오브젝트 스토리지({ENDPOINT})가 떠 있지 않음"
)


@pytest.fixture(scope="module")
def client():
    return minio.Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY, secure=False)


@pytest.mark.parametrize("bucket", BUCKETS)
def test_init_buckets_exist(client, bucket):
    assert client.bucket_exists(bucket), f"{bucket} 없음 — minio-init 로그 확인"


def test_put_get_stat_list_remove(client):
    bucket = "tradeproof-docs"
    key = f"TX-SMOKE-{uuid.uuid4().hex[:8]}/invoice.pdf"  # contracts.blob_key() 와 같은 모양
    data = b"%PDF-1.4 smoke " + os.urandom(1024)

    client.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type="application/pdf")
    try:
        stat = client.stat_object(bucket, key)
        assert stat.size == len(data)
        assert stat.content_type == "application/pdf"

        resp = client.get_object(bucket, key)
        try:
            assert resp.read() == data
        finally:
            resp.close()
            resp.release_conn()

        prefix = key.split("/")[0] + "/"
        names = [o.object_name for o in client.list_objects(bucket, prefix=prefix, recursive=True)]
        assert names == [key]
    finally:
        client.remove_object(bucket, key)

    with pytest.raises(S3Error) as exc:
        client.stat_object(bucket, key)
    assert exc.value.code in ("NoSuchKey", "NotFound")


def test_multipart_size_upload(client):
    """6MB — minio 클라이언트가 multipart 로 나눠 올리는 크기(5MB 초과). 모델 파일 반입(B) 대비."""
    bucket = "tradeproof-registry"
    key = f"smoke/{uuid.uuid4().hex[:8]}.bin"
    data = os.urandom(6 * 1024 * 1024)
    client.put_object(bucket, key, io.BytesIO(data), length=len(data), part_size=5 * 1024 * 1024)
    try:
        assert client.stat_object(bucket, key).size == len(data)
        resp = client.get_object(bucket, key)
        try:
            assert resp.read() == data
        finally:
            resp.close()
            resp.release_conn()
    finally:
        client.remove_object(bucket, key)


def test_wrong_secret_is_rejected():
    bad = minio.Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key="wrong-secret", secure=False)
    with pytest.raises(S3Error) as exc:
        bad.bucket_exists("tradeproof-docs")
    assert exc.value.code in ("SignatureDoesNotMatch", "AccessDenied")
