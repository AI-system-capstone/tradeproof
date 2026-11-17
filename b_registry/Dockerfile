FROM python:3.13.7-slim
WORKDIR /app
COPY b_registry/requirements.txt /app/b_registry/requirements.txt
RUN pip install --no-cache-dir -r /app/b_registry/requirements.txt
COPY common /app/common
COPY b_registry/__init__.py b_registry/config.py b_registry/contracts.py b_registry/db.py b_registry/storage.py b_registry/cli.py b_registry/migrations.py /app/b_registry/
COPY b_registry/api /app/b_registry/api
COPY b_registry/verify /app/b_registry/verify
EXPOSE 8100
CMD ["uvicorn", "b_registry.api.main:app", "--host", "0.0.0.0", "--port", "8100"]
