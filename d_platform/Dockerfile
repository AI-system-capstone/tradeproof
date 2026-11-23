# d_platform 이미지 (배포 API, 포트 8300)
# 빌드 컨텍스트는 저장소 루트: docker build -f d_platform/Dockerfile -t tradeproof/d-platform:dev .
# 베이스 이미지 태그는 버전 고정 (전 모듈 동일 태그 → 레이어 공유로 디스크 절약)
#
# argo 드라이버(DEPLOY_DRIVER=argo)가 클러스터 안에서 돌 수 있게 helm·kubectl·Argo Rollouts 플러그인과 A 차트를 넣는다.
# 세 도구는 공식 배포처에서 받고 sha256 을 고정해 확인한다 (amd64·arm64: 리눅스 PC·WSL·맥 k3d).
# 버전은 d_platform/README 의 k3s 시연 환경에서 확인한 것과 같다. 올릴 때는 버전과 sha256 을 같이 바꾼다.

# ---------------------------------------------------------------- 도구 받기
FROM python:3.13.7-slim AS tools
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.34.1
ARG HELM_VERSION=v3.22.1
ARG ARGO_ROLLOUTS_VERSION=v1.10.2
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) KUBECTL_SHA=7721f265e18709862655affba5343e85e1980639395d5754473dafaadcaa69e3; \
             HELM_SHA=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea; \
             ARGO_SHA=355d62379d787a778f92903ce0b2fc1c1f4999d790d829370b6f1854a55f1cf9 ;; \
      arm64) KUBECTL_SHA=420e6110e3ba7ee5a3927b5af868d18df17aae36b720529ffa4e9e945aa95450; \
             HELM_SHA=4c29e286b279d170fa048ddfc3e4e83dcd069ca9c52102766ba672d7149e694d; \
             ARGO_SHA=b2d414abbdde815f2985c26f58c8461c7949b253e4c23a83737817af34b3be70 ;; \
      *) echo "지원하지 않는 아키텍처: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    python - "$TARGETARCH" "$KUBECTL_VERSION" "$HELM_VERSION" "$ARGO_ROLLOUTS_VERSION" \
             "$KUBECTL_SHA" "$HELM_SHA" "$ARGO_SHA" <<'PY'
import hashlib, io, sys, tarfile, urllib.request
arch, kv, hv, av, ks, hs, as_ = sys.argv[1:]
def get(url, sha):
    data = urllib.request.urlopen(url, timeout=120).read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        sys.exit(f"sha256 불일치: {url}\n  기대 {sha}\n  실제 {got}")
    return data
def put(path, data):
    with open(path, "wb") as f:
        f.write(data)
put("/usr/local/bin/kubectl", get(f"https://dl.k8s.io/release/{kv}/bin/linux/{arch}/kubectl", ks))
tgz = get(f"https://get.helm.sh/helm-{hv}-linux-{arch}.tar.gz", hs)
with tarfile.open(fileobj=io.BytesIO(tgz)) as t:
    put("/usr/local/bin/helm", t.extractfile(f"linux-{arch}/helm").read())
put("/usr/local/bin/kubectl-argo-rollouts",
    get(f"https://github.com/argoproj/argo-rollouts/releases/download/{av}/kubectl-argo-rollouts-linux-{arch}", as_))
PY
RUN chmod 0755 /usr/local/bin/kubectl /usr/local/bin/helm /usr/local/bin/kubectl-argo-rollouts

# ---------------------------------------------------------------- 배포 API
FROM python:3.13.7-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY d_platform/requirements.txt /app/d_platform/requirements.txt
RUN pip install -r /app/d_platform/requirements.txt

COPY --from=tools /usr/local/bin/kubectl /usr/local/bin/helm /usr/local/bin/kubectl-argo-rollouts /usr/local/bin/
COPY common/ /app/common/
COPY d_platform/__init__.py /app/d_platform/__init__.py
COPY d_platform/api/ /app/d_platform/api/
COPY d_platform/helm/ /app/d_platform/helm/
COPY d_platform/tests/ /app/d_platform/tests/

# helm 은 캐시·설정을 HOME 아래에 쓴다 → 쓸 수 있는 홈 폴더를 준다
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8300
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8300/healthz')"
CMD ["uvicorn", "d_platform.api.main:app", "--host", "0.0.0.0", "--port", "8300", "--no-access-log"]
