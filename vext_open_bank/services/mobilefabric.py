"""
mobilefabric.py
Client for Kony MobileFabric Gateway services.
Includes standard invocation plus step-up MFA token exchange.
"""

import logging
from typing import Any, Dict, Optional

import requests
import urllib3

from vext_open_bank.core.config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("MobileFabricService")


class MobileFabricClient:
    def __init__(self) -> None:
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
            }
        )
        self.claims_token: Optional[str] = None
        self.elevated_token: Optional[str] = None  # Set after MFA step-up

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def _base_headers(self) -> Dict[str, str]:
        return {
            "X-Kony-App-Key": settings.APP_KEY,
            "X-Kony-App-Secret": settings.APP_SECRET,
            "X-Kony-DeviceId": settings.DEVICE_ID,
            "X-Kony-SDK-Type": "js",
            "X-Kony-SDK-Version": settings.SDK_VERSION,
            "X-Kony-Platform-Type": "android",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def login_claims(self) -> str:
        """
        Authenticate with MobileFabric gateway to acquire a claims authorization JWT.
        """
        url = f"{settings.MOBILEFABRIC_URL}/authService/100000002/login"
        resp = self.session.post(url, headers=self._base_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("claims_token", {}).get("value")
        if not token:
            raise ValueError("No claims_token in gateway response")
        self.claims_token = token
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
    # Service invocation
    # ------------------------------------------------------------------

    def invoke_service(
        self,
        service_path: str,
        params: Optional[Dict[str, Any]] = None,
        api_version: str = "1.0",
        elevated: bool = False,
    ) -> Dict[str, Any]:
        """
        Invoke an operation on a MobileFabric integration service.

        :param elevated: If True, use the step-up elevated token instead of
                         the standard claims token (required for mutating services).
        """
        if not self.claims_token:
            self.login_claims()

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
        resp = self.session.post(
            url, headers=headers, data=params or {}, timeout=15
        )
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
