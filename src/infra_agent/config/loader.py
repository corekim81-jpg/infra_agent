"""설정 로딩.

우선순위: 기본값 < YAML 설정 파일 < 환경 변수.

- `INFRA_AGENT_CONFIG`: 설정 파일 경로
- `INFRA_AGENT_PROFILE`: 프로필 덮어쓰기
- `INFRA_AGENT__<SECTION>__<KEY>...`: 개별 항목 덮어쓰기 (`__`로 중첩 구분, 대소문자 무관)
  예) `INFRA_AGENT__DATASOURCES__PROMETHEUS__URL=http://127.0.0.1:19090`
  값은 YAML 스칼라로 해석합니다 (`true`, `15`, `null` 등).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from infra_agent.config.settings import Settings

CONFIG_PATH_ENV = "INFRA_AGENT_CONFIG"
PROFILE_ENV = "INFRA_AGENT_PROFILE"
OVERRIDE_PREFIX = "INFRA_AGENT__"


class ConfigError(Exception):
    """설정 파일 또는 환경 변수 설정 오류."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일 YAML 구문 오류: {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일 최상위는 매핑이어야 합니다: {path}")
    return data


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.upper().startswith(OVERRIDE_PREFIX):
            continue
        parts = [p.lower() for p in key[len(OVERRIDE_PREFIX) :].split("__")]
        if not parts or any(p == "" for p in parts):
            raise ConfigError(f"환경 변수 이름 형식이 올바르지 않습니다: {key}")
        try:
            value = yaml.safe_load(raw) if raw != "" else None
        except yaml.YAMLError as exc:
            raise ConfigError(f"환경 변수 값을 해석할 수 없습니다: {key}") from exc
        node = result
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"환경 변수 설정이 서로 충돌합니다: {key}")
            node = child
        node[parts[-1]] = value
    return result


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "설정 값이 올바르지 않습니다:\n" + "\n".join(lines)


def load_settings(
    config_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """설정 파일과 환경 변수를 병합해 검증된 Settings를 반환합니다."""
    env = os.environ if environ is None else environ

    data: dict[str, Any] = {}
    path_value = config_path if config_path is not None else env.get(CONFIG_PATH_ENV)
    if path_value:
        data = _read_yaml(Path(path_value))

    if env.get(PROFILE_ENV):
        data = _deep_merge(data, {"profile": env[PROFILE_ENV]})
    data = _deep_merge(data, _env_overrides(env))

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
