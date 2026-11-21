# Repository Guidelines

## Project Structure

TradeProof splits responsibility for model and sanctions-list updates across four Python services.

| Module | Responsibility |
| --- | --- |
| `a_screening/` | OCR, transaction screening, and the reviewer UI (port 8000) |
| `b_registry/` | Package intake, bundle registry, release gates, and approvals (8100) |
| `c_replay/` | Golden-set validation, Replay jobs, and the operator UI (8200) |
| `d_platform/` | Canary deployment, rollback, monitoring, and k3s infrastructure (8300) |

Each module keeps tests in its own `tests/` directory. Shared Python contracts and SQL live in `common/` and `common/schema/`. Dataset specifications live in `dataset/spec/`. Keep generated assets in `data/`, and keep local `.env` files and signing keys out of Git.

## Local Development

Run commands from the repository root. For local storage, copy `.env.example` to `.env` on first setup.

```sh
docker compose up -d postgres minio minio-init   # Start PostgreSQL and SeaweedFS and initialize buckets
docker build -f b_registry/Dockerfile -t tradeproof/b-registry:dev .
```

To run the C API and operator UI locally, activate a Python 3.13 virtual environment and run these commands.

```sh
python -m pip install -r c_replay/requirements.txt
python -m uvicorn c_replay.api.main:app --port 8200
```

## Coding and Testing

Use four-space indentation, `snake_case` for functions and variables, and `PascalCase` for classes. Import shared status values and field names from `common/contracts.py`. Pin dependencies with `==` in the module's requirements file and pin Docker base image tags.

Name pytest files and functions `test_*`. After installing the module's requirements and pytest, run its suite, for example `python -m pytest -q b_registry/tests`. CI runs changed modules and selects all module suites for changes under `common/`. CI specifies no coverage threshold or formatter check.

## Commits and Pull Requests

Use a `<module>/<topic>` branch, such as `b/demo-recovery`. Write a short commit subject describing the change. Recent commits often start with a module tag such as `[A]` or `[C]`.

Prefix PR titles with `[A]`, `[B]`, `[C]`, `[D]`, or `[DATA]`. Follow the [PR template](.github/PULL_REQUEST_TEMPLATE.md) for requirement IDs, scenarios, and test results. Coordinate cross-module edits with the owner. For API changes, update the module README and OpenAPI JSON. For interface changes, update the relevant table and dated log in [docs/interfaces.md](docs/interfaces.md), notify the team chat, apply `contract-change`, and request affected owners' reviews. Merge to `main` after one review and passing CI.
