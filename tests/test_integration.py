"""Integration tests – exercise the full webhook → response pipeline.

These tests hit the real FastAPI endpoints via httpx.AsyncClient, with only
external I/O mocked (OpenAI, Google Sheets, WhatsApp, Chatwoot, Calendar).
The SQLite database is real (tmp_path) so we verify actual DB persistence.
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from httpx import AsyncClient, ASGITransport

from tests.conftest import make_completion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_appointment_iso() -> str:
    """A weekday 11:00 Madrid-time slot far enough ahead to stay valid (avoids
    hardcoding a date that eventually lands in the past)."""
    from datetime import datetime, timedelta
    from calendar_service import MADRID_TZ, _HOLIDAYS

    dt = (datetime.now(MADRID_TZ) + timedelta(days=14)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    while dt.weekday() >= 5 or dt.strftime("%m-%d") in _HOLIDAYS:
        dt += timedelta(days=1)
    return dt.isoformat()

def _whatsapp_webhook_body(sender: str, text: str) -> dict:
    """Build a minimal Meta WhatsApp webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _chatwoot_webhook_body(
    conversation_id: int,
    content: str,
    phone: str = "34612345678",
    contact_id: int = 42,
    labels: list | None = None,
) -> dict:
    """Build a minimal Chatwoot agent-bot webhook payload."""
    conversation: dict = {
        "id": conversation_id,
        "contact_inbox": {"source_id": phone},
    }
    if labels is not None:
        conversation["labels"] = labels
    return {
        "event": "message_created",
        "message_type": "incoming",
        "content": content,
        "content_type": "text",
        "conversation": conversation,
        "sender": {"id": contact_id, "name": "Test User", "email": "", "phone_number": phone},
    }


