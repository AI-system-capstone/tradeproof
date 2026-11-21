# c_replay 이미지 (서비스별 빌드 대상). 빌드 컨텍스트는 저장소 루트.
#   docker build -f c_replay/Dockerfile --target api -t tradeproof/c-replay:dev .       # 검증 조회 API + 운영 화면 (8200)
#   docker build -f c_replay/Dockerfile --target job -t tradeproof/c-replay-job:dev .   # 검증 Job (job/job-template.json 의 image)
# 검증 Job 은 A 의 OCR·판정 코드를 그대로 실행하므로 tesseract 를 A 의 ocr-base 와 같은 버전으로 고정한다
# (버전이 다르면 OCR 결과가 달라져 SVC-04 가 깨진다).

# ---------------------------------------------------------------- 공통
FROM python:3.13.7-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY c_replay/requirements.txt c_replay/requirements.txt
RUN pip install -r c_replay/requirements.txt
COPY common/__init__.py common/contracts.py common/blob.py common/
COPY c_replay/ c_replay/

# ---------------------------------------------------------------- api: 검증 조회 API + 운영 화면
FROM base AS api
EXPOSE 8200
CMD ["uvicorn", "c_replay.api.main:app", "--host", "0.0.0.0", "--port", "8200"]

# ---------------------------------------------------------------- job: 검증 Job (골든셋 검증 + Replay)
# 실행할 때 data/sanctions(제재 리스트)와 data/docs(과거 거래 서류)를 /app/data 에 붙여야 한다 (contracts.DATA)
FROM base AS job
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr=5.5.0-1+b1 tesseract-ocr-eng=1:4.1.0-2 poppler-utils \
 && rm -rf /var/lib/apt/lists/*
COPY a_screening/__init__.py a_screening/pipeline.py a_screening/registry.py a_screening/store.py a_screening/db_values.py a_screening/
COPY a_screening/ocr/ a_screening/ocr/
COPY a_screening/screening/ a_screening/screening/
CMD ["python", "-m", "c_replay.job"]
