"""
exchange_logger.py
High-performance, persistent structured exchange & discovery logger for VextOpenBank.

Captures all inbound and outbound connections (CFG Bank MobileFabric, Airlock IAM,
Onegini, EnvoiSMS bridge, and HTTP API middleware), extracts telemetry, identifies
previously unseen schema fields and parameters, detects anomalies/interesting events,
and appends structured JSONL records to disk with rotating file protection.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

logger = logging.getLogger("ExchangeLogger")

# Configurable log paths & rotation parameters
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "work" / "logs"
DEFAULT_LOG_PATH = DEFAULT_LOG_DIR / "exchanges.jsonl"
TELEMETRY_LOG_PATH = DEFAULT_LOG_DIR / "telemetry.jsonl"

MAX_BYTES_BEFORE_ROTATION = 10 * 1024 * 1024  # 10 MB
MAX_BACKUP_COUNT = 5

SENSITIVE_KEY_PATTERNS = re.compile(
    r"(password|secret|app_secret|authorization|x-origin-shield|x-vext-key|elevated_token|claims_token|api_key|token|auth)",
    re.IGNORECASE,
)


class ExchangeLogger:
    """
    Thread-safe structured exchange logger with automatic schema discovery,
    telemetry anomaly detection, and size-capped JSONL log rotation.
    """

    def __init__(
        self,
        log_path: Optional[str] = None,
        telemetry_path: Optional[str] = None,
        max_bytes: int = MAX_BYTES_BEFORE_ROTATION,
        backup_count: int = MAX_BACKUP_COUNT,
    ) -> None:
        raw_path = log_path or os.environ.get("EXCHANGES_LOG_PATH")
        self.log_path = Path(raw_path) if raw_path else DEFAULT_LOG_PATH
        if telemetry_path:
            self.telemetry_path = Path(telemetry_path)
        elif raw_path:
            self.telemetry_path = self.log_path.parent / "telemetry.jsonl"
        else:
            self.telemetry_path = TELEMETRY_LOG_PATH
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()

        # Telemetry & Discovery trackers
        self._seen_fields: Set[str] = set()
        self._seen_headers: Set[str] = set()
        self._seen_status_codes: Set[int] = set()
        self._seen_endpoints: Set[str] = set()
        self._total_exchanges: int = 0
        self._total_discoveries: int = 0
        self._new_fields_history: List[str] = []

        # Ensure destination directory exists
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug("Failed to create log directory: %s", exc)

    def _rotate_if_needed(self, target_file: Path) -> None:
        """Rotate target_file if it exceeds self.max_bytes."""
        try:
            if not target_file.exists():
                return
            if target_file.stat().st_size < self.max_bytes:
                return

            for i in range(self.backup_count - 1, 0, -1):
                src = target_file.with_name(f"{target_file.name}.{i}")
                dst = target_file.with_name(f"{target_file.name}.{i + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)

            backup1 = target_file.with_name(f"{target_file.name}.1")
            if backup1.exists():
                backup1.unlink()
            target_file.rename(backup1)
        except Exception as exc:
            logger.debug("Log rotation encountered error: %s", exc)

    def sanitize_target(self, target: str) -> str:
        """Redact sensitive query parameters in target URL/endpoint."""
        if not target or "?" not in target:
            return target
        try:
            base, query = target.split("?", 1)
            sanitized_query = re.sub(
                r"(^|[?&])(password|secret|key|token|auth|app_secret|claims_token|api_key)=([^&#]*)",
                r"\1\2=[REDACTED_SECRET]",
                query,
                flags=re.IGNORECASE,
            )
            return f"{base}?{sanitized_query}"
        except Exception:
            return target

    def sanitize(self, data: Any, depth: int = 0, max_depth: int = 6) -> Any:
        """
        Recursively redact secrets and truncate large binary blobs.
        Guarantees zero internal secrets appear in persistent log files.
        """
        if depth > max_depth:
            return "[MAX_DEPTH_REACHED]"

        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                str_key = str(k)
                if SENSITIVE_KEY_PATTERNS.search(str_key):
                    if isinstance(v, str) and len(v) > 8:
                        sanitized[str_key] = f"{v[:4]}...[REDACTED_SECRET]"
                    else:
                        sanitized[str_key] = "[REDACTED_SECRET]"
                else:
                    sanitized[str_key] = self.sanitize(v, depth + 1, max_depth)
            return sanitized

        elif isinstance(data, (list, tuple)):
            # Cap array length to avoid massive log lines
            if len(data) > 50:
                sample = [self.sanitize(x, depth + 1, max_depth) for x in data[:50]]
                sample.append(f"[TRUNCATED_{len(data) - 50}_MORE_ITEMS]")
                return sample
            return [self.sanitize(x, depth + 1, max_depth) for x in data]

        elif isinstance(data, (bytes, bytearray)):
            return f"[BINARY_BLOB_{len(data)}_BYTES]"

        elif isinstance(data, str):
            if data.lower().startswith("bearer ") and len(data) > 12:
                return f"Bearer {data[7:11]}...[REDACTED_SECRET]"
            if len(data) > 2048:
                return f"{data[:256]}...[TRUNCATED_{len(data)}_CHARS]"

        return data

    def extract_field_paths(self, data: Any, prefix: str = "", depth: int = 0, max_depth: int = 6) -> Set[str]:
        """Recursively extract all nested dotted field paths from payload."""
        if depth > max_depth:
            return set()

        paths: Set[str] = set()
        if isinstance(data, dict):
            for k, v in data.items():
                curr = f"{prefix}.{k}" if prefix else str(k)
                paths.add(curr)
                paths.update(self.extract_field_paths(v, curr, depth + 1, max_depth))
        elif isinstance(data, (list, tuple)):
            if prefix:
                paths.add(f"{prefix}[]")
            for item in data[:5]:  # sample up to first 5 items for keys
                if isinstance(item, (dict, list, tuple)):
                    paths.update(self.extract_field_paths(item, f"{prefix}[]", depth + 1, max_depth))
        return paths

    def inspect_exchange(
        self,
        target: str,
        status_code: Optional[int],
        headers: Optional[Dict[str, Any]],
        request_data: Any,
        response_data: Any,
        duration_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze connection exchange for:
        1. Newly discovered schema fields/parameters
        2. Newly discovered headers
        3. Newly observed status codes
        4. Newly observed endpoint targets
        5. Interesting telemetry flags (rate limiting, MFA challenge, errors, latency, financial triggers)
        """
        new_fields: List[str] = []
        new_headers: List[str] = []
        is_new_status = False
        is_new_endpoint = False
        interesting_events: List[str] = []

        # 1. Inspect Endpoint Target
        clean_target = target.split("?")[0] if target else ""
        if clean_target and clean_target not in self._seen_endpoints:
            is_new_endpoint = True
            self._seen_endpoints.add(clean_target)
            interesting_events.append(f"NEW_ENDPOINT_{clean_target}")

        # 2. Inspect Status Code
        if status_code is not None:
            if status_code not in self._seen_status_codes:
                is_new_status = True
                self._seen_status_codes.add(status_code)
                interesting_events.append(f"NEW_STATUS_CODE_{status_code}")

            if status_code == 429:
                interesting_events.append("RATE_LIMITED_429")
            elif status_code == 401:
                interesting_events.append("UNAUTHORIZED_401")
            elif status_code == 403:
                interesting_events.append("FORBIDDEN_403")
            elif status_code >= 500:
                interesting_events.append(f"SERVER_ERROR_{status_code}")

        # 3. Inspect Headers
        if headers and isinstance(headers, dict):
            for h in headers.keys():
                hl = str(h).lower()
                if hl not in self._seen_headers:
                    self._seen_headers.add(hl)
                    new_headers.append(hl)

                # Check for interesting telemetry headers
                if hl == "retry-after":
                    interesting_events.append(f"RETRY_AFTER_HEADER_{headers[h]}")
                elif hl == "cf-ray":
                    interesting_events.append("CLOUDFLARE_EDGE_RAY_DETECTED")
                elif hl.startswith("x-volterra-"):
                    interesting_events.append(f"VOLTERRA_WAF_{hl}")

        # 4. Inspect Payload Schemas (Request + Response)
        req_paths = self.extract_field_paths(request_data, prefix="req")
        resp_paths = self.extract_field_paths(response_data, prefix="res")
        all_paths = req_paths | resp_paths

        for p in sorted(all_paths):
            if p not in self._seen_fields:
                self._seen_fields.add(p)
                new_fields.append(p)
                self._new_fields_history.append(p)

        # 5. Telemetry Events in Response / Metadata
        if isinstance(response_data, dict):
            # Kony MobileFabric opstatus detection
            opstatus = response_data.get("opstatus")
            if opstatus is not None and opstatus != 0:
                interesting_events.append(f"MF_OPSTATUS_{opstatus}")

            if "errmsg" in response_data or "errorMessage" in response_data:
                err_text = response_data.get("errmsg") or response_data.get("errorMessage")
                interesting_events.append(f"ERROR_MESSAGE_{err_text}")

            if "detail" in response_data and isinstance(response_data["detail"], str):
                interesting_events.append(f"ERROR_DETAIL_{response_data['detail'][:60]}")

            # MFA Elevation & Challenge
            if response_data.get("status") in ("CHALLENGE_SENT", "OTP_SENT"):
                interesting_events.append("MFA_CHALLENGE_TRIGGERED")
            if "elevated_token" in response_data or "claims_token" in response_data:
                interesting_events.append("TOKEN_ISSUANCE_DETECTED")

            # High-value transaction telemetry
            amount = response_data.get("amount") or response_data.get("amount_mad")
            if isinstance(amount, (int, float)) and amount >= 10000.0:
                interesting_events.append(f"HIGH_VALUE_MOVEMENT_{amount}_MAD")

            # Reconciliation conflicts / statuses
            status_val = response_data.get("status")
            if status_val in ("CONFLICT", "UNMATCHED"):
                interesting_events.append(f"RECONCILIATION_{status_val}")

        # Latency anomaly (> 2000 ms)
        if duration_ms is not None and duration_ms > 2000.0:
            interesting_events.append(f"HIGH_LATENCY_{int(duration_ms)}MS")

        # Decoy / Untrusted probe detection
        if metadata and metadata.get("trusted") is False:
            interesting_events.append("DECOY_UNTRUSTED_PROBE")

        has_discovery = bool(new_fields or new_headers or is_new_status or is_new_endpoint or interesting_events)
        if has_discovery:
            self._total_discoveries += 1

        return {
            "has_new_data": has_discovery,
            "new_fields": new_fields,
            "new_headers": new_headers,
            "is_new_status": is_new_status,
            "is_new_endpoint": is_new_endpoint,
            "interesting_events": interesting_events,
        }

    def log_exchange(
        self,
        exchange_type: str,
        target: str,
        direction: str = "OUTBOUND",
        status_code: Optional[int] = None,
        request_data: Any = None,
        response_data: Any = None,
        headers: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record and persist a connection exchange with safe sanitization and telemetry discovery.
        Guaranteed non-crashing: failures are caught safely.
        """
        try:
            with self._lock:
                self._total_exchanges += 1

                sanitized_target = self.sanitize_target(target)

                # Discovery analysis
                discovery = self.inspect_exchange(
                    target=sanitized_target,
                    status_code=status_code,
                    headers=headers,
                    request_data=request_data,
                    response_data=response_data,
                    duration_ms=duration_ms,
                    metadata=metadata,
                )

                # Sanitization
                sanitized_req = self.sanitize(request_data)
                sanitized_resp = self.sanitize(response_data)
                sanitized_headers = self.sanitize(headers) if headers else None

                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "exchange_id": f"exc_{int(time.time() * 1000)}_{self._total_exchanges}",
                    "exchange_type": exchange_type,
                    "target": sanitized_target,
                    "direction": direction,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
                    "discovery": discovery,
                    "headers": sanitized_headers,
                    "request": sanitized_req,
                    "response": sanitized_resp,
                    "metadata": metadata or {},
                    # Compatibility fields for offline replay harness
                    "url": sanitized_target,
                    "verb": (metadata or {}).get("method", "POST" if direction == "OUTBOUND" else "GET"),
                    "req_headers": sanitized_headers,
                    "req_body": sanitized_req,
                }

                json_line = json.dumps(record, separators=(",", ":"))

                # Write to primary exchanges log with rotation
                self._rotate_if_needed(self.log_path)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json_line + "\n")

                # If discovery was detected, also append to telemetry.jsonl for quick auditing
                if discovery["has_new_data"]:
                    self._rotate_if_needed(self.telemetry_path)
                    with open(self.telemetry_path, "a", encoding="utf-8") as tf:
                        tf.write(json_line + "\n")

                return record
        except Exception as exc:
            logger.debug("Exchange logger failed silently: %s", exc)
            return {"error": str(exc)}

    def get_telemetry_stats(self) -> Dict[str, Any]:
        """Return runtime discovery and connection telemetry metrics."""
        with self._lock:
            return {
                "total_exchanges": self._total_exchanges,
                "total_discoveries": self._total_discoveries,
                "unique_fields_count": len(self._seen_fields),
                "unique_headers_count": len(self._seen_headers),
                "observed_status_codes": sorted(list(self._seen_status_codes)),
                "log_file": str(self.log_path),
                "log_size_bytes": self.log_path.stat().st_size if self.log_path.exists() else 0,
                "recent_new_fields": self._new_fields_history[-20:],
            }

    def reset_state(self) -> None:
        """Reset in-memory discovery state (used in testing)."""
        with self._lock:
            self._seen_fields.clear()
            self._seen_headers.clear()
            self._seen_status_codes.clear()
            self._seen_endpoints.clear()
            self._total_exchanges = 0
            self._total_discoveries = 0
            self._new_fields_history.clear()


# Global singleton instance
exchange_logger = ExchangeLogger()
