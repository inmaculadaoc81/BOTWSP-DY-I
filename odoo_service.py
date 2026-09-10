import logging
import httpx

from config import settings

logger = logging.getLogger(__name__)


class OdooService:
    """Service for creating leads in Odoo CRM via JSON-RPC."""

    def __init__(self):
        self.url = settings.ODOO_URL.rstrip("/")
        self.db = settings.ODOO_DB
        self.login = settings.ODOO_USER
        self.password = settings.ODOO_PASSWORD
        self.team_id = settings.ODOO_TEAM_ID
        self._session_cookie: str | None = None

    async def _authenticate(self) -> str:
        """Authenticate with Odoo and return session cookie."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.url}/web/session/authenticate",
                json={
                    "jsonrpc": "2.0",
                    "params": {
                        "db": self.db,
                        "login": self.login,
                        "password": self.password,
                    },
                },
            )
            resp.raise_for_status()
            cookie = resp.headers.get("set-cookie", "")
            session = cookie.split(";")[0] if cookie else ""
            self._session_cookie = session
            logger.info("Odoo authentication successful")
            return session

    async def create_lead(
        self,
        name: str,
        contact_name: str = "",
        phone: str = "",
        email: str = "",
        description: str = "",
    ) -> int | None:
        """Create a CRM lead in Odoo. Returns the lead ID or None on failure."""
        if not self._session_cookie:
            await self._authenticate()

        lead_data = {
            "name": name,
            "contact_name": contact_name,
            "phone": phone,
            "email_from": email,
            "description": description,
            "team_id": self.team_id,
        }

        try:
            lead_id = await self._call_create(lead_data)
            logger.info(f"Odoo lead created: id={lead_id}, name={name}")
            return lead_id
        except httpx.HTTPStatusError:
            # Session may have expired, retry once
            logger.warning("Odoo call failed, re-authenticating...")
            await self._authenticate()
            lead_id = await self._call_create(lead_data)
            logger.info(f"Odoo lead created (retry): id={lead_id}, name={name}")
            return lead_id
        except Exception as e:
            logger.error(f"Error creating Odoo lead: {e}", exc_info=True)
            return None

    async def _call_create(self, lead_data: dict) -> int:
        """Execute the create call to Odoo."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.url}/web/dataset/call_kw",
                headers={
                    "Content-Type": "application/json",
                    "Cookie": self._session_cookie,
                },
                json={
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "model": "crm.lead",
                        "method": "create",
                        "args": [lead_data],
                        "kwargs": {},
                    },
                },
            )
            resp.raise_for_status()
            result = resp.json().get("result")
            return result
