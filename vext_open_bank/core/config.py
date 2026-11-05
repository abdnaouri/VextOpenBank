"""
Configuration settings for VextOpenBank API.
All sensitive values MUST be supplied via environment variables.
No production credentials are stored in this file.
"""

import json
import os
import uuid
from pydantic import BaseModel


def _load_env_file() -> None:
    """Zero-dependency .env loader to ensure persistent configuration across restarts."""
    possible_paths = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"),
    ]
    for env_path in possible_paths:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k not in os.environ:
                                os.environ[k] = v
                break
            except Exception:
                pass


_load_env_file()


def _get_persistent_device_profile() -> dict:
    """
    Ensure DEVICE_ID, CLIENT_ID, and PROFILE_ID remain 100% constant across restarts
    so CFG Bank never observes device profile churn.
    """
    profile_path = os.environ.get("DEVICE_PROFILE_PATH", "/tmp/vext_persistent_device_profile.json")
    cached = {}
    if os.path.exists(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}

    device_id = os.environ.get("DEVICE_ID") or cached.get("DEVICE_ID") or str(uuid.uuid4())
    client_id = os.environ.get("CLIENT_ID") or cached.get("CLIENT_ID") or f"cli_{device_id[:8]}"
    profile_id = os.environ.get("PROFILE_ID") or cached.get("PROFILE_ID") or f"prof_{device_id[9:17]}"

    # Persist back if not already stored
    if not os.path.exists(profile_path) or cached.get("DEVICE_ID") != device_id:
        try:
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump({"DEVICE_ID": device_id, "CLIENT_ID": client_id, "PROFILE_ID": profile_id}, f)
        except Exception:
            pass

    return {"DEVICE_ID": device_id, "CLIENT_ID": client_id, "PROFILE_ID": profile_id}


_device_prof = _get_persistent_device_profile()


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

    # Persistent Device Hardware Profile — guaranteed stable across container restarts
    DEVICE_ID: str = _device_prof["DEVICE_ID"]
    CLIENT_ID: str = _device_prof["CLIENT_ID"]
    PROFILE_ID: str = _device_prof["PROFILE_ID"]
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
