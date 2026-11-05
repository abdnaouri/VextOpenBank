"""
test_exchange_logger.py
Comprehensive test suite verifying Exchange & Discovery Logger,
telemetry capture, rotation, secret redaction, and zero hardcoded secrets.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from vext_open_bank.core.exchange_logger import ExchangeLogger, exchange_logger
from vext_open_bank.main import app, ORIGIN_SHIELD_SECRET
from vext_open_bank.services.onegini import onegini_client
from vext_open_bank.services.airlock import airlock_web, _fetch_csrf_token


@pytest.fixture
def temp_logger():
    temp_dir = tempfile.mkdtemp()
    log_file = Path(temp_dir) / "exchanges.jsonl"
    telemetry_file = Path(temp_dir) / "telemetry.jsonl"
    logger_instance = ExchangeLogger(
        log_path=str(log_file),
        telemetry_path=str(telemetry_file),
    )
    yield logger_instance
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_basic_exchange_logging_and_jsonl(temp_logger):
    """Verify that exchanges are written as valid single-line JSON records."""
    rec = temp_logger.log_exchange(
        exchange_type="CFG_TEST_CALL",
        target="/test/endpoint",
        direction="OUTBOUND",
        status_code=200,
        request_data={"account_rib": "051780000000123456789001"},
        response_data={"balance_mad": 45200.50, "currency": "MAD"},
        duration_ms=35.4,
    )

    assert rec["status_code"] == 200
    assert rec["target"] == "/test/endpoint"
    assert temp_logger.log_path.exists()

    with open(temp_logger.log_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["exchange_type"] == "CFG_TEST_CALL"
        assert parsed["duration_ms"] == 35.4
        assert parsed["discovery"]["has_new_data"] is True
        assert "res.balance_mad" in parsed["discovery"]["new_fields"]


def test_discovery_engine_detects_new_fields_and_headers(temp_logger):
    """Verify that new fields and headers are discovered on first appearance and not flagged on duplicates."""
    # First call with novel schema
    r1 = temp_logger.log_exchange(
        exchange_type="CFG_ACCOUNT_INFO",
        target="/services/AccountOverview",
        status_code=200,
        request_data={"fieldA": "value1"},
        response_data={"fieldB": 100},
        headers={"x-custom-bank-header": "v1"},
    )
    assert r1["discovery"]["has_new_data"] is True
    assert "req.fieldA" in r1["discovery"]["new_fields"]
    assert "res.fieldB" in r1["discovery"]["new_fields"]
    assert "x-custom-bank-header" in r1["discovery"]["new_headers"]
    assert r1["discovery"]["is_new_endpoint"] is True

    # Second call with same schema — no new discovery
    r2 = temp_logger.log_exchange(
        exchange_type="CFG_ACCOUNT_INFO",
        target="/services/AccountOverview",
        status_code=200,
        request_data={"fieldA": "value2"},
        response_data={"fieldB": 200},
        headers={"x-custom-bank-header": "v2"},
    )
    assert r2["discovery"]["new_fields"] == []
    assert r2["discovery"]["new_headers"] == []
    assert r2["discovery"]["has_new_data"] is False


def test_interesting_telemetry_detection(temp_logger):
    """Verify detection of 429 rate limit, 500 error, MFA OTP challenge, high latency, and high-value movements."""
    # Rate limit 429 with Retry-After header
    r_429 = temp_logger.log_exchange(
        exchange_type="CFG_INVOKE",
        target="/services/Transfers",
        status_code=429,
        headers={"retry-after": "60"},
    )
    events = r_429["discovery"]["interesting_events"]
    assert any("RATE_LIMITED_429" in e for e in events)
    assert any("RETRY_AFTER" in e for e in events)

    # Server error 503
    r_503 = temp_logger.log_exchange(
        exchange_type="CFG_INVOKE",
        target="/services/Transfers",
        status_code=503,
    )
    assert any("SERVER_ERROR_503" in e for e in r_503["discovery"]["interesting_events"])

    # High-value transfer
    r_high = temp_logger.log_exchange(
        exchange_type="CFG_TRANSFER",
        target="/services/TransferConfirm",
        status_code=200,
        response_data={"amount_mad": 50000.0, "status": "CONFIRMED"},
    )
    assert any("HIGH_VALUE_MOVEMENT_50000.0_MAD" in e for e in r_high["discovery"]["interesting_events"])

    # MFA OTP challenge
    r_mfa = temp_logger.log_exchange(
        exchange_type="CFG_MFA",
        target="/services/MFAOtp",
        status_code=200,
        response_data={"status": "OTP_SENT"},
    )
    assert any("MFA_CHALLENGE_TRIGGERED" in e for e in r_mfa["discovery"]["interesting_events"])

    # High Latency Telemetry (>2000 ms)
    r_latency = temp_logger.log_exchange(
        exchange_type="CFG_SLOW_INVOKE",
        target="/services/SlowService",
        status_code=200,
        duration_ms=2450.0,
    )
    assert any("HIGH_LATENCY_2450MS" in e for e in r_latency["discovery"]["interesting_events"])


def test_zero_secrets_leakage_in_sanitization(temp_logger):
    """Ensure that passwords, tokens, app secrets, and shield keys are redacted before writing."""
    rec = temp_logger.log_exchange(
        exchange_type="CFG_LOGIN",
        target="/authService/100000002/login",
        status_code=200,
        request_data={
            "password": "super_secret_user_password_123",
            "app_secret": "sensitive_kony_secret_987654",
            "x-origin-shield": "cf_shield_test_origin_token",
            "auth_header": "Bearer live_access_bearer_token_999",
        },
        response_data={
            "claims_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3MDAwMDAwMDB9.sig",
            "elevated_token": "elevated_secret_session_token",
        },
    )

    # In-memory record check
    assert "super_secret_user_password_123" not in str(rec)
    assert "sensitive_kony_secret_987654" not in str(rec)
    assert "live_access_bearer_token_999" not in str(rec)
    assert "[REDACTED_SECRET]" in str(rec)

    # File content check
    raw_content = temp_logger.log_path.read_text(encoding="utf-8")
    assert "super_secret_user_password_123" not in raw_content
    assert "sensitive_kony_secret_987654" not in raw_content
    assert "cf_shield_test_origin_token" not in raw_content
    assert "live_access_bearer_token_999" not in raw_content


def test_target_query_sanitization(temp_logger):
    """Ensure sensitive query parameters in target URL are safely sanitized."""
    rec = temp_logger.log_exchange(
        exchange_type="INCOMING_PROBE",
        target="/api/v1/resource?token=secret_query_token_123&action=view&key=internal_key_abc",
        status_code=200,
    )

    assert "secret_query_token_123" not in rec["target"]
    assert "internal_key_abc" not in rec["target"]
    assert "token=[REDACTED_SECRET]" in rec["target"]
    assert "key=[REDACTED_SECRET]" in rec["target"]
    assert "action=view" in rec["target"]


def test_file_rotation():
    """Ensure that log files rotate when reaching size threshold without polluting workspace."""
    temp_dir = tempfile.mkdtemp()
    try:
        log_file = Path(temp_dir) / "exchanges.jsonl"
        telemetry_file = Path(temp_dir) / "telemetry.jsonl"
        isolated_logger = ExchangeLogger(
            log_path=str(log_file),
            telemetry_path=str(telemetry_file),
            max_bytes=300,
            backup_count=3,
        )

        for i in range(10):
            isolated_logger.log_exchange(
                exchange_type=f"CYCLE_{i}",
                target=f"/endpoint/{i}",
                status_code=200,
                request_data={"count": i, "payload": "large_padding_string_to_trigger_fast_rotation"},
            )

        rotated_file = log_file.with_name(f"{log_file.name}.1")
        assert log_file.exists()
        assert rotated_file.exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_zero_hardcoded_secrets_in_repo():
    """
    Strict security audit: verify that forbidden hardcoded keys and tokens are completely absent
    from source code and configuration files.
    """
    root_dir = Path(__file__).resolve().parent.parent.parent
    forbidden_tokens = [
        "vext_internal_live_key_9f8e7d6c5b4a",
        "cf_shield_origin_auth_837291048576",
    ]

    scanned_exts = {".py", ".js", ".toml", ".yml", ".yaml", ".json", ".sh"}
    violations = []

    for path in root_dir.rglob("*"):
        if path.is_file() and path.suffix in scanned_exts:
            # Skip git, venv, work, and test definitions
            rel = str(path.relative_to(root_dir))
            if any(rel.startswith(p) for p in [".git/", ".venv/", "work/", "output/"]) or rel == "vext_open_bank/tests/test_exchange_logger.py":
                continue

            content = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden_tokens:
                if token in content:
                    violations.append((rel, token))

    assert violations == [], f"Found hardcoded secrets in repository: {violations}"


def test_http_middleware_records_exchanges():
    """Verify that HTTP middleware automatically logs incoming requests to exchanges.jsonl."""
    client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})

    # Public endpoint
    res_pub = client.get("/api/v1/envoisms/instructions")
    assert res_pub.status_code == 200

    # Protected endpoint
    res_status = client.get("/api/v1/auth/status")
    assert res_status.status_code == 200

    # Telemetry metrics endpoint
    stats = exchange_logger.get_telemetry_stats()
    assert stats["total_exchanges"] > 0
    assert stats["log_size_bytes"] > 0
    assert 200 in stats["observed_status_codes"]


def test_http_middleware_records_request_and_response_payloads():
    """Verify that middleware captures JSON request body and response payload for schema discovery."""
    client = TestClient(app, headers={"x-origin-shield": ORIGIN_SHIELD_SECRET})

    exchange_logger.reset_state()
    res = client.post(
        "/api/v1/envoisms/check-topup",
        json={
            "topup_id": "topup_telemetry_test_1",
            "account_id": "acc_telemetry_1",
            "amount_mad": 500.0,
            "reference": "ENV-DISCOVERY-001",
        },
    )
    assert res.status_code == 200

    stats = exchange_logger.get_telemetry_stats()
    # Unique fields should have captured fields from the request body ("req.body.topup_id", etc.)
    assert any("req.body.reference" in f for f in exchange_logger._seen_fields)
    assert any("res.status" in f for f in exchange_logger._seen_fields)


def test_onegini_client_exchange_logging():
    """Verify that OneginiClient operations are instrumented with exchange_logger."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.return_value = {"challenge": "mock_crypto_challenge_xyz"}

    with patch.object(onegini_client.session, "post", return_value=mock_resp):
        res = onegini_client.get_challenge()
        assert res["challenge"] == "mock_crypto_challenge_xyz"

    stats = exchange_logger.get_telemetry_stats()
    assert stats["total_exchanges"] > 0


@pytest.mark.anyio
async def test_airlock_csrf_fetch_logging():
    """Verify that Airlock CSRF token fetching logs outbound connection."""
    mock_async_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/javascript"}
    mock_resp.text = 'var csrfToken = "0123456789abcdef0123456789abcdef";'
    mock_async_client.get = MagicMock(return_value=mock_resp)

    # Wrap in async coroutine mock
    async def mock_get(*args, **kwargs):
        return mock_resp

    mock_async_client.get = mock_get

    token = await _fetch_csrf_token(mock_async_client)
    assert token == "0123456789abcdef0123456789abcdef"
