"""비밀값 마스킹 테스트. 아래의 키·토큰은 모두 테스트용 가짜 값입니다."""

from __future__ import annotations

import io
import logging

import pytest

from infra_agent.security import MASK, RedactingFormatter, Redactor, redact


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ('headers={"Authorization": "Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
        ("password=hunter22", "hunter22"),
        ('{"token": "tok_fake_0001"}', "tok_fake_0001"),
        ("api_key: fakekey-1234", "fakekey-1234"),
        ("client_secret=abcd1234efgh", "abcd1234efgh"),
        ("http://admin:s3cretpw@prometheus:9090/api", "s3cretpw"),
        ("key sk-ant-api03-FAKEFAKEFAKEFAKE", "sk-ant-api03-FAKEFAKEFAKEFAKE"),
        ("ghp_FAKE0123456789abcdefFAKE01", "ghp_FAKE0123456789abcdefFAKE01"),
        ("aws AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlZmFrZQ",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlZmFrZQ",
        ),
    ],
)
def test_patterns_are_masked(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert MASK in out


@pytest.mark.parametrize(
    "text",
    [
        "k8s_node_cpu_usage{k8s_node_name='k3d-agent-0'} 0.42",
        "rate(hubble_drop_total[5m])",
        "http://127.0.0.1:19090/api/v1/query",
        "db_client_connection_count 12",
    ],
)
def test_normal_text_unchanged(text: str) -> None:
    assert redact(text) == text


def test_registered_literal_is_masked() -> None:
    r = Redactor()
    r.register("plain-secret-value")
    assert r.redact("value is plain-secret-value.") == f"value is {MASK}."


def test_short_literal_not_registered() -> None:
    r = Redactor()
    r.register("ab")
    r.register(None)
    assert r.redact("ab cd") == "ab cd"


def test_formatter_masks_message_args_and_exception() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("connect with %s", "password=topsecret1")
    try:
        raise RuntimeError("Authorization: Bearer leakedtoken12345")
    except RuntimeError:
        logger.exception("failed")

    out = stream.getvalue()
    assert "topsecret1" not in out
    assert "leakedtoken12345" not in out
    assert out.count(MASK) >= 2
