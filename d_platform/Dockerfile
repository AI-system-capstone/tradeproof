# d_platform 이미지 (템플릿 — 담당자가 완성)
# 빌드 컨텍스트는 저장소 루트: docker build -f d_platform/Dockerfile .
# 베이스 이미지 태그는 버전 고정 (전 모듈 동일 태그 → 레이어 공유로 디스크 절약)
FROM python:3.13.7-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY d_platform/requirements.txt /app/d_platform/requirements.txt
RUN pip install --no-cache-dir -r /app/d_platform/requirements.txt

COPY common/ /app/common/
COPY d_platform/ /app/d_platform/

EXPOSE 8300
CMD ["uvicorn", "d_platform.api.main:app", "--host", "0.0.0.0", "--port", "8300"]
