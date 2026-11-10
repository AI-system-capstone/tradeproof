# b_registry 이미지 (템플릿 — 담당자가 완성)
# 빌드 컨텍스트는 저장소 루트: docker build -f b_registry/Dockerfile .
# 베이스 이미지 태그는 버전 고정 (전 모듈 동일 태그 → 레이어 공유로 디스크 절약)
FROM python:3.13.7-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY b_registry/requirements.txt /app/b_registry/requirements.txt
RUN pip install --no-cache-dir -r /app/b_registry/requirements.txt

COPY common/ /app/common/
COPY b_registry/ /app/b_registry/

EXPOSE 8100
CMD ["uvicorn", "b_registry.api.main:app", "--host", "0.0.0.0", "--port", "8100"]
