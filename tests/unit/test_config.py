from __future__ import annotations

from pathlib import Path

import pytest

from infra_agent.config import ConfigError, DataPolicy, LLMProvider, Profile, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "example.yaml"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file_are_safe() -> None:
    s = load_settings(environ={})
    assert s.profile is Profile.CI
    assert s.llm.provider is LLMProvider.FAKE
    assert s.llm.data_policy is DataPolicy.NONE
    assert not s.datasources.prometheus.enabled
    assert not s.datasources.kubernetes.enabled


def test_example_config_loads() -> None:
    s = load_settings(EXAMPLE, environ={})
    assert s.profile is Profile.DEV_TUNNEL
    assert s.datasources.prometheus.url == "http://127.0.0.1:19090"
    assert s.datasources.loki.url == "http://127.0.0.1:13100"
    assert s.datasources.tempo.url == "http://127.0.0.1:13200"
    assert s.llm.data_policy is DataPolicy.NONE
    assert s.execution.default_time_range == "30m"


def test_config_path_from_env() -> None:
    s = load_settings(environ={"INFRA_AGENT_CONFIG": str(EXAMPLE)})
    assert s.profile is Profile.DEV_TUNNEL


def test_env_overrides_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "profile: dev-tunnel\n"
        "datasources:\n  prometheus:\n    enabled: true\n    url: http://127.0.0.1:19090\n",
    )
    env = {
        "INFRA_AGENT__DATASOURCES__PROMETHEUS__URL": "http://localhost:9090/",
        "INFRA_AGENT__DATASOURCES__PROMETHEUS__TIMEOUT_SECONDS": "5",
        "INFRA_AGENT__EXECUTION__MAX_CONCURRENCY": "2",
        "INFRA_AGENT__LLM__MODEL": "null",
    }
    s = load_settings(path, environ=env)
    assert s.datasources.prometheus.url == "http://localhost:9090"  # 끝 슬래시 제거
    assert s.datasources.prometheus.timeout_seconds == 5
    assert s.execution.max_concurrency == 2
    assert s.llm.model is None


def test_profile_env_override() -> None:
    s = load_settings(EXAMPLE, environ={"INFRA_AGENT_PROFILE": "in-cluster"})
    assert s.profile is Profile.IN_CLUSTER


def test_unknown_key_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "datasources:\n  prometheus:\n    urll: http://x\n")
    with pytest.raises(ConfigError, match=r"datasources\.prometheus\.urll"):
        load_settings(path, environ={})


def test_unknown_env_key_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown_section"):
        load_settings(environ={"INFRA_AGENT__UNKNOWN_SECTION__X": "1"})


def test_enabled_datasource_requires_url(tmp_path: Path) -> None:
    path = _write(tmp_path, "datasources:\n  loki:\n    enabled: true\n")
    with pytest.raises(ConfigError, match="url"):
        load_settings(path, environ={})


@pytest.mark.parametrize("url", ["ftp://host", "127.0.0.1:9090", "http://"])
def test_invalid_url_rejected(tmp_path: Path, url: str) -> None:
    path = _write(tmp_path, f"datasources:\n  prometheus:\n    url: '{url}'\n")
    with pytest.raises(ConfigError):
        load_settings(path, environ={})


def test_credentials_in_url_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "datasources:\n  prometheus:\n    url: http://user:pw@host:9090\n")
    with pytest.raises(ConfigError, match="token_env") as info:
        load_settings(path, environ={})
    assert "pw@" not in str(info.value)


def test_token_env_must_be_variable_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "datasources:\n  prometheus:\n    token_env: 'abc123-secret-value'\n")
    with pytest.raises(ConfigError, match="환경 변수 이름"):
        load_settings(path, environ={})


def test_ci_profile_requires_fake_llm() -> None:
    with pytest.raises(ConfigError, match="fake"):
        load_settings(environ={"INFRA_AGENT__LLM__PROVIDER": "claude_agent_sdk"})


def test_dev_profile_allows_claude_agent_sdk() -> None:
    s = load_settings(EXAMPLE, environ={"INFRA_AGENT__LLM__PROVIDER": "claude_agent_sdk"})
    assert s.llm.provider is LLMProvider.CLAUDE_AGENT_SDK


def test_invalid_data_policy_rejected() -> None:
    with pytest.raises(ConfigError, match="data_policy"):
        load_settings(environ={"INFRA_AGENT__LLM__DATA_POLICY": "everything"})


def test_timeout_ordering_enforced() -> None:
    with pytest.raises(ConfigError, match="tool_timeout_seconds"):
        load_settings(environ={"INFRA_AGENT__EXECUTION__TOOL_TIMEOUT_SECONDS": "100"})


def test_invalid_default_time_range() -> None:
    with pytest.raises(ConfigError, match="default_time_range"):
        load_settings(environ={"INFRA_AGENT__EXECUTION__DEFAULT_TIME_RANGE": "30 minutes"})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="찾을 수 없습니다"):
        load_settings(tmp_path / "missing.yaml", environ={})


def test_non_mapping_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "- a\n- b\n")
    with pytest.raises(ConfigError, match="매핑"):
        load_settings(path, environ={})


def test_empty_file_uses_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    assert load_settings(path, environ={}).profile is Profile.CI


def test_settings_are_immutable() -> None:
    s = load_settings(environ={})
    with pytest.raises(ValueError):
        s.execution.max_concurrency = 10  # type: ignore[misc]
