"""읽기 전용 Kubernetes API 클라이언트 (전용 읽기 계정 전용).

- **계정 제한:** kubeconfig에서 ServiceAccount 토큰(`token`, `tokenFile`) 사용자만 허용합니다.
  클라이언트 인증서·exec 플러그인·auth-provider·사용자 이름/비밀번호·가장(impersonation) 설정이
  있으면 거부합니다. k3d·kubeadm 등이 만드는 관리자 kubeconfig는 클라이언트 인증서 형식이므로
  프로그램에 쓸 수 없습니다(CLAUDE.md "관리자 kubeconfig 사용 금지").
- **읽기 전용:** 허용 목록의 리소스에 대한 목록 조회(GET)와 `/version`만 보냅니다. `secrets`는 허용
  목록에 없습니다. 유일한 POST는 권한 점검용 `SelfSubjectRulesReview`이며, 이 요청은 저장되지
  않고 "현재 계정이 할 수 있는 일"만 돌려줍니다.
- 일시 오류(연결 실패, 타임아웃, 429, 5xx)만 제한 횟수 안에서 재시도합니다.
- 토큰은 마스킹 대상으로 등록하며, 오류 메시지에 kubeconfig 값을 넣지 않습니다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from infra_agent import __version__
from infra_agent.config.settings import KubernetesAuth, KubernetesConfig
from infra_agent.datasources.errors import (
    ConnectFailedError,
    DataSourceError,
    DataSourceTimeoutError,
    HttpStatusError,
    MissingCredentialError,
    ResponseFormatError,
)
from infra_agent.security import Redactor, default_redactor

SOURCE = "kubernetes"
SleepFn = Callable[[float], Awaitable[None]]

RESOURCES: Mapping[str, tuple[str, bool]] = {
    "pods": ("/api/v1", True),
    "events": ("/api/v1", True),
    "nodes": ("/api/v1", False),
    "namespaces": ("/api/v1", False),
    "deployments": ("/apis/apps/v1", True),
    "statefulsets": ("/apis/apps/v1", True),
    "daemonsets": ("/apis/apps/v1", True),
    "replicasets": ("/apis/apps/v1", True),
}
"""조회를 허용한 리소스 → (API 경로, 네임스페이스 리소스 여부). `secrets`는 넣지 않습니다."""

RULES_REVIEW_PATH = "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"
VERSION_PATH = "/version"
PAGE_SIZE = 500

FORBIDDEN_USER_KEYS = (
    "client-certificate",
    "client-certificate-data",
    "client-key",
    "client-key-data",
    "exec",
    "auth-provider",
    "username",
    "password",
    "as",
    "as-uid",
    "as-groups",
    "as-user-extra",
)
"""전용 읽기 계정 kubeconfig에 있으면 안 되는 사용자 설정 (관리자·개인 계정 형식, 가장)."""

_DNS_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9._/=!,-]{1,256}$")
_MAX_ERROR = 300
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class KubeconfigError(DataSourceError):
    """kubeconfig 형식 오류 또는 허용하지 않는 계정 형식."""

    code = "kubeconfig_error"


def is_k8s_name(value: str) -> bool:
    """Kubernetes 객체·네임스페이스 이름 형식(DNS subdomain)인지."""
    return bool(_DNS_NAME_RE.match(value))


@dataclass(frozen=True)
class KubeAccess:
    """kubeconfig에서 읽은 연결 정보. 토큰은 repr에 나오지 않습니다."""

    server: str
    token: str = field(repr=False)
    context: str
    ca_data: str | None = field(default=None, repr=False)
    """PEM 형식 CA 인증서 (certificate-authority-data를 복호화한 값)."""
    ca_file: str | None = None
    insecure: bool = False
    token_file: str | None = None
    """토큰을 다시 읽을 파일. 클러스터 내부의 ServiceAccount 토큰은 주기적으로 교체되므로
    요청마다 파일 변경을 확인합니다."""
    """kubeconfig의 insecure-skip-tls-verify. 서버 인증서를 검증하지 않으므로 점검에서 경고."""


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KubeconfigError(SOURCE, f"kubeconfig 형식 오류: {what}")
    return value


def _named(entries: object, name: str, what: str) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise KubeconfigError(SOURCE, f"kubeconfig 형식 오류: {what} 목록이 없습니다")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name:
            return _mapping(entry.get(what), f"{what} '{name}'")
    raise KubeconfigError(SOURCE, f"kubeconfig에 {what} '{name}'이(가) 없습니다")


def _resolve(base: Path, value: str) -> Path:
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else base / path


def load_kubeconfig(
    path: str | Path, context: str | None = None, *, allow_insecure_tls: bool = False
) -> KubeAccess:
    """전용 읽기 계정 kubeconfig를 읽습니다. 허용하지 않는 계정 형식이면 KubeconfigError.

    `insecure-skip-tls-verify`는 서버 인증서를 검증하지 않고 토큰을 보내므로, 설정에서 명시적으로
    허용(`allow_insecure_tls`)하지 않으면 거부합니다."""
    file = Path(path)
    try:
        doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise KubeconfigError(
            SOURCE, f"kubeconfig 파일을 읽지 못했습니다 ({type(exc).__name__})"
        ) from exc
    except yaml.YAMLError as exc:
        raise KubeconfigError(SOURCE, "kubeconfig YAML 형식 오류") from exc
    root = _mapping(doc, "최상위 객체가 아닙니다")
    ctx_name = context or root.get("current-context")
    if not isinstance(ctx_name, str) or not ctx_name:
        raise KubeconfigError(
            SOURCE, "사용할 컨텍스트가 없습니다 (context 설정 또는 current-context)"
        )
    ctx = _named(root.get("contexts"), ctx_name, "context")
    cluster_name, user_name = ctx.get("cluster"), ctx.get("user")
    if not isinstance(cluster_name, str) or not isinstance(user_name, str):
        raise KubeconfigError(SOURCE, f"context '{ctx_name}'에 cluster·user가 없습니다")
    cluster = _named(root.get("clusters"), cluster_name, "cluster")
    user = _named(root.get("users"), user_name, "user")

    forbidden = [k for k in FORBIDDEN_USER_KEYS if user.get(k) is not None]
    if forbidden:
        raise KubeconfigError(
            SOURCE,
            f"user '{user_name}'에 허용하지 않는 인증 설정({', '.join(forbidden)})이 있습니다. "
            "관리자·개인 kubeconfig는 사용할 수 없습니다. 전용 읽기 ServiceAccount 토큰 "
            "kubeconfig를 사용하세요 (docs/environment.md 3.5절).",
        )
    base = file.parent
    token = user.get("token")
    if not token and isinstance(user.get("tokenFile"), str):
        try:
            token = _resolve(base, user["tokenFile"]).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise KubeconfigError(
                SOURCE, f"tokenFile을 읽지 못했습니다 ({type(exc).__name__})"
            ) from exc
    if not isinstance(token, str) or not token:
        raise KubeconfigError(
            SOURCE, f"user '{user_name}'에 ServiceAccount 토큰(token 또는 tokenFile)이 없습니다"
        )

    server = cluster.get("server")
    parsed = urlparse(server) if isinstance(server, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.netloc:
        raise KubeconfigError(SOURCE, f"cluster '{cluster_name}'의 server는 https URL이어야 합니다")
    if parsed.username or parsed.password:
        raise KubeconfigError(SOURCE, "server URL에 인증정보를 넣지 마세요")

    ca_data: str | None = None
    raw_ca = cluster.get("certificate-authority-data")
    if isinstance(raw_ca, str) and raw_ca:
        try:
            ca_data = base64.b64decode(raw_ca, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise KubeconfigError(SOURCE, "certificate-authority-data 형식 오류") from exc
    ca_file = cluster.get("certificate-authority")
    insecure = bool(cluster.get("insecure-skip-tls-verify"))
    if insecure and not allow_insecure_tls:
        raise KubeconfigError(
            SOURCE,
            f"cluster '{cluster_name}'에 insecure-skip-tls-verify가 설정되어 있습니다. "
            "서버 인증서를 검증하지 않으면 토큰이 노출될 수 있어 거부합니다. "
            "certificate-authority-data를 쓰거나, 개발 환경에서만 "
            "datasources.kubernetes.allow_insecure_tls: true로 허용하세요.",
        )
    return KubeAccess(
        server=str(server).rstrip("/"),
        token=token,
        context=ctx_name,
        ca_data=ca_data,
        ca_file=str(_resolve(base, ca_file)) if isinstance(ca_file, str) and ca_file else None,
        insecure=insecure,
    )


SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
"""Pod에 ServiceAccount 토큰·CA가 마운트되는 표준 경로."""


def access_in_cluster(
    environ: Mapping[str, str] | None = None, sa_dir: Path = SERVICE_ACCOUNT_DIR
) -> KubeAccess:
    """Pod에 마운트된 ServiceAccount 토큰으로 접속 정보를 만듭니다 (클러스터 내부 실행).

    토큰·CA는 Pod의 ServiceAccount 것만 씁니다. 그 계정이 읽기 전용인지는 권한 점검
    (`assess_rules`)이 확인합니다."""
    env = os.environ if environ is None else environ
    host, port = env.get("KUBERNETES_SERVICE_HOST"), env.get("KUBERNETES_SERVICE_PORT")
    if not host or not port:
        raise MissingCredentialError(
            SOURCE,
            "클러스터 내부 실행이 아닙니다 (KUBERNETES_SERVICE_HOST·PORT 없음). "
            "클러스터 밖에서는 datasources.kubernetes.auth: kubeconfig를 사용하세요.",
        )
    token_file, ca_file = sa_dir / "token", sa_dir / "ca.crt"
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingCredentialError(
            SOURCE,
            f"ServiceAccount 토큰을 읽지 못했습니다 ({type(exc).__name__}). Pod의 "
            "serviceAccountName과 automountServiceAccountToken 설정을 확인하세요.",
        ) from exc
    if not token:
        raise MissingCredentialError(SOURCE, "ServiceAccount 토큰 파일이 비어 있습니다")
    if not ca_file.is_file():
        raise KubeconfigError(SOURCE, "ServiceAccount CA 인증서(ca.crt)가 없습니다")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6
    return KubeAccess(
        server=f"https://{host}:{port}",
        token=token,
        context="in-cluster",
        ca_file=str(ca_file),
        token_file=str(token_file),
    )


class _BearerAuth(httpx.Auth):
    """요청마다 Bearer 토큰을 붙입니다. 토큰 파일이 있으면 바뀌었을 때 다시 읽습니다."""

    def __init__(self, access: KubeAccess, redactor: Redactor) -> None:
        self._token = access.token
        self._path = Path(access.token_file) if access.token_file else None
        self._mtime: float | None = None
        self._redactor = redactor
        redactor.register(self._token)

    def _current(self) -> str:
        if self._path is None:
            return self._token
        try:
            mtime = self._path.stat().st_mtime
            if mtime != self._mtime:
                token = self._path.read_text(encoding="utf-8").strip()
                if token:
                    self._token = token
                    self._redactor.register(token)
                self._mtime = mtime
        except (OSError, UnicodeDecodeError):
            pass  # 교체 중 일시적으로 읽지 못하면 직전 토큰을 씁니다(만료됐다면 401로 드러남)
        return self._token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._current()}"
        yield request


def access_from_config(
    config: KubernetesConfig, environ: Mapping[str, str] | None = None
) -> KubeAccess:
    """설정의 인증 방식에 따라 연결 정보를 읽습니다 (kubeconfig 파일 또는 클러스터 내부)."""
    if config.auth is KubernetesAuth.IN_CLUSTER:
        return access_in_cluster(environ)
    env = os.environ if environ is None else environ
    path = env.get(config.kubeconfig_env)
    if not path:
        raise MissingCredentialError(
            SOURCE,
            f"kubeconfig 경로 환경 변수 {config.kubeconfig_env}가 설정되지 않았습니다 "
            "(전용 읽기 계정 kubeconfig 경로)",
        )
    return load_kubeconfig(path, config.context, allow_insecure_tls=config.allow_insecure_tls)


@dataclass(frozen=True)
class ListResult:
    items: tuple[dict[str, Any], ...]
    truncated: bool
    """`max_items`에 도달해 나머지를 가져오지 않았는지."""


@dataclass(frozen=True)
class RulesReview:
    namespace: str
    resource_rules: tuple[dict[str, Any], ...]
    incomplete: bool
    evaluation_error: str | None = None


def _verify(access: KubeAccess) -> ssl.SSLContext | bool:
    if access.insecure:
        return False
    if access.ca_data or access.ca_file:
        try:
            return ssl.create_default_context(cafile=access.ca_file, cadata=access.ca_data)
        except (ssl.SSLError, OSError) as exc:
            raise KubeconfigError(
                SOURCE, f"CA 인증서를 읽지 못했습니다 ({type(exc).__name__})"
            ) from exc
    return True


def _status_message(text: str) -> str:
    """Kubernetes Status 응답의 message (없으면 본문 앞부분)."""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        text = body["message"]
    return _CONTROL_RE.sub(" ", text).strip()[:_MAX_ERROR]


def _check_selector(value: str | None) -> None:
    if value is not None and not _SELECTOR_RE.match(value):
        raise ValueError(f"허용되지 않는 selector 형식입니다: {value!r}")


class KubernetesClient:
    """허용 리소스 목록 조회와 권한 점검만 하는 비동기 클라이언트."""

    def __init__(
        self,
        access: KubeAccess,
        *,
        timeout_seconds: float = 15.0,
        max_items: int = 2000,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
        redactor: Redactor | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.name = SOURCE
        self.server = access.server
        self.context = access.context
        self.insecure = access.insecure
        self._max_items = max_items
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self.last_elapsed_ms: int | None = None
        self._client = httpx.AsyncClient(
            base_url=access.server,
            headers={
                "User-Agent": f"infra-agent/{__version__}",
                "Accept": "application/json",
            },
            auth=_BearerAuth(access, redactor or default_redactor),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            verify=_verify(access) if transport is None else True,
            follow_redirects=False,
        )

    @classmethod
    def from_config(
        cls,
        config: KubernetesConfig,
        *,
        environ: Mapping[str, str] | None = None,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> KubernetesClient:
        return cls(
            access_from_config(config, environ),
            timeout_seconds=config.timeout_seconds,
            max_items=config.max_items,
            max_retries=max_retries,
            transport=transport,
        )

    async def __aenter__(self) -> KubernetesClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- 요청

    async def _send(
        self, method: str, path: str, params: Sequence[tuple[str, str]], body: object
    ) -> Any:
        if method == "POST":
            if path != RULES_REVIEW_PATH:
                raise ValueError(f"POST는 권한 점검 경로에만 허용됩니다: {path!r}")
        elif method != "GET":
            raise ValueError(f"허용되지 않는 메서드입니다: {method}")
        attempt = 0
        while True:
            try:
                return await self._once(method, path, params, body)
            except DataSourceError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                await self._sleep(self._backoff * (2**attempt))
                attempt += 1

    async def _once(
        self, method: str, path: str, params: Sequence[tuple[str, str]], body: object
    ) -> Any:
        started = time.perf_counter()
        try:
            response = await self._client.request(
                method, path, params=list(params), json=body if method == "POST" else None
            )
        except httpx.TimeoutException as exc:
            raise DataSourceTimeoutError(SOURCE, f"요청 시간 초과: {path}") from exc
        except httpx.TransportError as exc:
            raise ConnectFailedError(
                SOURCE, f"연결 실패: {self.server} ({type(exc).__name__})"
            ) from exc
        self.last_elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            message = _status_message(response.text)
            if response.status_code == 401:
                message = "인증 실패 (토큰이 유효하지 않거나 만료됨)"
            elif response.status_code == 403:
                message = f"권한 없음 — 읽기 계정 ClusterRole 확인 필요: {message}"
            raise HttpStatusError(SOURCE, response.status_code, message)
        try:
            return response.json()
        except ValueError as exc:
            raise ResponseFormatError(SOURCE, f"JSON이 아닌 응답입니다: {path}") from exc

    # --- 공개 메서드

    async def version(self) -> dict[str, Any]:
        data = await self._send("GET", VERSION_PATH, (), None)
        if not isinstance(data, dict):
            raise ResponseFormatError(SOURCE, "/version 응답 형식 오류")
        return data

    def list_path(self, resource: str, namespace: str | None = None) -> str:
        if resource not in RESOURCES:
            raise ValueError(f"허용되지 않은 리소스입니다: {resource!r}")
        prefix, namespaced = RESOURCES[resource]
        if namespace is None:
            return f"{prefix}/{resource}"
        if not namespaced:
            raise ValueError(f"{resource}는 네임스페이스 리소스가 아닙니다")
        if not is_k8s_name(namespace):
            raise ValueError(f"네임스페이스 이름 형식이 올바르지 않습니다: {namespace!r}")
        return f"{prefix}/namespaces/{namespace}/{resource}"

    async def list(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        field_selector: str | None = None,
    ) -> ListResult:
        """리소스 목록을 페이지 단위로 가져옵니다 (최대 `max_items`)."""
        path = self.list_path(resource, namespace)
        _check_selector(field_selector)
        items: list[dict[str, Any]] = []
        token: str | None = None
        # 빈 페이지와 continue 토큰을 계속 돌려주는 응답에 대비한 페이지 수 상한
        for _ in range(self._max_items // PAGE_SIZE + 3):
            params: list[tuple[str, str]] = [
                ("limit", str(min(PAGE_SIZE, self._max_items - len(items))))
            ]
            if field_selector:
                params.append(("fieldSelector", field_selector))
            if token:
                params.append(("continue", token))
            data = await self._send("GET", path, params, None)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ResponseFormatError(SOURCE, f"목록 응답 형식 오류: {path}")
            items.extend(i for i in data["items"] if isinstance(i, dict))
            meta = data.get("metadata")
            token = meta.get("continue") if isinstance(meta, dict) else None
            if len(items) > self._max_items or (token and len(items) >= self._max_items):
                return ListResult(tuple(items[: self._max_items]), truncated=True)
            if not token:
                return ListResult(tuple(items), truncated=False)
        return ListResult(tuple(items[: self._max_items]), truncated=True)

    async def self_rules(self, namespace: str = "default") -> RulesReview:
        """현재 계정이 `namespace`에서 가진 권한 목록 (ClusterRole 권한 포함)."""
        if not is_k8s_name(namespace):
            raise ValueError(f"네임스페이스 이름 형식이 올바르지 않습니다: {namespace!r}")
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": namespace},
        }
        data = await self._send("POST", RULES_REVIEW_PATH, (), body)
        status = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status, dict):
            raise ResponseFormatError(SOURCE, "SelfSubjectRulesReview 응답 형식 오류")
        rules = status.get("resourceRules")
        error = status.get("evaluationError")
        return RulesReview(
            namespace=namespace,
            resource_rules=tuple(r for r in rules or () if isinstance(r, dict)),
            incomplete=bool(status.get("incomplete")),
            evaluation_error=_CONTROL_RE.sub(" ", str(error))[:_MAX_ERROR] if error else None,
        )


# --- 권한 판정

WRITE_VERBS = frozenset(
    {
        "create",
        "update",
        "patch",
        "delete",
        "deletecollection",
        "escalate",
        "bind",
        "impersonate",
        "approve",
        "sign",
        "*",
    }
)
READ_VERBS = frozenset({"get", "list", "watch", "*"})
EXEC_SUBRESOURCES = frozenset(
    {
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "pods/proxy",
        "services/proxy",
        "nodes/proxy",
    }
)
"""동사와 관계없이(get 포함) 컨테이너 실행·kubelet·서비스 접근이 가능한 하위 리소스."""
SELF_REVIEW_RESOURCES = frozenset(
    {
        ("authorization.k8s.io", "selfsubjectaccessreviews"),
        ("authorization.k8s.io", "selfsubjectrulesreviews"),
        ("authentication.k8s.io", "selfsubjectreviews"),
    }
)
"""모든 인증 사용자에게 주어지는 자기 권한 조회(create). 저장되지 않으므로 쓰기로 보지 않습니다."""


@dataclass(frozen=True)
class PermissionReport:
    problems: tuple[str, ...]
    """쓰기·실행·프록시·secrets 읽기 권한, 권한 확인 불가 (있으면 API 조회를 하지 않음)."""
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _resource_label(group: str, resource: str) -> str:
    return f"{group}/{resource}" if group else resource


def merge_reviews(reviews: Sequence[RulesReview]) -> RulesReview:
    """여러 네임스페이스의 권한 목록을 합칩니다."""
    return RulesReview(
        namespace=", ".join(r.namespace for r in reviews),
        resource_rules=tuple(rule for r in reviews for rule in r.resource_rules),
        incomplete=any(r.incomplete for r in reviews),
        evaluation_error=next((r.evaluation_error for r in reviews if r.evaluation_error), None),
    )


def _is_exec_resource(group: str, resource: str) -> bool:
    """실행·프록시 하위 리소스를 포함하는 규칙인지 (`*`, `pods/*` 등 와일드카드 포함)."""
    if group not in ("", "*"):
        return False
    if resource == "*" or resource in EXEC_SUBRESOURCES:
        return True
    base, _, sub = resource.partition("/")
    return sub == "*" and base in ("pods", "nodes", "services", "*")


def assess_rules(review: RulesReview) -> PermissionReport:
    """권한 목록에서 쓰기·실행·프록시·secrets 읽기 권한을 찾습니다.

    권한 목록이 불완전(`incomplete`)한데 확인된 규칙이 하나도 없으면 판정할 수 없으므로 문제로
    봅니다(읽기 전용임을 확인하지 못한 계정은 쓰지 않음)."""
    writes: dict[str, set[str]] = {}
    execs: dict[str, set[str]] = {}
    secrets = False
    for rule in review.resource_rules:
        verbs = set(_strings(rule.get("verbs")))
        groups = _strings(rule.get("apiGroups")) or [""]
        resources = _strings(rule.get("resources"))
        write_verbs = verbs & WRITE_VERBS
        for group in groups:
            for resource in resources:
                base = resource.split("/", 1)[0]
                if write_verbs and not (
                    write_verbs == {"create"} and (group, base) in SELF_REVIEW_RESOURCES
                ):
                    writes.setdefault(_resource_label(group, resource), set()).update(write_verbs)
                if verbs & READ_VERBS and group in ("", "*") and base in ("secrets", "*"):
                    secrets = True
                if verbs and _is_exec_resource(group, resource):
                    execs.setdefault(_resource_label(group, resource), set()).update(verbs)
    problems: list[str] = []
    if writes:
        shown = sorted(writes.items())[:5]
        detail = "; ".join(f"{name}: {','.join(sorted(v))}" for name, v in shown)
        more = f" 외 {len(writes) - len(shown)}개" if len(writes) > len(shown) else ""
        problems.append(f"쓰기 권한 있음 ({detail}{more})")
    if execs:
        shown = sorted(execs.items())[:5]
        detail = "; ".join(f"{name}: {','.join(sorted(v))}" for name, v in shown)
        problems.append(f"Pod 실행·프록시 권한 있음 ({detail})")
    if secrets:
        problems.append("secrets 읽기 권한 있음")
    notes: list[str] = []
    if review.incomplete and not review.resource_rules:
        problems.append("권한 목록을 확인하지 못함 (incomplete, 확인된 규칙 없음)")
    elif review.incomplete:
        notes.append(
            "권한 목록이 불완전합니다(일부 권한 규칙을 평가하지 못함). 확인된 규칙만으로 판정"
        )
    return PermissionReport(tuple(problems), tuple(notes))


def rules_scope_note(namespaces: str) -> str:
    return (
        f"권한 점검은 '{namespaces}' 네임스페이스 기준입니다(ClusterRole 권한 포함, "
        "다른 네임스페이스에만 묶인 Role은 확인하지 않음)"
    )


def summarize_rules(rules: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """근거에 남길 권한 규칙 요약 (그룹·리소스·동사만)."""
    return [
        {
            "apiGroups": _strings(r.get("apiGroups")),
            "resources": _strings(r.get("resources")),
            "verbs": _strings(r.get("verbs")),
        }
        for r in rules
    ]
