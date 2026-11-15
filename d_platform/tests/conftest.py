"""`pytest d_platform/tests` 처럼 pytest 를 직접 실행해도(CI) `import d_platform` 이 되게 저장소 루트를 경로에 넣는다."""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
