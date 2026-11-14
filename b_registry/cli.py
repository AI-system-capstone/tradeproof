"""Run from the tradeproof repository root: python -m b_registry.cli."""
import argparse
import json
from pathlib import Path
from b_registry.config import Settings
from b_registry.db import Database
from b_registry.verify.validation import validate

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-db")
    sub.add_parser("migrate-db")
    check = sub.add_parser("validate")
    check.add_argument("package", type=Path)
    check.add_argument("--trust-key", type=Path, required=True)
    args = parser.parse_args()
    cfg = Settings()
    if args.command == "migrate-db":
        from b_registry.migrations import migrate
        migrate(cfg.database_url)
        print("B registry migration applied; existing IDs and artifact URIs preserved")
    elif args.command == "check-db":
        db = Database(cfg.database_url)
        try:
            db.check_schema()
            print("B registry schema ready")
        finally:
            db.engine.dispose()
    else:
        cfg.trust_key = args.trust_key
        with args.package.open("rb") as stream:
            report = validate(stream.read(cfg.max_zip_bytes + 1), cfg)
        print(json.dumps({"status": "REJECTED" if report.error else "APPROVED",
                          "reason": str(report.error) if report.error else None}))
        raise SystemExit(1 if report.error else 0)

if __name__ == "__main__":
    main()
