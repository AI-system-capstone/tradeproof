"""API 명세서 파일을 코드에서 다시 만든다: docs/openapi/a_screening.json

  python -m a_screening.api.export_openapi            # 파일 갱신
  python -m a_screening.api.export_openapi --check    # 파일이 코드와 다르면 실패 (CI·테스트용)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "openapi" / "a_screening.json"


def render() -> str:
    from a_screening.api.main import app
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} 가 코드와 다름 → python -m a_screening.api.export_openapi", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"썼음: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
