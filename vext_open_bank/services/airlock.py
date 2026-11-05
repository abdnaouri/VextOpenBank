"""
airlock.py
Async Airlock IAM Web Portal client with background session keep-alive.

Maintains a persistent AL_SESS-S cookie and rotating CSRF token so that
MobileFabric tokens can be minted without touching hardware keystores.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, Optional

import httpx
import urllib3
from bs4 import BeautifulSoup

from vext_open_bank.core.config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("AirlockService")

# ---------------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------------
_session_state: Dict[str, Any] = {
    "al_sess": None,
    "csrf_token": None,
    "refreshed_at": 0.0,
}

KEEPALIVE_INTERVAL_SECONDS = 240  # 4 minutes


# ---------------------------------------------------------------------------
# Synchronous helper (used by legacy routes_auth.py)
# ---------------------------------------------------------------------------

class AirlockWebClient:
    """
    Direct web-based client connecting to the Airlock IAM portal.
    Uses environment-sourced URLs — no hardcoded hostnames.
    """

    @property
    def _access_url(self) -> str:
        base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
        return f"{base}/auth/ui/app/auth/application/access?lang=fr"

    @property
    def _csrft_url(self) -> str:
        base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
        return f"{base}/CSRFT759.js"

    def check_portal_status(self) -> Dict[str, Any]:
        """
        Check if the web portal is reachable and extract CSRF/session cookies.
        """
        import requests

        sess = requests.Session()
        sess.verify = False
        if settings.PROXY_URL:
            sess.proxies = {"http": settings.PROXY_URL, "https": settings.PROXY_URL}
        sess.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            }
        )
        resp = sess.get(self._access_url, timeout=10)
        cookies = dict(sess.cookies)
        soup = BeautifulSoup(resp.text, "html.parser")
        has_loader = bool(soup.find("div", {"id": "iamLoader"}))
        has_loginapp = bool(soup.find("iam-loginapp"))

        return {
            "status_code": resp.status_code,
            "final_url": str(resp.url),
            "cookies": list(cookies.keys()),
            "airlock_iam_ready": has_loader and has_loginapp,
            "server": resp.headers.get("server", "unknown"),
            "location": resp.headers.get("x-volterra-location", "unknown"),
        }


airlock_web = AirlockWebClient()


# ---------------------------------------------------------------------------
# Async keep-alive worker (called from FastAPI lifespan)
# ---------------------------------------------------------------------------

async def _fetch_csrf_token(client: httpx.AsyncClient) -> Optional[str]:
    """Parse the rotating CSRF token from /CSRFT759.js."""
    try:
        base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
        resp = await client.get(f"{base}/CSRFT759.js", timeout=10)
        # Token is typically: var csrfToken = "abc123";
        match = re.search(r'["\']([A-Za-z0-9_\-]{16,})["\']', resp.text)
        return match.group(1) if match else None
    except Exception as exc:
        logger.warning("CSRF fetch failed: %s", exc)
        return None


async def _keepalive_once(client: httpx.AsyncClient) -> None:
    """Perform one keep-alive poll and refresh stored session state."""
    base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
    try:
        resp = await client.get(
            f"{base}/protected/self-service/flows/", timeout=10
        )
        csrf = await _fetch_csrf_token(client)
        al_sess = client.cookies.get("AL_SESS-S")
        _session_state.update(
            {
                "al_sess": al_sess,
                "csrf_token": csrf,
                "refreshed_at": time.time(),
            }
        )
        logger.debug(
            "Airlock keep-alive OK — status=%s AL_SESS-S=%s CSRF=%s",
            resp.status_code,
            bool(al_sess),
            bool(csrf),
        )
    except Exception as exc:
        logger.warning("Airlock keep-alive failed: %s", exc)


async def airlock_keepalive_worker() -> None:
    """
    Background async loop that refreshes the Airlock IAM session every
    KEEPALIVE_INTERVAL_SECONDS seconds.  Designed to run inside a FastAPI
    `lifespan` context manager.
    """
    proxy_map = (
        {"http://": settings.PROXY_URL, "https://": settings.PROXY_URL}
        if settings.PROXY_URL
        else None
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(
        verify=False,
        proxies=proxy_map,
        headers=headers,
        follow_redirects=True,
    ) as client:
        # Perform an initial keep-alive immediately
        await _keepalive_once(client)
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            await _keepalive_once(client)


def get_session_state() -> Dict[str, Any]:
    """Return the current in-memory Airlock session state (for injection into MF requests)."""
    return dict(_session_state)
