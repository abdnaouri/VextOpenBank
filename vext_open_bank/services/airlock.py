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
from vext_open_bank.core.exchange_logger import exchange_logger

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
        start_t = time.time()
        try:
            resp = sess.get(self._access_url, timeout=10)
            duration_ms = (time.time() - start_t) * 1000.0
            cookies = dict(sess.cookies)
            soup = BeautifulSoup(resp.text, "html.parser")
            has_loader = bool(soup.find("div", {"id": "iamLoader"}))
            has_loginapp = bool(soup.find("iam-loginapp"))

            res = {
                "status_code": resp.status_code,
                "final_url": str(resp.url),
                "cookies": list(cookies.keys()),
                "airlock_iam_ready": has_loader and has_loginapp,
                "server": resp.headers.get("server", "unknown"),
                "location": resp.headers.get("x-volterra-location", "unknown"),
            }

            exchange_logger.log_exchange(
                exchange_type="AIRLOCK_PORTAL_CHECK",
                target=self._access_url,
                direction="OUTBOUND",
                status_code=resp.status_code,
                response_data=res,
                headers=dict(resp.headers),
                duration_ms=duration_ms,
            )

            return res
        except Exception as exc:
            duration_ms = (time.time() - start_t) * 1000.0
            exchange_logger.log_exchange(
                exchange_type="AIRLOCK_PORTAL_CHECK",
                target=self._access_url,
                direction="OUTBOUND",
                status_code=getattr(getattr(exc, "response", None), "status_code", 500),
                response_data={"error": str(exc)},
                duration_ms=duration_ms,
            )
            raise


airlock_web = AirlockWebClient()


# ---------------------------------------------------------------------------
# Async keep-alive worker (called from FastAPI lifespan)
# ---------------------------------------------------------------------------

async def _fetch_csrf_token(client: httpx.AsyncClient) -> Optional[str]:
    """Parse the rotating CSRF token from /CSRFT759.js."""
    base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
    target = f"{base}/CSRFT759.js"
    start_t = time.time()
    try:
        resp = await client.get(target, timeout=10)
        duration_ms = (time.time() - start_t) * 1000.0
        # Token is typically: var csrfToken = "abc123";
        match = re.search(r'["\']([A-Za-z0-9_\-]{16,})["\']', resp.text)
        token = match.group(1) if match else None

        exchange_logger.log_exchange(
            exchange_type="AIRLOCK_CSRF_FETCH",
            target=target,
            direction="OUTBOUND",
            status_code=resp.status_code,
            response_data={"csrf_token_found": bool(token)},
            headers=dict(resp.headers),
            duration_ms=duration_ms,
        )
        return token
    except Exception as exc:
        duration_ms = (time.time() - start_t) * 1000.0
        exchange_logger.log_exchange(
            exchange_type="AIRLOCK_CSRF_FETCH",
            target=target,
            direction="OUTBOUND",
            status_code=getattr(getattr(exc, "response", None), "status_code", 500),
            response_data={"error": str(exc)},
            duration_ms=duration_ms,
        )
        logger.warning("CSRF fetch failed: %s", exc)
        return None


async def _keepalive_once(client: httpx.AsyncClient) -> None:
    """Perform one keep-alive poll and refresh stored session state."""
    base = settings.AIRLOCK_PORTAL_URL.rstrip("/")
    start_t = time.time()
    try:
        resp = await client.get(
            f"{base}/protected/self-service/flows/", timeout=10
        )
        duration_ms = (time.time() - start_t) * 1000.0
        csrf = await _fetch_csrf_token(client)
        al_sess = client.cookies.get("AL_SESS-S")
        _session_state.update(
            {
                "al_sess": al_sess,
                "csrf_token": csrf,
                "refreshed_at": time.time(),
            }
        )
        exchange_logger.log_exchange(
            exchange_type="AIRLOCK_KEEPALIVE_POLL",
            target=f"{base}/protected/self-service/flows/",
            direction="OUTBOUND",
            status_code=resp.status_code,
            response_data={"al_sess_present": bool(al_sess), "csrf_present": bool(csrf)},
            headers=dict(resp.headers),
            duration_ms=duration_ms,
        )
        logger.debug(
            "Airlock keep-alive OK — status=%s AL_SESS-S=%s CSRF=%s",
            resp.status_code,
            bool(al_sess),
            bool(csrf),
        )
    except Exception as exc:
        duration_ms = (time.time() - start_t) * 1000.0
        exchange_logger.log_exchange(
            exchange_type="AIRLOCK_KEEPALIVE_POLL",
            target=f"{base}/protected/self-service/flows/",
            direction="OUTBOUND",
            status_code=500,
            response_data={"error": str(exc)},
            duration_ms=duration_ms,
        )
        logger.warning("Airlock keep-alive failed: %s", exc)


async def airlock_keepalive_worker() -> None:
    """
    Background async loop that refreshes the Airlock IAM session every
    KEEPALIVE_INTERVAL_SECONDS seconds.  Designed to run inside a FastAPI
    `lifespan` context manager.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    client_kwargs = {
        "verify": False,
        "headers": headers,
        "follow_redirects": True,
    }
    if settings.PROXY_URL:
        # Support both httpx modern (proxy=) and legacy (proxies=)
        try:
            client_kwargs["proxy"] = settings.PROXY_URL
        except Exception:
            client_kwargs["proxies"] = {"http://": settings.PROXY_URL, "https://": settings.PROXY_URL}

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # Perform an initial keep-alive immediately
            await _keepalive_once(client)
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
                await _keepalive_once(client)
    except Exception as exc:
        logger.warning("Airlock background worker exited with: %s", exc)


def get_session_state() -> Dict[str, Any]:
    """Return the current in-memory Airlock session state (for injection into MF requests)."""
    return dict(_session_state)
