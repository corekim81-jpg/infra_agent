"""데이터 소스 오류 분류.

모든 메시지는 마스킹된 상태로 보관합니다.
"""

from __future__ import annotations

from infra_agent.security import redact


class DataSourceError(Exception):
    """데이터 소스 조회 오류의 기본 클래스."""

    code: str = "datasource_error"
    retryable: bool = False

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = redact(message)
        super().__init__(f"[{source}] {self.message}")


class MissingCredentialError(DataSourceError):
    code = "missing_credential"


class ConnectFailedError(DataSourceError):
    code = "connect_error"
    retryable = True


class DataSourceTimeoutError(DataSourceError):
    code = "timeout"
    retryable = True


class HttpStatusError(DataSourceError):
    def __init__(self, source: str, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.code = f"http_{status_code}"
        self.retryable = status_code == 429 or status_code >= 500
        super().__init__(source, f"HTTP {status_code}: {message}")


class QueryError(DataSourceError):
    """조회식 오류 등 데이터 소스가 요청을 거부한 경우 (재시도하지 않음)."""

    code = "query_error"


class ResponseFormatError(DataSourceError):
    code = "bad_response"
