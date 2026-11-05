"""
Configuration settings for VextOpenBank API.
All sensitive values MUST be supplied via environment variables.
No production credentials are stored in this file.
"""

import os
from pydantic import BaseModel


class Settings(BaseModel):
    # Service Info
    PROJECT_NAME: str = "VextOpenBank API"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api/v1"

    # Gateways — override via environment variables
    TOKEN_SERVER_URL: str = os.environ.get(
        "TOKEN_SERVER_URL", "https://mbregister.example.com"
    )
    MOBILEFABRIC_URL: str = os.environ.get(
        "MOBILEFABRIC_URL", "https://cfgmbapp.example.com:8443"
    )
    WEB_PORTAL_URL: str = os.environ.get(
        "WEB_PORTAL_URL", "https://clients.example.com"
    )
    AIRLOCK_PORTAL_URL: str = os.environ.get(
        "AIRLOCK_PORTAL_URL", "https://clients.example.com"
    )

    # Proxy — set PROXY_URL="" to disable
    PROXY_URL: str = os.environ.get("PROXY_URL", "")

    # Kony MobileFabric App Credentials — REQUIRED in production
    APP_KEY: str = os.environ.get("MOBILEFABRIC_APP_KEY", "")
    APP_SECRET: str = os.environ.get("MOBILEFABRIC_APP_SECRET", "")
    APP_ID: str = os.environ.get("MOBILEFABRIC_APP_ID", "")
    BASE_ID: str = os.environ.get("MOBILEFABRIC_BASE_ID", "")
    APP_NAME: str = os.environ.get("MOBILEFABRIC_APP_NAME", "CFGRetailBanking")
    SDK_VERSION: str = os.environ.get("MOBILEFABRIC_SDK_VERSION", "9.7.52")

    # Device Hardware Profile — REQUIRED in production
    DEVICE_ID: str = os.environ.get("DEVICE_ID", "")
    CLIENT_ID: str = os.environ.get("CLIENT_ID", "")
    PROFILE_ID: str = os.environ.get("PROFILE_ID", "")
    USER_AGENT: str = os.environ.get(
        "USER_AGENT",
        "CFGBankApp/5.5.2 (Linux; U; Android 14; KB2003 Build/UKQ1.230924.001)",
    )

    # Airlock IAM credentials — REQUIRED for session keep-alive
    AIRLOCK_USERNAME: str = os.environ.get("AIRLOCK_USERNAME", "")
    AIRLOCK_PASSWORD: str = os.environ.get("AIRLOCK_PASSWORD", "")

    # Local Storage Database
    LOCAL_STORAGE_PATH: str = os.environ.get("LOCAL_STORAGE_PATH", "/tmp/localstorage.db")


settings = Settings()
