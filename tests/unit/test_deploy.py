"""배포 파일 검사 (이미지 빌드·클러스터 적용 없이 내용만 확인)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from infra_agent.config.settings import KubernetesAuth, LLMProvider, Profile, Settings

ROOT = Path(__file__).resolve().parents[2]
JOBS = ("job-check.yaml", "job-ask.yaml")


def _load(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((ROOT / "deploy/k8s" / name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_in_cluster_config_is_valid_and_secret_free() -> None:
    raw = _load("config.yaml")["data"]["config.yaml"]
    settings = Settings.model_validate(yaml.safe_load(raw))
    assert settings.profile is Profile.IN_CLUSTER
    assert settings.datasources.kubernetes.auth is KubernetesAuth.IN_CLUSTER
    assert settings.llm.provider is LLMProvider.FAKE
    # 예시 주소만 사용 (실제 내부 주소·토큰 없음)
    for source in (settings.datasources.prometheus, settings.datasources.loki):
        assert source.url is not None and ".example.svc" in source.url
    assert "token" not in raw.replace("토큰", "").lower().replace("serviceaccount", "")


@pytest.mark.parametrize("name", JOBS)
def test_jobs_run_read_only_and_unprivileged(name: str) -> None:
    job = _load(name)
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "infra-agent-reader"
    assert pod["securityContext"]["runAsNonRoot"] is True
    (container,) = pod["containers"]
    security = container["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    assert "env" not in container  # 비밀값을 환경 변수로 넣지 않음
    assert container["args"][0] in ("check", "ask")


def test_containerfile_and_ignore_file() -> None:
    text = (ROOT / "deploy/Containerfile").read_text(encoding="utf-8")
    assert text.count("FROM rockylinux:9-minimal") == 2
    assert "USER 10001" in text and 'ENTRYPOINT ["infra-agent"]' in text
    copies = [line for line in text.splitlines() if line.startswith("COPY")]
    assert not any("local" in c or "kubeconfig" in c or ".env" in c for c in copies)
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "config/local.yaml", "*.kubeconfig", "var", ".git"):
        assert entry in ignored


def test_network_policy_example_opens_query_ports_only() -> None:
    text = (ROOT / "deploy/k8s/networkpolicy-example.yaml").read_text(encoding="utf-8")
    docs = [d for d in yaml.safe_load_all(text) if d]
    ports = set()
    for doc in docs:
        assert doc["kind"] == "NetworkPolicy" and doc["spec"]["policyTypes"] == ["Ingress"]
        (rule,) = doc["spec"]["ingress"]
        (source,) = rule["from"]
        assert source["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "infra-agent"
        }
        ports |= {p["port"] for p in rule["ports"]}
    assert ports == {9090, 3100, 3200}


def test_deployment_serves_api_with_token_from_secret() -> None:
    text = (ROOT / "deploy/k8s/deployment.yaml").read_text(encoding="utf-8")
    docs = {d["kind"]: d for d in yaml.safe_load_all(text) if d}
    pod = docs["Deployment"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "infra-agent-reader"
    (container,) = pod["containers"]
    assert container["args"][0] == "serve"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    (env,) = container["env"]
    # 토큰 값은 매니페스트에 없고 Secret에서만 받음
    assert env["name"] == "INFRA_AGENT_API_TOKEN" and "value" not in env
    assert env["valueFrom"]["secretKeyRef"] == {"name": "infra-agent-api", "key": "token"}
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert docs["Service"]["spec"]["type"] == "ClusterIP"  # 외부 노출 없음
    assert "Secret" not in docs and "Ingress" not in docs


def test_mcp_check_job_uses_token_from_secret_without_cluster_access() -> None:
    job = _load("job-mcp-check.yaml")
    assert job["metadata"]["namespace"] != "infra-agent"  # 다른 네임스페이스에서 점검
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod
    assert pod["securityContext"]["runAsNonRoot"] is True
    (container,) = pod["containers"]
    assert container["args"][:2] == ["mcp-check", "--url"]
    assert container["args"][2].endswith(".svc:8080/mcp")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    (env,) = container["env"]
    assert env["name"] == "INFRA_AGENT_API_TOKEN" and "value" not in env
    assert env["valueFrom"]["secretKeyRef"]["key"] == "token"


def test_api_clients_policy_limits_ingress_to_labeled_namespaces() -> None:
    doc = _load("networkpolicy-api-clients-example.yaml")
    assert doc["kind"] == "NetworkPolicy" and doc["metadata"]["namespace"] == "infra-agent"
    assert doc["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "infra-agent"}
    (rule,) = doc["spec"]["ingress"]
    (source,) = rule["from"]
    assert source == {"namespaceSelector": {"matchLabels": {"infra-agent.io/api-client": "true"}}}
    assert rule["ports"] == [{"port": 8080, "protocol": "TCP"}]


def test_deployment_probes_wait_for_startup() -> None:
    text = (ROOT / "deploy/k8s/deployment.yaml").read_text(encoding="utf-8")
    deployment = next(d for d in yaml.safe_load_all(text) if d and d["kind"] == "Deployment")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    assert container["readinessProbe"]["initialDelaySeconds"] > 0
    assert container["livenessProbe"]["initialDelaySeconds"] > 0
