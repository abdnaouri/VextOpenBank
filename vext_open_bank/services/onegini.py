"""
onegini.py
Client for Onegini Token Server & Security Gateway communications.
"""

import uuid
import json
import logging
from typing import Dict, Any, Optional
import requests
import urllib3
from vext_open_bank.core.config import settings

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
        resp = self.session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def validate_client(self, client_assertion: str, software_statement: str) -> Dict[str, Any]:
        """Validate client credentials against Token Server."""
        url = f"{settings.TOKEN_SERVER_URL}/oauth/v3/client/validate"
        payload = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
            "software_statement": software_statement,
        }
        resp = self.session.post(url, json=payload, timeout=10)
        return resp.json()

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
        resp = self.session.post(url, json=payload, timeout=10)
        return resp.json()


onegini_client = OneginiClient()