def _chatwoot_outgoing_body(
    conversation_id: int,
    content: str,
    phone: str = "34612345678",
    sender_type: str = "user",
    labels: list | None = None,
    template_name: str | None = None,
) -> dict:
    """Build a Chatwoot outgoing message payload (agent or n8n template)."""
    conversation: dict = {
        "id": conversation_id,
        "contact_inbox": {"source_id": phone},
    }
    if labels is not None:
        conversation["labels"] = labels
    body: dict = {
        "event": "message_created",
        "message_type": "outgoing",
        "content": content,
        "content_type": "text",
        "conversation": conversation,
        "sender": {"id": 10, "name": "Agent", "type": sender_type},
    }
    if template_name:
        body["additional_attributes"] = {
            "template_params": {"name": template_name}
        }
    return body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _isolated_db(tmp_path):
    """Replace the global Database with one backed by a temp SQLite file."""
    import main
    from database import Database

    real_db = Database()
    real_db.db_path = str(tmp_path / "integration.db")
    await real_db.init()

    original_db = main.db
    main.db = real_db
    yield real_db
    main.db = original_db
    await real_db.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the IP rate limiter between tests to avoid cross-test 429s."""
    import main
    main.limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _mock_external_services():
    """Patch every external-I/O service so nothing leaves the test process."""
    with (
        patch("main.whatsapp_svc") as wa,
        patch("main.chatwoot_svc") as cw,
        patch("main.sheets_svc") as sh,
        patch("main.espocrm_svc") as espo,
        patch("main.calendar_svc") as cal,
    ):
        wa.send_message = AsyncMock(return_value={"messages": [{"id": "msg1"}]})

        cw.send_message = AsyncMock(return_value={"id": 1})
        cw.get_contact_phone = AsyncMock(return_value="34612345678")
        cw.find_conversation_by_phone = AsyncMock(return_value=999)
        cw.handoff_to_agent = AsyncMock(return_value={"status": "open"})
        cw.assign_handoff_agent = AsyncMock(return_value=13)
        cw.get_conversation_labels = AsyncMock(return_value=[])

        sh.get_repairs_by_phone = AsyncMock(return_value=[])
        sh.get_all_prices = AsyncMock(return_value=[])
        sh.format_repairs_for_prompt = MagicMock(return_value="[REPAIRS]")
        sh.format_prices_for_prompt = MagicMock(return_value="[PRICES]")

        espo.create_lead = AsyncMock(return_value="lead-42")
        espo.schedule_lead_from_conversation = MagicMock()

        cal.get_appointment_context = MagicMock(return_value="[APPOINTMENT CONTEXT]")
        cal.create_event = AsyncMock(return_value={"id": "evt1"})

        yield {
            "whatsapp": wa,
            "chatwoot": cw,
            "sheets": sh,
            "espocrm": espo,
            "calendar": cal,
        }


@pytest.fixture
def mock_intent():
    """Patch classify_intent to return a controllable result."""
    with patch("main.classify_intent", new_callable=AsyncMock) as mock:
        from intent_classifier import IntentResult
        mock.return_value = IntentResult()
        yield mock


@pytest.fixture
def mock_openai_generate():
    """Patch openai_svc.generate_response to return a controllable string."""
    with patch("main.openai_svc") as svc:
        svc.generate_response = AsyncMock(return_value="Hola, bienvenido a Kelatos.")
        svc.client = MagicMock()
        yield svc


@pytest.fixture
async def client():
    """httpx AsyncClient wired to the FastAPI app (no real server)."""
    from main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ===========================================================================
# WhatsApp Webhook – full pipeline
# ===========================================================================


class TestWhatsAppWebhookFlow:
    """Integration: POST /webhook with a WhatsApp text message."""

    async def test_basic_text_message(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Message arrives → intent classified → AI responds → WhatsApp sends → DB saved."""
        mock_openai_generate.generate_response.return_value = "Hola, te puedo ayudar."

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # AI was called
        mock_openai_generate.generate_response.assert_called_once()

        # WhatsApp received the reply
        _mock_external_services["whatsapp"].send_message.assert_called_once_with(
            to="34600111222", text="Hola, te puedo ayudar."
        )

        # Messages persisted in DB (user + assistant)
        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    async def test_repair_intent_injects_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.needs_repair_lookup, sheets data is fetched and injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [
            {"resguardo": "R001", "equipo_modelo": "HP Pavilion", "estado": "En reparación"}
        ]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "como va mi reparacion"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repairs_by_phone.assert_called_once_with("34600111222")
        _mock_external_services["sheets"].format_repairs_for_prompt.assert_called_once()

        # extra_context passed to generate_response
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["extra_context"] is not None
        assert "[REPAIRS]" in call_kwargs["extra_context"]

    async def test_prices_intent_injects_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.needs_prices, price data is fetched and injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_prices=True)
        _mock_external_services["sheets"].get_all_prices.return_value = [{"marca": "HP"}]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "cuanto cuesta"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_all_prices.assert_called_once()
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "[PRICES]" in call_kwargs["extra_context"]

    async def test_appointment_intent_injects_calendar_context(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When intent.wants_appointment, calendar context is injected."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero una cita"))

        assert resp.status_code == 200
        _mock_external_services["calendar"].get_appointment_context.assert_called_once()
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "[APPOINTMENT CONTEXT]" in call_kwargs["extra_context"]

    async def test_combined_intents(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """Multiple intents combine their context."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(
            needs_repair_lookup=True, needs_prices=True, wants_appointment=True
        )
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [{"resguardo": "R001"}]
        _mock_external_services["sheets"].get_all_prices.return_value = [{"marca": "HP"}]

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "todo"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        ctx = call_kwargs["extra_context"]
        assert "[REPAIRS]" in ctx
        assert "[PRICES]" in ctx
        assert "[APPOINTMENT CONTEXT]" in ctx

    async def test_brand_faq_loaded(self, client, mock_intent, mock_openai_generate):
        """When intent has a brand, load_brand_faq is called and passed to generate."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(brand="dyson")

        with patch("main.load_brand_faq", return_value="Dyson FAQ content") as mock_faq:
            resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi dyson"))

        assert resp.status_code == 200
        mock_faq.assert_called_once_with("dyson")
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["brand_faq"] == "Dyson FAQ content"

    async def test_resguardo_lookup_when_no_phone_match(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When no repairs by phone, a resguardo number in message triggers resguardo lookup.

        Since 2026-04-21 the resguardo lookup is intentionally phone-agnostic
        (the client giving the resguardo is enough), so no phone is passed.
        """
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []
        _mock_external_services["sheets"].get_repair_by_resguardo = AsyncMock(
            return_value={"resguardo": "17058", "equipo_modelo": "HP Pavilion", "estado": "En reparación"}
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi resguardo es 17058"))

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repair_by_resguardo.assert_called_once_with("17058")
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert call_kwargs["extra_context"] is not None

    async def test_resguardo_not_found_message(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When the resguardo number doesn't exist, the bot asks the client to double-check it."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []
        _mock_external_services["sheets"].get_repair_by_resguardo = AsyncMock(return_value=None)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "17058"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "No se encontro ningun resguardo con el numero 17058" in call_kwargs["extra_context"]

    async def test_no_repairs_at_all_asks_for_resguardo(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """When no repairs found by phone and no resguardo in message, the bot asks for the resguardo."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = []

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero saber el estado"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        assert "numero de resguardo" in call_kwargs["extra_context"]

    async def test_cita_creates_calendar_event(self, client, mock_intent, mock_openai_generate, _mock_external_services, _isolated_db):
        """AI responds with CONFIRMAR_CITA → calendar event created, clean response sent."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        await _isolated_db.save_message("34600111222", "user", "mi correo es juan@example.com")
        mock_openai_generate.generate_response.return_value = (
            f"Tu cita queda registrada.\nCONFIRMAR_CITA|{_future_appointment_iso()}|Juan Garcia|Reparacion portatil"
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero cita manana"))

        assert resp.status_code == 200

        # Calendar event was created
        _mock_external_services["calendar"].create_event.assert_called_once()
        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert call_kwargs["title"] == "CITA: Juan Garcia"

        # WhatsApp received clean confirmation (no CONFIRMAR_CITA line)
        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "CONFIRMAR_CITA" not in wa_text
        assert "Tu cita ha sido registrada" in wa_text

    async def test_envio_creates_calendar_event_without_asking_for_data(self, client, mock_intent, mock_openai_generate, _mock_external_services, _isolated_db):
        """AI responds with CONFIRMAR_ENVIO (no personal data collected in chat) → calendar event created, payment link sent to client."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Recogida confirmada.\n"
            "CONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Pendiente|Dyson V15"
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero envio"))

        assert resp.status_code == 200

        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert call_kwargs["title"] == "RECOGIDA: Pendiente"
        assert "Dyson V15" in call_kwargs["description"]

        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "30€" in wa_text
        assert "CONFIRMAR_ENVIO" not in wa_text

    async def test_alquiler_7_dias_envia_enlaces_de_alquiler_y_fianza(self, client, mock_intent, mock_openai_generate, _mock_external_services, _isolated_db):
        """AI responds with CONFIRMAR_ALQUILER for 8 dias → envio gratis, enlaces CASO 1 (Windows), sin cobrar 30€."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        mock_openai_generate.generate_response.return_value = (
            "Alquiler confirmado.\n"
            "CONFIRMAR_ALQUILER|2026-05-10T00:00:00+02:00|Pendiente|Windows - HP 15-bc|8 días|domicilio|Pendiente - datos en enlace de pago"
        )

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero alquilar"))

        assert resp.status_code == 200

        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert "Windows - HP 15-bc" in call_kwargs["title"]

        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "Windows - HP 15-bc" in wa_text
        assert "https://sis.redsys.es/tiendaWeb/item/NDk4OzU=" in wa_text
        assert "https://sis.redsys.es/tiendaWeb/item/NDk4OzY=" in wa_text
        assert "30€" not in wa_text
        assert "CONFIRMAR_ALQUILER" not in wa_text

    async def test_calendar_failure_appends_error(self, client, mock_intent, mock_openai_generate, _mock_external_services, _isolated_db):
        """If calendar event creation fails, user gets a graceful fallback message."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        await _isolated_db.save_message("34600111222", "user", "mi correo es juan@example.com")
        mock_openai_generate.generate_response.return_value = (
            f"Ok.\nCONFIRMAR_CITA|{_future_appointment_iso()}|Juan Garcia|Repair"
        )
        _mock_external_services["calendar"].create_event.return_value = None

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "cita"))

        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "no he podido registrarla automáticamente" in wa_text

    async def test_non_text_message_ignored(self, client, _mock_external_services):
        """Non-text messages get a polite rejection."""
        body = {
            "entry": [{"changes": [{"value": {"messages": [{"from": "34600111222", "type": "image"}]}}]}]
        }
        resp = await client.post("/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "non-text ignored"
        _mock_external_services["whatsapp"].send_message.assert_called_once()

    async def test_empty_webhook_body(self, client):
        """Webhook with no entry returns gracefully."""
        resp = await client.post("/webhook", json={"entry": []})
        assert resp.status_code == 200
        assert resp.json()["status"] == "no entry"

    async def test_status_update_no_messages(self, client):
        """Webhook status update (no messages) handled gracefully."""
        body = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
        resp = await client.post("/webhook", json=body)
        assert resp.status_code == 200
        assert resp.json()["status"] == "no messages"


# ===========================================================================
# Human mode
# ===========================================================================


class TestHumanMode:
    async def test_human_mode_skips_ai(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """When conversation is in human mode, message is saved but AI is not called."""
        await _isolated_db.set_conversation_mode("34600111222", "human")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()
        _mock_external_services["whatsapp"].send_message.assert_not_called()

        # Message was still saved
        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 1
        assert history[0]["content"] == "hola"


# ===========================================================================
# Rate limiting (per-phone)
# ===========================================================================


class TestPhoneRateLimit:
    async def test_rate_limit_blocks_after_threshold(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """After 10 messages in 60s, subsequent messages are rate-limited."""
        # Simulate 10 prior messages
        for i in range(10):
            await _isolated_db.save_message("34600111222", "user", f"msg {i}")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "one more"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "phone_rate_limited"
        mock_openai_generate.generate_response.assert_not_called()


# ===========================================================================
# Chatwoot Webhook – full pipeline
# ===========================================================================


class TestChatwootWebhookFlow:
    async def test_basic_chatwoot_message(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Chatwoot incoming message → AI responds → Chatwoot reply sent → DB saved."""
        mock_openai_generate.generate_response.return_value = "Hola desde Chatwoot."

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(101, "hola"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Chatwoot received the reply
        _mock_external_services["chatwoot"].send_message.assert_called_once_with(101, "Hola desde Chatwoot.")

        # DB has messages under the phone key (used for history so context
        # persists across new conversation_ids from the same client).
        history = await _isolated_db.get_history("34612345678")
        assert len(history) == 2

    async def test_chatwoot_first_message_schedules_espocrm_lead(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """First message schedules a delayed EspoCRM lead (10-min volcado)."""
        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(102, "necesito reparar mi portatil"))

        assert resp.status_code == 200
        _mock_external_services["espocrm"].schedule_lead_from_conversation.assert_called_once()
        call_kwargs = _mock_external_services["espocrm"].schedule_lead_from_conversation.call_args.kwargs
        assert "lead_label" in call_kwargs

    async def test_chatwoot_second_message_no_lead(self, client, mock_intent, mock_openai_generate, _isolated_db, _mock_external_services):
        """Second message does not schedule a new EspoCRM lead."""
        # Pre-populate history under the phone key (used for history so
        # context persists across new conversation_ids from the same client).
        await _isolated_db.save_message("34612345678", "user", "previous msg")

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(103, "hola otra vez"))

        assert resp.status_code == 200
        _mock_external_services["espocrm"].schedule_lead_from_conversation.assert_not_called()

    async def test_chatwoot_repair_intent_uses_phone_from_source(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """Repair lookup uses phone from contact_inbox.source_id."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.return_value = [{"resguardo": "R100"}]

        body = _chatwoot_webhook_body(104, "como va mi reparacion", phone="34699887766")
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        _mock_external_services["sheets"].get_repairs_by_phone.assert_called_once_with("34699887766")

    async def test_chatwoot_envio_with_calendar(self, client, mock_intent, mock_openai_generate, _mock_external_services, _isolated_db):
        """Chatwoot flow also handles CONFIRMAR_ENVIO correctly."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(wants_appointment=True)
        await _isolated_db.save_message("34612345678", "user", "mi correo es ana@example.com")
        mock_openai_generate.generate_response.return_value = (
            "Listo.\nCONFIRMAR_ENVIO|2026-04-01T10:00:00+02:00|Ana Ruiz|MacBook|Calle Sol 5, Madrid|12345678A"
        )

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(105, "quiero envio"))

        assert resp.status_code == 200
        _mock_external_services["calendar"].create_event.assert_called_once()
        call_kwargs = _mock_external_services["calendar"].create_event.call_args.kwargs
        assert "RECOGIDA" in call_kwargs["title"]

        # Chatwoot got the clean payment-link response
        cw_text = _mock_external_services["chatwoot"].send_message.call_args[0][1]
        assert "CONFIRMAR_ENVIO" not in cw_text
        assert "30€" in cw_text

    async def test_chatwoot_ignores_non_incoming(self, client, _mock_external_services):
        """Chatwoot outgoing/activity events are ignored."""
        body = {"event": "message_created", "message_type": "outgoing", "content": "hi"}
        resp = await client.post("/chatwoot/webhook", json=body)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_chatwoot_human_mode(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Chatwoot conversation in human mode saves message but skips AI."""
        await _isolated_db.set_conversation_mode("chatwoot_106", "human")

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(106, "ayuda"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()

    async def test_bot_own_outgoing_message_ignored(self, client, _isolated_db, _mock_external_services):
        """Bot's own outgoing messages should NOT activate human mode."""
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, bienvenido a Kelatos",
            "sender": {"id": 1, "name": "Kelatos Bot", "type": "agent_bot"},
            "conversation": {
                "id": 109,
                "contact_inbox": {"source_id": "34600999666"},
            },
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "bot_echo_ignored"

        # Mode should still be bot (default)
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_109")
        assert mode_cw == "bot"
        mode_phone = await _isolated_db.get_conversation_mode("34600999666")
        assert mode_phone == "bot"

    async def test_agent_outgoing_message_activates_human_mode(self, client, _isolated_db, _mock_external_services):
        """When an agent sends a message from Chatwoot, bot switches to human mode."""
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, soy el tecnico",
            "sender": {"id": 12, "name": "Cielo", "type": "user"},
            "conversation": {
                "id": 107,
                "contact_inbox": {"source_id": "34600999888"},
            },
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "agent_mode"

        # Both keys set to human
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_107")
        assert mode_cw == "human"
        mode_phone = await _isolated_db.get_conversation_mode("34600999888")
        assert mode_phone == "human"

    async def test_agent_outgoing_then_client_reply_bot_silent(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """After agent writes from Chatwoot, client reply via WhatsApp doesn't trigger bot."""
        # Agent writes
        body = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Hola, te informo sobre tu equipo",
            "sender": {"id": 12, "name": "Cielo", "type": "user"},
            "conversation": {
                "id": 108,
                "contact_inbox": {"source_id": "34600999777"},
            },
        }
        await client.post("/chatwoot/webhook", json=body)

        # Client replies via WhatsApp
        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600999777", "gracias"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()


# ===========================================================================
# Conversation history persistence
# ===========================================================================


class TestHistoryPersistence:
    async def test_history_passed_to_ai(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Previous messages are loaded and passed to generate_response."""
        # Simulate prior conversation
        await _isolated_db.save_message("34600111222", "user", "msg anterior")
        await _isolated_db.save_message("34600111222", "assistant", "respuesta anterior")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "y ahora?"))

        assert resp.status_code == 200
        call_kwargs = mock_openai_generate.generate_response.call_args.kwargs
        history = call_kwargs["history"]
        assert len(history) == 2
        assert history[0]["content"] == "msg anterior"
        assert history[1]["content"] == "respuesta anterior"

    async def test_multiple_messages_accumulate(self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services):
        """Each message round-trip adds to the history."""
        mock_openai_generate.generate_response.return_value = "resp1"
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "msg1"))

        mock_openai_generate.generate_response.return_value = "resp2"
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "msg2"))

        history = await _isolated_db.get_history("34600111222")
        assert len(history) == 4  # user+assistant × 2
        assert history[0]["content"] == "msg1"
        assert history[1]["content"] == "resp1"
        assert history[2]["content"] == "msg2"
        assert history[3]["content"] == "resp2"


# ===========================================================================
# Webhook verification (GET /webhook)
# ===========================================================================


class TestWebhookVerification:
    async def test_valid_verification(self, client):
        from config import settings
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.VERIFY_TOKEN,
            "hub.challenge": "test_challenge_123",
        })
        assert resp.status_code == 200
        assert resp.text == "test_challenge_123"

    async def test_invalid_token_rejected(self, client):
        resp = await client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "test_challenge_123",
        })
        assert resp.status_code == 403


# ===========================================================================
# Health check
# ===========================================================================


class TestHealthCheck:
    async def test_health_endpoint(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ===========================================================================
# Error resilience
# ===========================================================================


class TestErrorResilience:
    async def test_sheets_error_does_not_break_flow(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """If sheets throws, the message flow still completes (without repair data)."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_repair_lookup=True)
        _mock_external_services["sheets"].get_repairs_by_phone.side_effect = Exception("Sheets API down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "mi reparacion"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # AI was still called, just without extra context
        mock_openai_generate.generate_response.assert_called_once()

    async def test_whatsapp_send_error_does_not_crash(self, client, mock_intent, mock_openai_generate, _mock_external_services):
        """If WhatsApp send fails, endpoint still returns 200 (error caught)."""
        _mock_external_services["whatsapp"].send_message.side_effect = Exception("WA API down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola"))

        # The endpoint catches all exceptions and returns status: error
        assert resp.status_code == 200


# ===========================================================================
# Handoff to human agent
# ===========================================================================


class TestHandoff:
    @pytest.fixture(autouse=True)
    def _force_business_hours(self, mocker):
        """Ensure handoff tests always run as if within business hours."""
        mocker.patch("main.is_within_business_hours", return_value=True)

    async def test_whatsapp_handoff_sends_message_and_sets_human_mode(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """When needs_human=True, bot sends handoff message, sets mode to human, calls Chatwoot handoff."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero hablar con un tecnico"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Handoff message sent via WhatsApp
        wa_text = _mock_external_services["whatsapp"].send_message.call_args.kwargs["text"]
        assert "transfiero" in wa_text.lower()

        # Chatwoot searched for conversation and called handoff
        _mock_external_services["chatwoot"].find_conversation_by_phone.assert_called_once_with("34600111222")
        _mock_external_services["chatwoot"].handoff_to_agent.assert_called_once_with(999)

        # Mode set to human
        mode = await _isolated_db.get_conversation_mode("34600111222")
        assert mode == "human"

        # AI was NOT called
        mock_openai_generate.generate_response.assert_not_called()

    async def test_whatsapp_handoff_message_saved_to_history(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Handoff message is persisted in chat history."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "pasame con alguien"))

        history = await _isolated_db.get_history("34600111222")
        # user message + handoff assistant message
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert "transfiero" in history[1]["content"].lower()

    async def test_chatwoot_handoff(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Chatwoot webhook also handles handoff correctly."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(201, "necesito hablar con alguien"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Message sent via Chatwoot
        cw_args = _mock_external_services["chatwoot"].send_message.call_args
        assert "transfiero" in cw_args[0][1].lower()

        # Handoff called with the conversation_id from the webhook
        _mock_external_services["chatwoot"].handoff_to_agent.assert_called_once_with(201)

        # Mode set to human
        mode = await _isolated_db.get_conversation_mode("chatwoot_201")
        assert mode == "human"

    async def test_after_handoff_bot_silent(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """After handoff, subsequent messages don't trigger AI."""
        from intent_classifier import IntentResult

        # First: handoff
        mock_intent.return_value = IntentResult(needs_human=True)
        await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "quiero hablar con alguien"))

        # Reset mocks
        mock_openai_generate.generate_response.reset_mock()
        _mock_external_services["whatsapp"].send_message.reset_mock()

        # Second: another message — should be silent
        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "hola sigues ahi?"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "human mode"
        mock_openai_generate.generate_response.assert_not_called()

    async def test_conversation_resolved_restores_bot_mode(
        self, client, _isolated_db, _mock_external_services
    ):
        """Chatwoot conversation_status_changed resolved → bot mode restored."""
        # Set up human mode
        await _isolated_db.set_conversation_mode("chatwoot_301", "human")

        body = {
            "event": "conversation_status_changed",
            "status": "resolved",
            "id": 301,
            "contact_inbox": {"source_id": "34600111222"},
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "bot_restored"

        # Both keys restored to bot
        mode_cw = await _isolated_db.get_conversation_mode("chatwoot_301")
        assert mode_cw == "bot"
        mode_phone = await _isolated_db.get_conversation_mode("34600111222")
        assert mode_phone == "bot"

    async def test_conversation_resolved_without_phone(
        self, client, _isolated_db, _mock_external_services
    ):
        """Resolved event without source_id still restores chatwoot key."""
        await _isolated_db.set_conversation_mode("chatwoot_302", "human")

        body = {
            "event": "conversation_status_changed",
            "status": "resolved",
            "id": 302,
        }
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        mode = await _isolated_db.get_conversation_mode("chatwoot_302")
        assert mode == "bot"

    async def test_handoff_chatwoot_failure_still_sets_human_mode(
        self, client, _isolated_db, mock_intent, mock_openai_generate, _mock_external_services
    ):
        """Even if Chatwoot handoff API fails, mode is still set to human."""
        from intent_classifier import IntentResult
        mock_intent.return_value = IntentResult(needs_human=True)
        _mock_external_services["chatwoot"].handoff_to_agent.side_effect = Exception("Chatwoot down")

        resp = await client.post("/webhook", json=_whatsapp_webhook_body("34600111222", "pasame con un tecnico"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "handoff"

        # Mode still set to human despite Chatwoot failure
        mode = await _isolated_db.get_conversation_mode("34600111222")
        assert mode == "human"


# ===========================================================================
# Survey outgoing exclusion (n8n template no activa human mode)
# ===========================================================================


class TestSurveyOutgoingExclusion:
    async def test_outgoing_template_with_survey_label_does_not_set_human_mode(
        self, client, _isolated_db, _mock_external_services,
    ):
        """Outgoing message with survey label in conversation → survey_outgoing_ignored, no human mode."""
        body = _chatwoot_outgoing_body(
            conversation_id=601,
            content="Por favor valora el servicio: ...",
            labels=["encuesta_reseña_pendiente", "encuesta_destino_kelatos"],
        )
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "survey_outgoing_ignored"

        mode = await _isolated_db.get_conversation_mode("chatwoot_601")
        assert mode == "bot"

    async def test_outgoing_template_by_name_does_not_set_human_mode(
        self, client, _isolated_db, _mock_external_services,
    ):
        """Outgoing message matching encuesta_satisfaccion_servicio_v2 template → survey_outgoing_ignored."""
        body = _chatwoot_outgoing_body(
            conversation_id=602,
            content="Valora el servicio recibido:",
            template_name="encuesta_satisfaccion_servicio_v2",
        )
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "survey_outgoing_ignored"

        mode = await _isolated_db.get_conversation_mode("chatwoot_602")
        assert mode == "bot"

    async def test_normal_agent_outgoing_without_survey_label_activates_human_mode(
        self, client, _isolated_db, _mock_external_services,
    ):
        """Normal agent outgoing message (no survey label, no template) still activates human mode."""
        body = _chatwoot_outgoing_body(
            conversation_id=603,
            content="Hola, te atiendo enseguida.",
            labels=["cliente_recurrente"],
        )
        resp = await client.post("/chatwoot/webhook", json=body)

        assert resp.status_code == 200
        assert resp.json()["status"] == "agent_mode"

        mode = await _isolated_db.get_conversation_mode("chatwoot_603")
        assert mode == "human"


# ===========================================================================
# Survey response exclusion (encuesta de satisfacción ↔ n8n)
# ===========================================================================


class TestSurveyResponseExclusion:
    # --- Path A: etiquetas presentes en el payload → sin llamada HTTP ---

    @pytest.mark.parametrize(
        "raw_text,expected_normalized",
        [
            ("Muy bueno", "muy bueno"),
            ("Bueno", "bueno"),
            ("Malo", "malo"),
            ("Muy malo", "muy malo"),
            ("   MUY MALO", "muy malo"),
        ],
    )
    async def test_survey_response_ignored_via_payload_labels(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
        raw_text, expected_normalized,
    ):
        """Survey label in payload → ignored immediately, zero HTTP calls to labels endpoint."""
        resp = await client.post(
            "/chatwoot/webhook",
            json=_chatwoot_webhook_body(
                501, raw_text,
                labels=["encuesta_reseña_pendiente", "encuesta_destino_kelatos"],
            ),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "survey_response_ignored"
        assert body["survey_response"] == expected_normalized

        mock_openai_generate.generate_response.assert_not_called()
        _mock_external_services["chatwoot"].send_message.assert_not_called()
        # No HTTP call because labels were in the payload
        _mock_external_services["chatwoot"].get_conversation_labels.assert_not_called()

    async def test_survey_response_payload_has_labels_but_not_survey_label_processes_normally(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """Payload has labels but not the survey label → normal flow, no HTTP call."""
        mock_openai_generate.generate_response.return_value = "Claro, te informo."

        resp = await client.post(
            "/chatwoot/webhook",
            json=_chatwoot_webhook_body(502, "Bueno", labels=["cliente_recurrente"]),
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_openai_generate.generate_response.assert_called_once()
        _mock_external_services["chatwoot"].get_conversation_labels.assert_not_called()

    # --- Path B: payload sin etiquetas → fallback HTTP ---

    async def test_survey_response_no_payload_labels_uses_http_fallback(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """No labels in payload + survey response → HTTP fallback to get_conversation_labels."""
        _mock_external_services["chatwoot"].get_conversation_labels.return_value = [
            "encuesta_reseña_pendiente",
        ]

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(503, "Bueno"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "survey_response_ignored"

        mock_openai_generate.generate_response.assert_not_called()
        _mock_external_services["chatwoot"].get_conversation_labels.assert_called_once_with(503)

    async def test_survey_response_http_fallback_no_label_processes_normally(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """HTTP fallback returns labels without survey label → normal processing."""
        _mock_external_services["chatwoot"].get_conversation_labels.return_value = ["cliente_recurrente"]
        mock_openai_generate.generate_response.return_value = "Hola desde Chatwoot."

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(504, "Bueno"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_openai_generate.generate_response.assert_called_once()
        _mock_external_services["chatwoot"].get_conversation_labels.assert_called_once_with(504)

    async def test_label_lookup_failure_fails_open(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """HTTP fallback errors out → fail-open, bot still answers."""
        _mock_external_services["chatwoot"].get_conversation_labels.side_effect = Exception("Chatwoot down")
        mock_openai_generate.generate_response.return_value = "Hola desde Chatwoot."

        resp = await client.post("/chatwoot/webhook", json=_chatwoot_webhook_body(505, "Malo"))

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_openai_generate.generate_response.assert_called_once()
        _mock_external_services["chatwoot"].send_message.assert_called_once()

    # --- Mensajes que no son respuesta exacta → nunca consultan etiquetas ---

    async def test_non_exact_text_never_checks_labels(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """'Bueno, necesito ayuda' is not an exact match → no label check at all."""
        mock_openai_generate.generate_response.return_value = "Claro, dime."

        resp = await client.post(
            "/chatwoot/webhook",
            json=_chatwoot_webhook_body(506, "Bueno, necesito ayuda"),
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_openai_generate.generate_response.assert_called_once()
        _mock_external_services["chatwoot"].get_conversation_labels.assert_not_called()

    async def test_normal_message_with_pending_label_in_payload_processes_normally(
        self, client, mock_intent, mock_openai_generate, _mock_external_services,
    ):
        """Normal question (not exact survey match) → no label check even with survey label in payload."""
        mock_openai_generate.generate_response.return_value = "Tu equipo estará listo el viernes."

        resp = await client.post(
            "/chatwoot/webhook",
            json=_chatwoot_webhook_body(
                507,
                "Necesito saber cuándo estará listo mi ordenador",
                labels=["encuesta_reseña_pendiente"],
            ),
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_openai_generate.generate_response.assert_called_once()
        _mock_external_services["chatwoot"].get_conversation_labels.assert_not_called()


# ===========================================================================
# Admin reset endpoint
# ===========================================================================


class TestAdminResetConversation:
    async def test_reset_conversation_restores_bot_mode(self, client, _isolated_db):
        """POST /admin/reset-conversation/{id} with valid token resets human mode to bot."""
        from config import settings

        await _isolated_db.set_conversation_mode("chatwoot_489", "human")

        resp = await client.post(
            "/admin/reset-conversation/489",
            headers={"x-admin-token": settings.VERIFY_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "reset"

        mode = await _isolated_db.get_conversation_mode("chatwoot_489")
        assert mode == "bot"

    async def test_reset_conversation_forbidden_without_token(self, client):
        """POST without valid token returns 403."""
        resp = await client.post(
            "/admin/reset-conversation/489",
            headers={"x-admin-token": "wrong_token"},
        )
        assert resp.status_code == 403
