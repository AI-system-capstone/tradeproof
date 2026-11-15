# d_platform 이미지 (배포 API, 포트 8300)
# 빌드 컨텍스트는 저장소 루트: docker build -f d_platform/Dockerfile -t tradeproof/d-platform:dev .
# 베이스 이미지 태그는 버전 고정 (전 모듈 동일 태그 → 레이어 공유로 디스크 절약)
FROM python:3.13.7-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY d_platform/requirements.txt /app/d_platform/requirements.txt
RUN pip install -r /app/d_platform/requirements.txt

COPY common/ /app/common/
COPY d_platform/__init__.py /app/d_platform/__init__.py
COPY d_platform/api/ /app/d_platform/api/
COPY d_platform/tests/ /app/d_platform/tests/

RUN useradd --no-create-home --uid 10001 app
USER app

EXPOSE 8300
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8300/healthz')"
CMD ["uvicorn", "d_platform.api.main:app", "--host", "0.0.0.0", "--port", "8300", "--no-access-log"]
