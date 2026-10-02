"""가상 Kubernetes API (httpx.MockTransport) 와 가상 kubeconfig.

모든 객체·토큰·주소는 테스트용 가상 데이터입니다.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import yaml

SYN_TOKEN = "synthetic-sa-token-0123456789abcdef"
SYN_SERVER = "https://k8s.synthetic.test:6443"
SYN_CA = "-----BEGIN CERTIFICATE-----\nU1lOVEhFVElD\n-----END CERTIFICATE-----\n"

READ_ONLY_RULES: list[dict[str, Any]] = [
    {
        "apiGroups": ["authorization.k8s.io"],
        "resources": ["selfsubjectaccessreviews", "selfsubjectrulesreviews"],
        "verbs": ["create"],
    },
    {
        "apiGroups": [""],
        "resources": ["pods", "nodes", "namespaces", "events"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["apps"],
        "resources": ["deployments", "statefulsets", "daemonsets", "replicasets"],
        "verbs": ["get", "list", "watch"],
    },
]
ADMIN_RULES: list[dict[str, Any]] = [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]


def write_kubeconfig(
    directory: Path,
    *,
    user: dict[str, Any] | None = None,
    server: str = SYN_SERVER,
    cluster_extra: dict[str, Any] | None = None,
) -> Path:
    cluster: dict[str, Any] = {
        "server": server,
        "certificate-authority-data": base64.b64encode(SYN_CA.encode()).decode(),
    }
    cluster.update(cluster_extra or {})
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "reader",
        "clusters": [{"name": "syn", "cluster": cluster}],
        "users": [{"name": "infra-agent-reader", "user": user or {"token": SYN_TOKEN}}],
        "contexts": [
            {"name": "reader", "context": {"cluster": "syn", "user": "infra-agent-reader"}}
        ],
    }
    path = directory / "kubeconfig.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def pod(
    name: str,
    *,
    namespace: str = "otel-demo",
    phase: str = "Running",
    node: str | None = "k3d-syn-0",
    scheduled: dict[str, Any] | None = None,
    containers: list[dict[str, Any]] | None = None,
    owner: tuple[str, str] | None = None,
    created: str | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"phase": phase, "containerStatuses": containers or []}
    if scheduled is not None:
        status["conditions"] = [{"type": "PodScheduled", **scheduled}]
    spec: dict[str, Any] = {"nodeName": node} if node else {}
    meta: dict[str, Any] = {"name": name, "namespace": namespace}
    if created:
        meta["creationTimestamp"] = created
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {
        "metadata": meta,
        "spec": {**spec, "containers": [{"name": "x", "env": [{"name": "SECRET", "value": "v"}]}]},
        "status": status,
    }


def container(
    name: str,
    *,
    ready: bool = True,
    restarts: int = 0,
    waiting: dict[str, Any] | None = None,
    last_terminated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "ready": ready, "restartCount": restarts}
    out["state"] = {"waiting": waiting} if waiting else {"running": {}}
    if last_terminated:
        out["lastState"] = {"terminated": last_terminated}
    return out


def event(
    name: str,
    reason: str,
    last: str,
    *,
    kind: str = "Pod",
    namespace: str = "otel-demo",
    count: int = 1,
    message: str = "synthetic message",
    field_path: str | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {"kind": kind, "name": name, "namespace": namespace}
    if field_path:
        obj["fieldPath"] = field_path
    return {
        "metadata": {"name": f"{name}.{reason}", "namespace": namespace},
        "involvedObject": obj,
        "type": "Warning",
        "reason": reason,
        "message": message,
        "count": count,
        "lastTimestamp": last,
        "source": {"host": "k3d-syn-0"},
    }


class FakeKubeApi:
    """읽기 전용 Kubernetes API 흉내. 요청을 기록하고 허용되지 않은 요청이면 실패합니다."""

    def __init__(
        self,
        *,
        pods: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        rules: list[dict[str, Any]] | None = None,
        page_size: int | None = None,
    ) -> None:
        self.pods = pods or []
        self.events = events or []
        self.rules = READ_ONLY_RULES if rules is None else rules
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.fail_paths: dict[str, int] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def _page(self, items: list[dict[str, Any]], request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("continue") or 0)
        size = self.page_size or len(items) or 1
        chunk = items[start : start + size]
        meta: dict[str, Any] = {}
        if start + size < len(items):
            meta["continue"] = str(start + size)
        return httpx.Response(200, json={"kind": "List", "metadata": meta, "items": chunk})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {SYN_TOKEN}"
        path = request.url.path
        if path in self.fail_paths:
            return httpx.Response(self.fail_paths[path], json={"message": "synthetic failure"})
        if request.method == "POST":
            assert path == "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"
            body = json.loads(request.content)
            assert body["kind"] == "SelfSubjectRulesReview"
            return httpx.Response(
                201,
                json={"status": {"resourceRules": self.rules, "incomplete": False}},
            )
        assert request.method == "GET"
        if path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.31.5+k3s1-synthetic"})
        parts = path.strip("/").split("/")
        resource = parts[-1]
        namespace = parts[3] if len(parts) == 5 and parts[2] == "namespaces" else None
        if resource == "pods":
            items = [p for p in self.pods if namespace in (None, p["metadata"]["namespace"])]
            return self._page(items, request)
        if resource == "events":
            assert request.url.params.get("fieldSelector") == "type=Warning"
            items = [
                e for e in self.events if namespace in (None, e["involvedObject"]["namespace"])
            ]
            return self._page(items, request)
        return httpx.Response(404, json={"message": "not found"})
