"""
onegini.py
Client for Onegini Token Server & Security Gateway communications.
"""

import uuid
import json
import logging
from typing import Dict, Any, Optional
import time
import requests
import urllib3
from vext_open_bank.core.config import settings
from vext_open_bank.core.exchange_logger import exchange_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("OneginiService")


class OneginiClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        if settings.PROXY_URL:
            self.session.proxies = {
                "http": settings.PROXY_URL,
                "https": settings.PROXY_URL,
            }
        self.session.headers.update(
            {
                "User-Agent": settings.USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def get_challenge(self, reg_id: Optional[str] = None) -> Dict[str, Any]:
        """Request ephemeral cryptographic challenge."""
        url = f"{settings.TOKEN_SERVER_URL}/oauth/v2/client/challenge"
        payload = {
            "registration_id": reg_id or str(uuid.uuid4()),
            "software_id": "CFGBankApp",
            "software_version": "5.5.2",
            "onegini_sdk_platform": "android",
            "onegini_sdk_platform_version": "14",
        }
        start_t = time.time()
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            duration_ms = (time.time() - start_t) * 1000.0
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {"raw_text": resp.text[:500]}

            exchange_logger.log_exchange(
                exchange_type="ONEGINI_GET_CHALLENGE",
                target="/oauth/v2/client/challenge",
                direction="OUTBOUND",
                status_code=resp.status_code,
                request_data=payload,
                response_data=resp_data,
                headers=dict(resp.headers),
                duration_ms=duration_ms,
            )
            resp.raise_for_status()
            return resp_data if isinstance(resp_data, dict) else resp.json()
        except Exception as exc:
            duration_ms = (time.time() - start_t) * 1000.0
            exchange_logger.log_exchange(
                exchange_type="ONEGINI_GET_CHALLENGE",
                target="/oauth/v2/client/challenge",
                direction="OUTBOUND",
                status_code=getattr(getattr(exc, "response", None), "status_code", 500),
                request_data=payload,
                response_data={"error": str(exc)},
                duration_ms=duration_ms,
            )
            raise

    def validate_client(self, client_assertion: str, software_statement: str) -> Dict[str, Any]:
        """Validate client credentials against Token Server."""
        url = f"{settings.TOKEN_SERVER_URL}/oauth/v3/client/validate"
        payload = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
            "software_statement": software_statement,
        }
        start_t = time.time()
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            duration_ms = (time.time() - start_t) * 1000.0
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {"raw_text": resp.text[:500]}

            exchange_logger.log_exchange(
                exchange_type="ONEGINI_VALIDATE_CLIENT",
                target="/oauth/v3/client/validate",
                direction="OUTBOUND",
                status_code=resp.status_code,
                request_data=payload,
                response_data=resp_data,
                headers=dict(resp.headers),
                duration_ms=duration_ms,
            )
            return resp_data if isinstance(resp_data, dict) else resp.json()
        except Exception as exc:
            duration_ms = (time.time() - start_t) * 1000.0
            exchange_logger.log_exchange(
                exchange_type="ONEGINI_VALIDATE_CLIENT",
                target="/oauth/v3/client/validate",
                direction="OUTBOUND",
                status_code=getattr(getattr(exc, "response", None), "status_code", 500),
                request_data=payload,
                response_data={"error": str(exc)},
                duration_ms=duration_ms,
            )
            raise

    def login_custom_idp(
        self,
        username: str,
        password: str,
        profile_id: Optional[str] = None,
        client_assertion: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Authenticate via Airlock IAM Custom Identity Provider."""
        url = f"{settings.TOKEN_SERVER_URL}/oauth/v2/custom-registration/cfg_idp_airlock_iam/complete"
        inner_data = json.dumps({"operation": "login", "password": password, "username": username})
        payload = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "profile_id": profile_id or settings.PROFILE_ID,
            "client_assertion": client_assertion,
            "data": inner_data,
            "scope": ["read"],
        }
        start_t = time.time()
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            duration_ms = (time.time() - start_t) * 1000.0
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {"raw_text": resp.text[:500]}

            exchange_logger.log_exchange(
                exchange_type="ONEGINI_LOGIN_CUSTOM_IDP",
                target="/oauth/v2/custom-registration/cfg_idp_airlock_iam/complete",
                direction="OUTBOUND",
                status_code=resp.status_code,
                request_data=payload,
                response_data=resp_data,
                headers=dict(resp.headers),
                duration_ms=duration_ms,
            )
            return resp_data if isinstance(resp_data, dict) else resp.json()
        except Exception as exc:
            duration_ms = (time.time() - start_t) * 1000.0
            exchange_logger.log_exchange(
                exchange_type="ONEGINI_LOGIN_CUSTOM_IDP",
                target="/oauth/v2/custom-registration/cfg_idp_airlock_iam/complete",
                direction="OUTBOUND",
                status_code=getattr(getattr(exc, "response", None), "status_code", 500),
                request_data=payload,
                response_data={"error": str(exc)},
                duration_ms=duration_ms,
            )
            raise


onegini_client = OneginiClient()
