"""공통 pytest 설정.

`live` 마커 테스트는 개발 서버 실제 데이터 소스에 연결합니다.
`INFRA_AGENT_LIVE_TESTS=1`이 아니면 건너뜁니다 (CI에서는 항상 건너뜀).
"""

from __future__ import annotations

import os

import pytest

LIVE_ENV = "INFRA_AGENT_LIVE_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip_live = pytest.mark.skip(
        reason=f"{LIVE_ENV}=1 이 아니므로 개발 서버 연동 테스트를 건너뜁니다"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
