"""
data_store.py
Write-through cache: query live MobileFabric first, SQLite as fallback.

When a live MobileFabric call succeeds the response is persisted into the
local SQLite tables so the next read can survive gateway outages.
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from vext_open_bank.core.config import settings

logger = logging.getLogger("DataStore")


class DataStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.LOCAL_STORAGE_PATH

    # ------------------------------------------------------------------
    # Raw SQLite helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database not found at {self.db_path}")
        return sqlite3.connect(self.db_path)

    def get_raw_value(self, key: str) -> Optional[bytes]:
        try:
            with self._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM localstorage WHERE key = ?", (key,))
                row = c.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def _write_cache(self, key: str, value: Any) -> None:
        """Persist a JSON-serialisable value to the SQLite cache."""
        try:
            with self._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "CREATE TABLE IF NOT EXISTS vext_cache "
                    "(key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                c.execute(
                    "INSERT INTO vext_cache (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (key, json.dumps(value)),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Cache write failed for key=%s: %s", key, exc)

    def _read_cache(self, key: str) -> Optional[Any]:
        """Read a previously cached JSON value."""
        try:
            with self._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM vext_cache WHERE key = ?", (key,))
                row = c.fetchone()
                return json.loads(row[0]) if row else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Write-through fetch helpers
    # ------------------------------------------------------------------

    def _live_fetch(self, service_path: str, params: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Attempt a live MobileFabric call.  Returns the parsed JSON on success,
        or None on any failure (rate-limit, network error, missing credentials).
        """
        try:
            from vext_open_bank.services.mobilefabric import mf_client  # local import to avoid circular
            return mf_client.invoke_service(service_path, params=params)
        except Exception as exc:
            logger.warning("Live MF fetch failed (%s): %s", service_path, exc)
            return None

    # ------------------------------------------------------------------
    # Public accessors (write-through)
    # ------------------------------------------------------------------

    def get_account_details(self) -> Dict[str, Any]:
        """
        Returns live CFGAccountOverview data when available; falls back to
        SQLite cache, then to static defaults.
        """
        CACHE_KEY = "account_details"
        live = self._live_fetch("CFGAccountOverview/AccountOverview")
        if live:
            self._write_cache(CACHE_KEY, live)
            return live

        cached = self._read_cache(CACHE_KEY)
        if cached:
            return cached

        # Static fallback (no real credentials embedded)
        return {
            "account_name": "Compte courant",
            "account_type": "COURANT",
            "currency": "MAD",
            "balance": 0.0,
            "balance_display": "0,00 MAD",
            "open_date": "",
            "rib": os.environ.get("FALLBACK_ACCOUNT_RIB", "000000000000000000000000"),
            "formatted_rib": "",
            "bank_code": "",
            "branch_code": "",
            "account_number": "",
            "rib_key": "",
            "business_partner_id": "",
            "position_id": "",
            "contract_id": "",
            "available_for_wallet": False,
            "rib_available": False,
            "statement_available": False,
        }

    def get_user_profile(self) -> Dict[str, Any]:
        """
        Returns live customer profile data, with static fallback.
        Profile is not fetched from MF on every call (privacy / rate-limit
        sensitivity); override via VEXT_PROFILE_JSON env var if needed.
        """
        env_profile = os.environ.get("VEXT_PROFILE_JSON", "")
        if env_profile:
            try:
                return json.loads(env_profile)
            except Exception:
                pass

        CACHE_KEY = "user_profile"
        cached = self._read_cache(CACHE_KEY)
        if cached:
            return cached

        return {
            "client_name": os.environ.get("CLIENT_NAME", ""),
            "client_code": os.environ.get("CLIENT_CODE", ""),
            "user_id": os.environ.get("USER_ID", ""),
            "email": os.environ.get("CLIENT_EMAIL", ""),
            "phone": os.environ.get("CLIENT_PHONE", ""),
            "address": {},
            "relationship_manager": {},
            "branch": {},
            "sponsorship_url": "",
        }

    def get_cards(self) -> List[Dict[str, Any]]:
        """Return cached or static card list."""
        CACHE_KEY = "cards"
        cached = self._read_cache(CACHE_KEY)
        if cached:
            return cached
        return []

    def get_pfm_categories(self) -> List[Dict[str, str]]:
        """
        Return PFM expense categories — live from CFGDataServices if available,
        otherwise from cache / static list.
        """
        CACHE_KEY = "pfm_categories"
        live = self._live_fetch("CFGDataServices/cfgPFMDonut")
        if live and "categories" in live:
            categories = live["categories"]
            self._write_cache(CACHE_KEY, categories)
            return categories

        cached = self._read_cache(CACHE_KEY)
        if cached:
            return cached

        return [
            {"code": "alimentation", "name": "Alimentation", "icon": "food"},
            {"code": "restaurant", "name": "Restaurant", "icon": "dining"},
            {"code": "rent", "name": "Loyer", "icon": "home"},
            {"code": "house", "name": "Maison", "icon": "house"},
            {"code": "hobbies", "name": "Loisirs", "icon": "leisure"},
            {"code": "school", "name": "Ecole", "icon": "education"},
            {"code": "kids", "name": "Enfants", "icon": "family"},
            {"code": "pets", "name": "Animaux", "icon": "pets"},
            {"code": "tax", "name": "Impôts & Taxes", "icon": "finance"},
            {"code": "medical", "name": "Santé", "icon": "health"},
            {"code": "other_expenses", "name": "Autres dépenses", "icon": "more"},
        ]


data_store = DataStore()
