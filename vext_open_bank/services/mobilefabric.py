"""
mobilefabric.py
Client for Kony MobileFabric Gateway services.
Includes standard invocation plus step-up MFA token exchange.
"""

import base64
import json
import logging
import random
import time
from typing import Any, Dict, Optional

import requests
import urllib3

from vext_open_bank.core.config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("MobileFabricService")


class MobileFabricClient:
    def __init__(self, enable_jitter: bool = True) -> None:
        self.session = requests.Session()
        self.session.verify = False
        if settings.PROXY_URL:
            self.session.proxies = {
                "http": settings.PROXY_URL,
                "https": settings.PROXY_URL,
            }
        # Whitelist ONLY authentic CFGBankApp Android headers - no proxy or client leakage
        self.session.headers.clear()
        self.session.headers.update(
            {
                "User-Agent": settings.USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "keep-alive",
            }
        )
        self.claims_token: Optional[str] = None
        self.claims_token_exp: float = 0.0
        self.elevated_token: Optional[str] = None  # Set after MFA step-up
        self.enable_jitter = enable_jitter

    # ------------------------------------------------------------------
    # Authentication & Session helpers
    # ------------------------------------------------------------------

    def _parse_jwt_exp(self, token: str) -> Optional[float]:
        """Extract expiration timestamp from standard JWT claims payload."""
        try:
            parts = token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
                payload = json.loads(payload_json)
                if "exp" in payload:
                    return float(payload["exp"])
        except Exception:
            pass
        return None

    def _base_headers(self) -> Dict[str, str]:
        """Generate strictly sanitized Android Kony MobileFabric request headers."""
        return {
            "X-Kony-App-Key": settings.APP_KEY,
            "X-Kony-App-Secret": settings.APP_SECRET,
            "X-Kony-DeviceId": settings.DEVICE_ID,
            "X-Kony-SDK-Type": "js",
            "X-Kony-SDK-Version": settings.SDK_VERSION,
            "X-Kony-Platform-Type": "android",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def is_token_valid(self) -> bool:
        """Check if current claims token is set and not expired (with 45s safety buffer)."""
        if not self.claims_token:
            return False
        if self.claims_token_exp > 0:
            return time.time() < (self.claims_token_exp - 45.0)
        return True

    def login_claims(self, force: bool = False) -> str:
        """
        Authenticate with MobileFabric gateway to acquire a claims authorization JWT.
        Reuses valid token to prevent excessive login churn and AI fraud triggers.
        """
        if not force and self.is_token_valid():
            return self.claims_token

        # Apply natural micro-jitter before login to emulate human interaction cadence
        if self.enable_jitter:
            time.sleep(random.uniform(0.08, 0.25))

        url = f"{settings.MOBILEFABRIC_URL}/authService/100000002/login"
        resp = self.session.post(url, headers=self._base_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("claims_token", {}).get("value")
        if not token:
            raise ValueError("No claims_token in gateway response")

        self.claims_token = token
        # Extract JWT exp or default to 15 minute TTL
        exp = self._parse_jwt_exp(token)
        self.claims_token_exp = exp if exp else (time.time() + 900.0)
        return token

    def get_service_catalog(self) -> Dict[str, Any]:
        """
        Fetch the complete service catalog from MobileFabric appconfig.
        """
        url = f"{settings.MOBILEFABRIC_URL}/authService/100000002/appconfig"
        headers = {
            "X-Kony-App-Key": settings.APP_KEY,
            "X-Kony-App-Secret": settings.APP_SECRET,
        }
        resp = self.session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Service invocation with AI-trigger mitigation & origin hygiene
    # ------------------------------------------------------------------

    def invoke_service(
        self,
        service_path: str,
        params: Optional[Dict[str, Any]] = None,
        api_version: str = "1.0",
        elevated: bool = False,
        retry_on_auth_fail: bool = True,
    ) -> Dict[str, Any]:
        """
        Invoke an operation on a MobileFabric integration service.

        Protections:
        - Origin Concealment: Zero external headers (no X-Forwarded-For / Origin / Referer).
        - AI Fraud Mitigation: Jittered timing, valid token reuse, silent single re-auth.
        """
        if not self.is_token_valid():
            self.login_claims()

        # Human-like network jitter (100ms - 250ms) to evade robotic pattern detection
        if self.enable_jitter:
            time.sleep(random.uniform(0.05, 0.18))

        auth_token = (
            self.elevated_token if elevated and self.elevated_token
            else self.claims_token
        )

        url = f"{settings.MOBILEFABRIC_URL}/services/{service_path}"
        headers = {
            "X-Kony-Authorization": auth_token,
            "X-Kony-App-Key": settings.APP_KEY,
            "X-Kony-App-Secret": settings.APP_SECRET,
            "X-Kony-DeviceId": settings.DEVICE_ID,
            "X-Kony-API-Version": api_version,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            resp = self.session.post(
                url, headers=headers, data=params or {}, timeout=15
            )
            # Detect expired session/claims
            if resp.status_code == 401 and retry_on_auth_fail:
                logger.info("Claims token expired on CFG gateway, refreshing session...")
                self.login_claims(force=True)
                return self.invoke_service(
                    service_path,
                    params=params,
                    api_version=api_version,
                    elevated=elevated,
                    retry_on_auth_fail=False,
                )
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                backoff_time = float(retry_after) if retry_after and retry_after.isdigit() else 30.0
                logger.warning("CFG gateway returned 429 Too Many Requests. Backing off for %.1fs", backoff_time)
                time.sleep(backoff_time)
                resp.raise_for_status()

            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code == 401 and retry_on_auth_fail:
                logger.info("CFG 401 received, forcing claims token refresh...")
                self.login_claims(force=True)
                return self.invoke_service(
                    service_path,
                    params=params,
                    api_version=api_version,
                    elevated=elevated,
                    retry_on_auth_fail=False,
                )
            raise
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Step-Up MFA token exchange
    # ------------------------------------------------------------------

    def trigger_mfa_challenge(self, channel: str = "sms") -> Dict[str, Any]:
        """
        Step 1 of MFA step-up: trigger SMS/email OTP or Entrust push.
        Calls /services/MFAOtp with 'CopyMFAOtp' as fallback.

        :param channel: 'sms', 'email', or 'entrust'
        """
        if not self.claims_token:
            self.login_claims()

        try:
            return self.invoke_service("MFAOtp", params={"channel": channel})
        except Exception:
            logger.warning("MFAOtp failed, trying CopyMFAOtp fallback")
            return self.invoke_service("CopyMFAOtp", params={"channel": channel})

    def verify_mfa_and_elevate(self, otp_code: str) -> str:
        """
        Step 2 of MFA step-up: submit OTP to AcccessTokenForMFA.
        On success, stores the short-lived elevated token and returns it.

        :param otp_code: The 6-digit OTP received by the user.
        :returns: The elevated claims_token value.
        """
        if not self.claims_token:
            self.login_claims()

        result = self.invoke_service(
            "AcccessTokenForMFA",
            params={"otp": otp_code},
        )
        elevated = (
            result.get("claims_token", {}).get("value")
            or result.get("elevated_token")
        )
        if not elevated:
            raise ValueError(
                "AcccessTokenForMFA did not return an elevated token. "
                f"Response: {result}"
            )
        self.elevated_token = elevated
        return elevated

    def validate_token(self, token: str) -> Dict[str, Any]:
        """Call CFGValidateToken to check whether a claims token is still valid."""
        return self.invoke_service(
            "CFGValidateToken", params={"token": token}
        )

    def logout(self) -> Dict[str, Any]:
        """Invalidate the current session via _internal_logout."""
        try:
            result = self.invoke_service("_internal_logout")
        finally:
            self.claims_token = None
            self.elevated_token = None
        return result

    def get_microsoft_token(self) -> Dict[str, Any]:
        """Obtain a MicrosoftToken for Dynamics CRM integration."""
        return self.invoke_service("MicrosoftToken")

    def get_access_token1(self) -> Dict[str, Any]:
        """Obtain an AccessToken1 (secondary auth flow)."""
        return self.invoke_service("AccessToken1")


mf_client = MobileFabricClient()
