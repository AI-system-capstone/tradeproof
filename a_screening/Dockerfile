# a_screening 이미지 (서비스별 빌드 대상). 빌드 컨텍스트는 저장소 루트.
#   docker build -f a_screening/Dockerfile --target screening-api -t tradeproof/a-screening:dev .
#   docker build -f a_screening/Dockerfile --target ocr-worker    -t tradeproof/a-ocr-worker:dev .
#   docker build -f a_screening/Dockerfile --target data-pipeline -t tradeproof/a-data-pipeline:dev .
# 버전 고정: OCR 결과(= 미탐 지표)가 tesseract 버전에 따라 달라지므로 베이스 이미지와 tesseract 를 고정한다.
#   python:3.13.7-slim = Debian 13(trixie) → tesseract-ocr 5.5.0-1+b1 (팀 기준값 94.3% 를 낸 버전)

# ---------------------------------------------------------------- 공통: 계약 + 판정 코드 (표준 라이브러리만)
FROM python:3.13.7-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY common/__init__.py common/contracts.py common/blob.py common/
COPY a_screening/__init__.py a_screening/store.py a_screening/db_values.py a_screening/
COPY a_screening/screening/ a_screening/screening/

# ---------------------------------------------------------------- screening-api: 접수·조회·검토·지표 (OCR 없음)
FROM base AS screening-api
COPY a_screening/requirements/api.txt a_screening/requirements/
RUN pip install -r a_screening/requirements/api.txt
COPY a_screening/api/ a_screening/api/
COPY a_screening/ui/ a_screening/ui/
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "a_screening.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------- OCR 공통 (tesseract 고정)
FROM base AS ocr-base
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr=5.5.0-1+b1 tesseract-ocr-eng=1:4.1.0-2 poppler-utils \
 && rm -rf /var/lib/apt/lists/*
COPY a_screening/requirements/ocr.txt a_screening/requirements/
RUN pip install -r a_screening/requirements/ocr.txt
COPY a_screening/ocr/ a_screening/ocr/
# OCR → 판정 경로 (워커와 C 의 골든셋·Replay 가 같이 씀)
COPY a_screening/pipeline.py a_screening/

# ---------------------------------------------------------------- ocr-worker: 큐(screening_jobs)에서 꺼내 OCR → 판정 → 저장 (상시)
FROM ocr-base AS ocr-worker
CMD ["python", "-m", "a_screening.ocr.worker"]

# ---------------------------------------------------------------- data-pipeline: 리스트 → 서류 → OCR → 평가 → DB 시드 (일회성)
FROM ocr-base AS data-pipeline
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY a_screening/requirements/pipeline.txt a_screening/requirements/
RUN pip install -r a_screening/requirements/pipeline.txt
COPY a_screening/sanctions/ a_screening/sanctions/
COPY a_screening/docs/ a_screening/docs/
COPY a_screening/db/ a_screening/db/
COPY a_screening/run_all.sh a_screening/
COPY common/schema/10_a_screening.sql common/schema/
CMD ["a_screening/run_all.sh"]
