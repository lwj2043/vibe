"""pytest 부트스트랩.

기존 vibe 의 테스트는 vibe/ 가 cwd 라는 가정으로 ``import server`` /
``from pipelines import ...`` 를 직접 한다. vibe-rebuild 에서는 server/routes/pipelines
가 ``orchestrator/`` 안에 들어갔으므로 그 디렉터리를 sys.path 에 prepend 해
기존 import 경로를 그대로 동작시킨다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # vibe-rebuild/
ORCH = ROOT / "orchestrator"
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))
