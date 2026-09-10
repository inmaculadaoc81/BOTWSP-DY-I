import logging
import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from config import settings

# Rate limiter: max requests per IP
limiter = Limiter(key_func=get_remote_address)

# Per-phone rate limit settings
PHONE_RATE_LIMIT = 10  # max messages per phone number
PHONE_RATE_WINDOW = 60  # in seconds

from database import Database
from openai_service import OpenAIService
from whatsapp_service import WhatsAppService
from sheets_service import SheetsService
from kelatos_api_service import KelatosApiService
from chatwoot_service import ChatwootService
# Odoo DESCONECTADO - reemplazado por EspoCRM.
# from odoo_service import OdooService
from espocrm_service import EspoCRMService
from intent_classifier import classify_intent
from faq_service import load_brand_faq
from calendar_service import CalendarService, process_ai_calendar_command

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Services
db = Database()
openai_svc = OpenAIService()
whatsapp_svc = WhatsAppService()
sheets_svc = SheetsService()
kelatos_svc = KelatosApiService()
chatwoot_svc = ChatwootService()
# Odoo DESCONECTADO - reemplazado por EspoCRM.
# odoo_svc = OdooService()
espocrm_svc = EspoCRMService()
calendar_svc = CalendarService()


# Marcadores del flujo de cita/recogida que aparecen en el último mensaje del bot
# cuando está pidiendo al cliente que confirme el resumen antes de registrar.
APPOINTMENT_FLOW_MARKERS = (
    "Resumen de tu cita",
    "Resumen de tu recogida",
    "Resumen de tu solicitud de alquiler",
    "Resumen de tu envío de vuelta",
    "¿Es correcto?",
)


def _is_in_appointment_flow(history: list[dict]) -> bool:
    """True si el último mensaje del bot pidió confirmación de cita/recogida.

    Sirve para rescatar turnos donde el cliente responde solo 'si' y el
    clasificador no detecta wants_appointment por falta de contexto.
    """
    if not history:
        return False
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "") or ""
            return any(m in content for m in APPOINTMENT_FLOW_MARKERS)
    return False


_BUDGET_DECISION_KEYWORDS = [
    "rechaz", "no acepto", "no lo acepto", "acepto el presupuesto",
    "acepto la reparacion", "acepto la reparación", "sí acepto",
    "si acepto", "no quiero que lo reparen", "cancelar la reparacion",
    "cancelar la reparación", "no me interesa la reparacion",
    "no me interesa la reparación",
]
_BUDGET_CONTEXT_KEYWORDS = ["presupuesto", "reparacion", "reparación", "precio"]

BUDGET_DECISION_RESPONSE = (
    "Para aceptar o rechazar el presupuesto es necesario que respondas al correo "
    "electrónico en el que te lo enviamos. Por WhatsApp no podemos gestionar esa "
    "confirmación. 😊"
)

# Etiqueta que n8n añade a la conversación mientras espera la respuesta del
# cliente a la encuesta de satisfacción. Si está presente, el bot debe
# ignorar la respuesta para que no compitan ambos sistemas por contestar.
SURVEY_LABEL = "encuesta_reseña_pendiente"

SURVEY_RESPONSES = {
    "muy bueno",
    "bueno",
    "malo",
    "muy malo",
}

# ── Webhook idempotency & aviso de adjuntos (en memoria; despliegue de un solo proceso) ──
#
# Meta y Chatwoot reintentan la entrega del webhook si la respuesta tarda
# (la generación de la respuesta con IA puede tardar varios segundos). Sin
# protección, cada reintento se procesaba como un mensaje nuevo: se volvía a
# clasificar la intención y a generar una respuesta con IA distinta cada vez,
# de ahí que el cliente recibiera varias contestaciones distintas para el
# mismo mensaje ("¡Claro! ¿modelo de tu Mac...?" repetido con variaciones).
_SEEN_EVENTS_TTL = 600  # segundos que se recuerda un id de evento ya procesado
_seen_events: dict[str, float] = {}

# Cuando el cliente envía varias imágenes seguidas, cada una llega como un
# evento de webhook independiente. Sin control, el bot respondía "solo puedo
# leer mensajes de texto" una vez por cada imagen. Este cooldown limita ese
# aviso a como mucho uno por conversación cada N segundos.
_ATTACHMENT_NOTICE_COOLDOWN = 20
_last_attachment_notice: dict[str, float] = {}


def _is_duplicate_event(event_key: str) -> bool:
    """True si event_key ya se proceso recientemente (reintento de webhook).

    Marca el evento como visto como efecto secundario. Purga entradas viejas
    para que el diccionario no crezca sin límite (estado en memoria, se
    reinicia si el proceso se reinicia).
    """
    now = time.monotonic()
    stale = [k for k, ts in _seen_events.items() if now - ts > _SEEN_EVENTS_TTL]
    for k in stale:
        del _seen_events[k]
    if event_key in _seen_events:
        return True
    _seen_events[event_key] = now
    return False


def _should_send_attachment_notice(key: str) -> bool:
    """False si ya se envio el aviso de 'solo texto' a esta conversacion
    dentro de la ventana de cooldown (evita repetirlo una vez por cada
    imagen cuando el cliente envia varias seguidas)."""
    now = time.monotonic()
    last = _last_attachment_notice.get(key)
    if last is not None and now - last < _ATTACHMENT_NOTICE_COOLDOWN:
        return False
    _last_attachment_notice[key] = now
    return True


def _is_budget_decision(text: str, history: list[dict]) -> bool:
    """True si el mensaje es una aceptación o rechazo de presupuesto."""
    t = text.lower()
    if any(kw in t for kw in _BUDGET_DECISION_KEYWORDS):
        return True
    # "rechazarlo", "aceptarlo" solos necesitan contexto de presupuesto en el historial
    if any(kw in t for kw in ["rechazarlo", "aceptarlo", "no lo quiero", "adelante"]):
        combined = " ".join(
            m.get("content", "") for m in (history or []) if m.get("content")
        ).lower()
        if any(kw in combined for kw in _BUDGET_CONTEXT_KEYWORDS):
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    await db.init()
    logger.info("Database initialized")
    await sheets_svc.connect()
    logger.info(f"Webhook verify token: {settings.VERIFY_TOKEN}")
    yield
    await db.close()
    logger.info("Database closed")


app = FastAPI(title="WhatsApp Bot API", version="1.0.1", lifespan=lifespan)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Return 429 when IP rate limit is exceeded."""
    logger.warning(f"IP rate limit exceeded: {get_remote_address(request)}")
    return JSONResponse(
        status_code=429,
        content={"error": "Too many requests. Try again later."},
    )


@app.get("/")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "whatsapp-bot"}


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification.
    Meta sends a GET request with a challenge to verify your endpoint.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning(f"Webhook verification failed. Mode: {hub_mode}")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
@limiter.limit("30/minute")
async def receive_message(request: Request):
    """
    Receive incoming WhatsApp messages from Meta.
    Must return 200 quickly to avoid Meta retrying.
    """
    body = await request.json()
    logger.info(f"Incoming webhook: {json.dumps(body, indent=2)}")

    try:
        # Extract message data from Meta's webhook format
        entry = body.get("entry", [])
        if not entry:
            return {"status": "no entry"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "no changes"}

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        if not messages:
            # Could be a status update (delivered, read, etc.)
            logger.info("No messages in webhook (status update)")
            return {"status": "no messages"}

        message = messages[0]
        sender = message.get("from")  # Phone number of sender
        msg_type = message.get("type")
        message_id = message.get("id")

        # Ignore webhook retries of a message already processed (Meta reenvia
        # el evento si no recibe 200 a tiempo, p.ej. mientras la IA genera
        # respuesta). wamid es unico por mensaje.
        if message_id and _is_duplicate_event(f"wa:{message_id}"):
            logger.info(f"Duplicate WhatsApp webhook delivery ignored (id={message_id})")
            return {"status": "duplicate_ignored"}

        # Only handle text messages for now
        if msg_type != "text":
            logger.info(f"Ignoring non-text message type: {msg_type}")
            if not _should_send_attachment_notice(sender):
                logger.info(f"Attachment notice suppressed for {sender} (cooldown)")
                return {"status": "non-text ignored (cooldown)"}
            if msg_type in ("image", "video"):
                response_text = (
                    "¡Gracias por tu mensaje! 😊 Entiendo que quieres enviarnos fotos o videos, "
                    "pero lamentablemente no nos es posible realizar un diagnóstico técnico preciso "
                    "únicamente a través de imágenes o videos.\n\n"
                    "Para que nuestros técnicos puedan evaluar correctamente tu equipo, necesitamos "
                    "tenerlo físicamente en el local. 🔧\n\n"
                    "Lo bueno es que:\n"
                    "✅ El diagnóstico es *GRATUITO* para la mayoría de equipos (ordenadores, portátiles, "
                    "consolas, Surface, Dyson, Thermomix...)\n"
                    "✅ Para otros equipos tiene un coste de *20€ + IVA*, que se descuenta si decides reparar 💶\n\n"
                    "📌 Puedes traerlo directamente al local 🏪 sin cita previa (L-V 09:30-18:00), "
                    "o si lo prefieres, podemos ir a buscarlo con nuestro *servicio de recogida a domicilio* 🚚 "
                    "(15€ recogida + 15€ envío de vuelta, solo península).\n\n"
                    "¿Te gustaría agendar una recogida o tienes alguna pregunta? 😊"
                )
            else:
                response_text = "Por ahora solo puedo leer mensajes de texto. 😊"
            await whatsapp_svc.send_message(
                to=sender,
                text=response_text,
            )
            return {"status": "non-text ignored"}

        text = message["text"]["body"]
        logger.info(f"Message from {sender}: {text}")

        # Per-phone rate limiting
        recent = await db.count_recent_messages(sender, seconds=PHONE_RATE_WINDOW)
        if recent >= PHONE_RATE_LIMIT:
            logger.warning(f"Phone rate limit exceeded for {sender} ({recent} msgs in {PHONE_RATE_WINDOW}s)")
            await whatsapp_svc.send_message(
                to=sender,
                text="Estás enviando mensajes muy rápido. Por favor espera un momento antes de continuar.",
            )
            return {"status": "phone_rate_limited"}

        # Check conversation mode (bot or human)
        mode = await db.get_conversation_mode(sender)

        if mode == "human":
            # In human mode, just save the message (agent sees it in panel)
            await db.save_message(sender, "user", text)
            logger.info(f"Message saved for human agent (sender: {sender})")
            return {"status": "human mode"}

        # Bot mode: get conversation history for context
        history = await db.get_history(sender, limit=20)

        # Detect returning session (gap > 4 hours)
        last_msg_time_wa = await db.get_last_message_time(sender)
        now_utc_wa = datetime.now(timezone.utc)
        session_context_wa = None
        if last_msg_time_wa:
            gap_hours = (now_utc_wa - last_msg_time_wa).total_seconds() / 3600
            if gap_hours >= 4:
                session_context_wa = (
                    f"[SESIÓN RETOMADA]\n"
                    f"Han pasado aproximadamente {gap_hours:.0f} horas desde el último mensaje del cliente.\n"
                    f"Revisa el historial para identificar el tema de la última consulta y saluda en consecuencia."
                )
                logger.info(f"Returning session detected for {sender} (gap: {gap_hours:.1f}h)")

        # Save user message
        await db.save_message(sender, "user", text)

        # Classify intent (cheap gpt-4o-mini call)
        intent = await classify_intent(
            client=openai_svc.client,
            user_message=text,
            history=history,
        )
        logger.info(f"Intent for {sender}: {intent}")
        logger.info(
            f"[PRICES] needs_prices={intent.needs_prices} | "
            f"PRICES_SHEET_ID_SET={bool(settings.GOOGLE_PRICES_SHEET_ID)}"
        )

        # Handoff to human agent if requested
        if intent.needs_human:
            if is_within_business_hours():
                await db.save_message(sender, "assistant", HANDOFF_MESSAGE)
                await whatsapp_svc.send_message(to=sender, text=HANDOFF_MESSAGE)
                await _handle_handoff(sender)
                return {"status": "handoff"}
            else:
                msg = get_outside_hours_message()
                await db.save_message(sender, "assistant", msg)
                await whatsapp_svc.send_message(to=sender, text=msg)
                logger.info(f"Handoff denied for {sender} — outside business hours")
                return {"status": "outside_hours"}

        # Fetch only what's needed based on classification
        extra_context_parts = []

        if session_context_wa:
            extra_context_parts.append(session_context_wa)

        needs_repair = intent.needs_repair_lookup
        if needs_repair:
            repair_ctx = await _repair_lookup(sender, text)
            if repair_ctx:
                extra_context_parts.append(repair_ctx)

        if intent.needs_prices:
            try:
                prices = await sheets_svc.get_all_prices()
                if prices:
                    extra_context_parts.append(sheets_svc.format_prices_for_prompt(prices))
            except Exception as e:
                logger.error(f"Error fetching prices: {e}", exc_info=True)

        if intent.needs_rental_lookup:
            try:
                equipos = await kelatos_svc.get_available_equipos()
                extra_context_parts.append(sheets_svc.format_equipos_for_prompt(equipos))
            except Exception as e:
                logger.error(f"Error fetching rental equipment: {e}", exc_info=True)

        if not intent.wants_appointment and _is_in_appointment_flow(history):
            intent.wants_appointment = True
            logger.info("Forced wants_appointment=True (active appointment flow detected in history)")

        if intent.wants_appointment:
            extra_context_parts.append(calendar_svc.get_appointment_context())

        extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else None

        # Load brand-specific FAQ if classified
        brand_faq = None
        if intent.brand:
            brand_faq = load_brand_faq(intent.brand)

        # Intercept: aceptación/rechazo de presupuesto — respuesta fija, sin LLM
        if _is_budget_decision(text, history):
            ai_response = BUDGET_DECISION_RESPONSE
            logger.info("Budget decision intercepted (WhatsApp), returning fixed response.")
        else:
        # Generate AI response
            ai_response = await openai_svc.generate_response(
                user_message=text,
                history=history,
                extra_context=extra_context,
                brand_faq=brand_faq,
            )
        logger.info("RAW AI RESPONSE (WhatsApp):\n%s", ai_response)

        # Check if AI wants to transfer to agent (product purchase)
        if "TRANSFERIR_AGENTE" in ai_response:
            if is_within_business_hours():
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                full_msg = f"{clean_response}\n\n{HANDOFF_MESSAGE}" if clean_response else HANDOFF_MESSAGE
                await db.save_message(sender, "assistant", full_msg)
                await whatsapp_svc.send_message(to=sender, text=full_msg)
                await _handle_handoff(sender)
            else:
                # Outside hours: keep whatever the model already answered, append closing note
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                closing = get_outside_hours_message()
                full_msg = f"{clean_response}\n\n{closing}" if clean_response else closing
                await db.save_message(sender, "assistant", full_msg)
                await whatsapp_svc.send_message(to=sender, text=full_msg)
            return {"status": "handoff"}

        # Check if AI confirmed an appointment or pickup
        clean_response, created_event = await process_ai_calendar_command(
            calendar_service=calendar_svc,
            ai_response=ai_response,
            attendee_phone=sender,
            history=history,
        )
        logger.info("CLEAN RESPONSE (WhatsApp):\n%s", clean_response)
        logger.info("CREATED EVENT (WhatsApp): %s", created_event)

        # Save bot response
        await db.save_message(sender, "assistant", clean_response)

        # Send response via WhatsApp
        await whatsapp_svc.send_message(to=sender, text=clean_response)

        logger.info(f"Response sent to {sender}: {clean_response[:100]}...")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        return {"status": "error"}


HANDOFF_MESSAGE = (
    "🔄 Te transfiero con un compañero del *equipo técnico*. "
    "En breve te atenderán."
)

# Spanish holidays 2026 (update yearly)
HOLIDAYS_2026 = {
    # Nacionales
    "01-01", "01-06", "04-02", "04-03", "05-01",
    "08-15", "10-12", "11-02", "12-07", "12-08", "12-25",
    # Madrid
    "05-02", "05-15", "11-09",
}

WEEKDAY_NAMES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _get_madrid_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


def is_within_business_hours() -> bool:
    """Check if current time is within L-V 9:30-18:00 Madrid time, excluding holidays."""
    now = _get_madrid_now()
    if now.weekday() >= 5:
        return False
    if now.strftime("%m-%d") in HOLIDAYS_2026:
        return False
    current_time = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= current_time < 18 * 60


def get_outside_hours_message() -> str:
    """Build message with the next business day/time."""
    now = _get_madrid_now()
    current_time = now.hour * 60 + now.minute

    from datetime import timedelta

    # Find next business day/time
    if now.weekday() < 5 and now.strftime("%m-%d") not in HOLIDAYS_2026 and current_time < 9 * 60 + 30:
        when = "hoy dentro de nuestro horario de atención"
        next_day_is_monday = False
    else:
        next_day = now + timedelta(days=1)
        for _ in range(7):
            if next_day.weekday() < 5 and next_day.strftime("%m-%d") not in HOLIDAYS_2026:
                break
            next_day += timedelta(days=1)

        diff_days = (next_day.date() - now.date()).days
        if diff_days == 1:
            when = "mañana dentro de nuestro horario de atención"
        else:
            day_name = WEEKDAY_NAMES[next_day.weekday()]
            when = f"el *{day_name}* dentro de nuestro horario de atención"

        # Pedir contacto si el siguiente día laborable es lunes (viernes noche, sábado o domingo).
        # WhatsApp solo permite responder dentro de las 24h del último mensaje del cliente,
        # por lo que si escribe el fin de semana, para el lunes puede haber vencido esa ventana.
        next_day_is_monday = next_day.weekday() == 0
    if next_day_is_monday:
        contact_line = (
            "\n\nComo el local está cerrado hasta el lunes y WhatsApp solo permite responder "
            "dentro de las 24h, te recomiendo que vuelvas a escribir el lunes por la mañana "
            "o déjame tu *nombre* y *número de contacto* para llamarte en cuanto abramos. 😊"
        )
    else:
        contact_line = "\n\nMientras tanto, puedo seguir ayudándote con cualquier consulta. 😊"

    return (
        f"🕐 En este momento estamos fuera de horario. Un compañero te atenderá "
        f"{when}.{contact_line}"
    )


async def _handle_handoff(sender_key: str, conversation_id: int | None = None):
    """
    Transfer the conversation to a human agent.
    - If conversation_id is provided (Chatwoot webhook), use it directly.
    - Otherwise (WhatsApp webhook), search Chatwoot by phone.
    Sets mode to 'human' so the bot stops responding.
    """
    # Find conversation_id if not provided
    if conversation_id is None:
        conversation_id = await chatwoot_svc.find_conversation_by_phone(sender_key)

    # Toggle conversation to open in Chatwoot so agent sees it
    if conversation_id:
        try:
            await chatwoot_svc.handoff_to_agent(conversation_id)
        except Exception as e:
            logger.error(f"Failed to handoff conversation {conversation_id}: {e}", exc_info=True)

        # Auto-assign to handoff agent (round-robin: Iván/Daniela)
        try:
            assigned = await chatwoot_svc.assign_handoff_agent(conversation_id)
            if assigned:
                logger.info(f"Handoff agent {assigned} auto-assigned to conversation {conversation_id}")
        except Exception as e:
            logger.error(f"Failed to auto-assign agent for conversation {conversation_id}: {e}", exc_info=True)

    # Set mode to human so bot stops responding
    await db.set_conversation_mode(sender_key, "human")
    logger.info(f"Handoff complete for {sender_key} (conversation: {conversation_id})")


# ── Repair lookup helper ──────────────────────────────────────────────

_RESGUARDO_RE = re.compile(r"\b(\d{4,6})\b")


async def _repair_lookup(phone: str, message: str) -> str | None:
    """Try to find repair data for the user.

    Prioridad:
    1. Si el mensaje contiene un numero de resguardo (4-6 digitos), buscar
       por ese resguardo sin validar el telefono. Es el flujo principal:
       el cliente da su resguardo y el bot le dice el estado.
    2. Si no hay resguardo en el mensaje, intentar buscar por el telefono
       del remitente por si coincide (atajo opcional).
    3. Si no hay nada, pedir amablemente el resguardo.
    """
    # Step 1: resguardo directly in message → lookup without phone check
    match = _RESGUARDO_RE.search(message)
    if match:
        resguardo = match.group(1)
        try:
            repair = await kelatos_svc.get_repair_by_resguardo(resguardo)
            if repair:
                logger.info(f"Found repair by resguardo {resguardo}")
                return sheets_svc.format_repairs_for_prompt([repair])
            else:
                logger.info(f"Resguardo {resguardo} not found")
                return (
                    "[RESULTADO BUSQUEDA RESGUARDO]\n"
                    f"No se encontro ningun resguardo con el numero {resguardo}.\n"
                    "INSTRUCCIONES: Informa al cliente amablemente que no se encuentra "
                    "ese numero de resguardo y pidele que lo revise y lo vuelva a enviar. "
                    "Si insiste en que es correcto, ofrece transferirlo con un compañero."
                )
        except Exception as e:
            logger.error(f"Error fetching resguardo {resguardo}: {e}", exc_info=True)

    # Step 2: try by sender phone as a shortcut (in case registered from same number)
    try:
        repairs = await kelatos_svc.get_repairs_by_phone(phone)
        if repairs:
            logger.info(f"Found {len(repairs)} repairs by phone for {phone}")
            return sheets_svc.format_repairs_for_prompt(repairs)
    except Exception as e:
        logger.error(f"Error fetching repairs by phone: {e}", exc_info=True)

    # Step 3: nothing found → ask for resguardo
    return (
        "[RESULTADO BUSQUEDA REPARACIONES]\n"
        "No se encontraron reparaciones para este cliente.\n"
        "INSTRUCCIONES: Pide al cliente su numero de resguardo (son 4 a 6 digitos "
        "que aparecen en el papel/correo que recibio al dejar el equipo) para poder "
        "buscar el estado."
    )


# ── Chatwoot Agent Bot webhook ──────────────────────────────────────────


@app.post("/chatwoot/webhook")
@limiter.limit("30/minute")
async def chatwoot_webhook(request: Request):
    """
    Receive incoming events from Chatwoot Agent Bot.
    Chatwoot sends message_created events when a customer writes.
    """
    body = await request.json()
    logger.info(f"Chatwoot webhook: {json.dumps(body, indent=2)}")

    try:
        event = body.get("event")
        message_type = body.get("message_type")

        # Handle conversation resolved → restore bot mode
        if event == "conversation_status_changed":
            status = body.get("status")
            if status == "resolved":
                conversation = body.get("id") or body.get("conversation", {}).get("id")
                if conversation:
                    sender_key = f"chatwoot_{conversation}"
                    await db.set_conversation_mode(sender_key, "bot")
                    logger.info(f"Conversation {conversation} resolved — bot mode restored")

                    # Also restore by phone if available
                    contact_inbox = body.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")
                    if source_id:
                        await db.set_conversation_mode(source_id, "bot")
                        logger.info(f"Bot mode restored for phone {source_id}")

                return {"status": "bot_restored"}
            return {"status": "ignored"}

        # Handle agent assigned/unassigned → toggle human/bot mode
        if event == "conversation_updated":
            # changed_attributes is an array of dicts, merge into one
            changed_list = body.get("changed_attributes", [])
            changed = {}
            for item in changed_list:
                changed.update(item)

            if "assignee_id" in changed:
                assignee = changed["assignee_id"]
                current = assignee.get("current_value")
                previous = assignee.get("previous_value")
                conversation_id = body.get("id")

                if conversation_id:
                    sender_key = f"chatwoot_{conversation_id}"
                    contact_inbox = body.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")

                    if current and not previous:
                        # Agent assigned → human mode
                        await db.set_conversation_mode(sender_key, "human")
                        if source_id:
                            await db.set_conversation_mode(source_id, "human")
                        logger.info(f"Agent {current} assigned to conversation {conversation_id} — human mode")
                        return {"status": "agent_assigned"}

                    elif not current and previous:
                        # Agent unassigned → restore bot mode
                        await db.set_conversation_mode(sender_key, "bot")
                        if source_id:
                            await db.set_conversation_mode(source_id, "bot")
                        logger.info(f"Agent unassigned from conversation {conversation_id} — bot mode restored")
                        return {"status": "agent_unassigned"}

            return {"status": "ignored"}

        # Only process incoming messages (from the customer)
        if event != "message_created" or message_type != "incoming":
            # Agent sent a message → activate human mode so bot doesn't interfere
            # But ignore messages from the bot itself (sender type "agent_bot")
            if event == "message_created" and message_type == "outgoing":
                sender_info = body.get("sender", {})
                sender_type = sender_info.get("type", "")
                if sender_type == "agent_bot":
                    logger.info(f"Ignoring bot's own outgoing message")
                    return {"status": "bot_echo_ignored"}

                conversation = body.get("conversation", {})
                conv_id = conversation.get("id")
                if conv_id:
                    # Si el mensaje saliente pertenece a la encuesta de
                    # satisfacción (por etiqueta o por nombre de plantilla),
                    # el bot no debe activar human mode: ese mensaje lo envía
                    # n8n y no implica que un agente humano haya tomado la
                    # conversación.
                    outgoing_labels = [
                        l.lower()
                        for l in conversation.get("labels", [])
                    ]
                    template_name = (
                        body.get("additional_attributes", {})
                        .get("template_params", {})
                        .get("name", "")
                        or ""
                    )
                    if (
                        SURVEY_LABEL in outgoing_labels
                        or template_name == "encuesta_satisfaccion_servicio_v2"
                    ):
                        logger.info(
                            f"event=survey_outgoing_ignored "
                            f"conversation_id={conv_id} "
                            f"template_name={template_name or 'none'}"
                        )
                        return {"status": "survey_outgoing_ignored"}

                    sender_key = f"chatwoot_{conv_id}"
                    await db.set_conversation_mode(sender_key, "human")

                    # Also set by phone so WhatsApp webhook respects it
                    contact_inbox = conversation.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")
                    if source_id:
                        await db.set_conversation_mode(source_id, "human")

                    logger.info(f"Agent message detected — human mode set for conversation {conv_id}")
                    return {"status": "agent_mode"}

            logger.info(f"Ignoring Chatwoot event: {event}, type: {message_type}")
            return {"status": "ignored"}

        # Ignore webhook retries of a message already processed (Chatwoot
        # reenvia el evento si no recibe 200 a tiempo, p.ej. mientras la IA
        # genera respuesta). El "id" de un evento message_created es el id
        # del mensaje, unico por evento.
        event_id = body.get("id")
        if event_id is not None and _is_duplicate_event(f"cw:{event_id}"):
            logger.info(f"Duplicate Chatwoot webhook delivery ignored (id={event_id})")
            return {"status": "duplicate_ignored"}

        # Extract data from Chatwoot payload
        # Chatwoot envía "content": null (no ausente) en mensajes de solo
        # adjunto (imagen/audio/video sin texto). Usar "or" en vez de
        # .get(..., "") para que null tambien caiga a cadena vacia y no
        # rompa el .strip() de mas abajo con un mensaje sin respuesta.
        content = body.get("content") or ""
        conversation = body.get("conversation", {})
        conversation_id = conversation.get("id")
        sender = body.get("sender", {})
        contact_id = sender.get("id")

        if not conversation_id:
            logger.warning("Missing conversation_id in Chatwoot webhook")
            return {"status": "missing data"}

        # Survey response exclusion: si el texto es exactamente una de las
        # respuestas de la encuesta de satisfacción y la conversación está
        # marcada por n8n como pendiente de esa encuesta, el bot debe
        # ignorar el mensaje para que no compitan ambos sistemas por
        # responder al cliente. Se comprueba antes de cualquier otro
        # procesamiento (prompt, LLM, guardado de historial, etc.) y solo
        # consulta las etiquetas de Chatwoot cuando el texto coincide
        # exactamente, para no añadir overhead al resto de mensajes.
        normalized_text = content.strip().lower()
        if normalized_text in SURVEY_RESPONSES:
            # Chatwoot incluye las etiquetas actuales en el propio payload del
            # webhook (conversation.labels). Si están presentes se usan
            # directamente para evitar la llamada HTTP. Solo si el payload no
            # incluye etiquetas se hace la consulta como fallback.
            payload_labels = [l.lower() for l in conversation.get("labels", [])]
            if payload_labels:
                if SURVEY_LABEL in payload_labels:
                    log_response = normalized_text.replace(" ", "_")
                    logger.info(
                        f"event=survey_response_ignored conversation_id={conversation_id} "
                        f"survey_response={log_response} reason=pending_survey_label"
                    )
                    return {
                        "status": "survey_response_ignored",
                        "conversation_id": conversation_id,
                        "survey_response": normalized_text,
                    }
                # El payload tiene etiquetas pero ninguna es la de encuesta →
                # procesamiento normal, sin llamada HTTP adicional.
            else:
                # El payload no incluye etiquetas — usar HTTP como fallback.
                try:
                    labels = await chatwoot_svc.get_conversation_labels(conversation_id)
                    if SURVEY_LABEL in labels:
                        log_response = normalized_text.replace(" ", "_")
                        logger.info(
                            f"event=survey_response_ignored conversation_id={conversation_id} "
                            f"survey_response={log_response} reason=pending_survey_label"
                        )
                        return {
                            "status": "survey_response_ignored",
                            "conversation_id": conversation_id,
                            "survey_response": normalized_text,
                        }
                except Exception:
                    log_response = normalized_text.replace(" ", "_")
                    logger.exception(
                        f"event=survey_label_check_failed conversation_id={conversation_id} "
                        f"survey_response={log_response}"
                    )
                    # Fail-open: la consulta de etiquetas falló, se continúa
                    # con el procesamiento normal del bot para no dejar al
                    # cliente sin respuesta.

        # Detect attachments (images, audio, video, files) or empty content
        attachments = body.get("attachments", [])
        if attachments or not content:
            if attachments:
                file_type = attachments[0].get("file_type", "file")
                logger.info(f"Received {file_type} attachment in conversation {conversation_id}")
            else:
                logger.info(f"Empty content in conversation {conversation_id}")
            if _should_send_attachment_notice(f"chatwoot_{conversation_id}"):
                await chatwoot_svc.send_message(
                    conversation_id,
                    "Por ahora solo puedo leer mensajes de texto. 😊 ¿Podrías describir tu consulta con palabras?",
                )
            else:
                logger.info(f"Attachment notice suppressed for conversation {conversation_id} (cooldown)")
            return {"status": "non-text ignored"}

        # Ignore non-text content_type
        content_type = body.get("content_type", "text")
        if content_type != "text":
            logger.info(f"Ignoring non-text content_type: {content_type}")
            if _should_send_attachment_notice(f"chatwoot_{conversation_id}"):
                await chatwoot_svc.send_message(
                    conversation_id,
                    "Por ahora solo puedo leer mensajes de texto. 😊 ¿Podrías describir tu consulta con palabras?",
                )
            else:
                logger.info(f"Attachment notice suppressed for conversation {conversation_id} (cooldown)")
            return {"status": "non-text ignored"}

        # Get the customer's phone number for repair lookups
        # First try source_id from contact_inbox (WhatsApp number)
        phone = None
        contact_inbox = conversation.get("contact_inbox", {})
        source_id = contact_inbox.get("source_id")
        if source_id:
            phone = source_id
            logger.info(f"Phone from source_id: {phone}")

        # Fallback: fetch phone from Chatwoot Contacts API
        if not phone and contact_id:
            phone = await chatwoot_svc.get_contact_phone(contact_id)
            logger.info(f"Phone from Contacts API: {phone}")

        # Use conversation_id as the "sender" key for mode/rate tracking
        sender_key = f"chatwoot_{conversation_id}"
        # Use phone as history key when available so context persists across
        # new conversations (new conversation_id) from the same client.
        history_key = phone if phone else sender_key

        # Per-phone rate limiting
        recent = await db.count_recent_messages(sender_key, seconds=PHONE_RATE_WINDOW)
        if recent >= PHONE_RATE_LIMIT:
            logger.warning(f"Rate limit exceeded for conversation {conversation_id} ({recent} msgs in {PHONE_RATE_WINDOW}s)")
            await chatwoot_svc.send_message(
                conversation_id,
                "Estás enviando mensajes muy rápido. Por favor espera un momento antes de continuar.",
            )
            return {"status": "phone_rate_limited"}

        # Check conversation mode (bot or human)
        mode = await db.get_conversation_mode(sender_key)

        if mode == "human":
            await db.save_message(history_key, "user", content)
            logger.info(f"Message saved for human agent (conversation: {conversation_id})")
            return {"status": "human mode"}

        # Get conversation history
        history = await db.get_history(history_key, limit=20)

        # Si la conversacion es nueva (sin historial) o lleva mas de
        # ESPOCRM_NEW_LEAD_AFTER_SECONDS inactiva, programar un volcado
        # diferido de la conversacion completa hacia EspoCRM.
        last_msg_time = await db.get_last_message_time(history_key)
        now_utc = datetime.now(timezone.utc)
        gap = (now_utc - last_msg_time).total_seconds() if last_msg_time else None
        is_new_session = last_msg_time is None or (
            gap is not None and gap > settings.ESPOCRM_NEW_LEAD_AFTER_SECONDS
        )
        logger.info(
            f"EspoCRM check for {sender_key}: last_msg={last_msg_time}, "
            f"gap={gap}s, threshold={settings.ESPOCRM_NEW_LEAD_AFTER_SECONDS}s, "
            f"is_new_session={is_new_session}"
        )
        if is_new_session:
            sender_name = sender.get("name", "")
            sender_email = sender.get("email", "")
            if not phone:
                phone = sender.get("phone_number", "")
            lead_label = sender_name or phone or f"Conversación {conversation_id}"
            try:
                espocrm_svc.schedule_lead_from_conversation(
                    db=db,
                    sender_key=history_key,
                    lead_label=lead_label,
                    contact_name=sender_name,
                    phone=phone or "",
                    email=sender_email,
                    session_started_at=now_utc,
                )
            except Exception as e:
                logger.error(f"Error scheduling EspoCRM lead: {e}", exc_info=True)

        # Save user message
        await db.save_message(history_key, "user", content)

        # Classify intent (cheap gpt-4o-mini call)
        intent = await classify_intent(
            client=openai_svc.client,
            user_message=content,
            history=history,
        )
        logger.info(f"Intent for conversation {conversation_id}: {intent}")
        logger.info(
            f"[PRICES] needs_prices={intent.needs_prices} | "
            f"PRICES_SHEET_ID_SET={bool(settings.GOOGLE_PRICES_SHEET_ID)}"
        )

        # Handoff to human agent if requested
        if intent.needs_human:
            if is_within_business_hours():
                await db.save_message(history_key, "assistant", HANDOFF_MESSAGE)
                await chatwoot_svc.send_message(conversation_id, HANDOFF_MESSAGE)
                await _handle_handoff(sender_key, conversation_id=conversation_id)
                return {"status": "handoff"}
            else:
                msg = get_outside_hours_message()
                await db.save_message(history_key, "assistant", msg)
                await chatwoot_svc.send_message(conversation_id, msg)
                logger.info(f"Handoff denied for conversation {conversation_id} — outside business hours")
                return {"status": "outside_hours"}

        # Fetch only what's needed based on classification
        extra_context_parts = []

        # Detect returning session (gap > 4 hours)
        if last_msg_time and gap is not None and gap >= 4 * 3600:
            gap_hours = gap / 3600
            extra_context_parts.append(
                f"[SESIÓN RETOMADA]\n"
                f"Han pasado aproximadamente {gap_hours:.0f} horas desde el último mensaje del cliente.\n"
                f"Revisa el historial para identificar el tema de la última consulta y saluda en consecuencia."
            )
            logger.info(f"Returning session detected for {sender_key} (gap: {gap_hours:.1f}h)")

        needs_repair = intent.needs_repair_lookup
        if needs_repair:
            repair_ctx = await _repair_lookup(phone or "", content)
            if repair_ctx:
                extra_context_parts.append(repair_ctx)

        if intent.needs_prices:
            try:
                prices = await sheets_svc.get_all_prices()
                if prices:
                    extra_context_parts.append(sheets_svc.format_prices_for_prompt(prices))
            except Exception as e:
                logger.error(f"Error fetching prices: {e}", exc_info=True)

        if intent.needs_rental_lookup:
            try:
                equipos = await kelatos_svc.get_available_equipos()
                extra_context_parts.append(sheets_svc.format_equipos_for_prompt(equipos))
            except Exception as e:
                logger.error(f"Error fetching rental equipment: {e}", exc_info=True)

        if not intent.wants_appointment and _is_in_appointment_flow(history):
            intent.wants_appointment = True
            logger.info("Forced wants_appointment=True (active appointment flow detected in history)")

        if intent.wants_appointment:
            extra_context_parts.append(calendar_svc.get_appointment_context())

        extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else None

        # Load brand-specific FAQ if classified
        brand_faq = None
        if intent.brand:
            brand_faq = load_brand_faq(intent.brand)

        # Intercept: aceptación/rechazo de presupuesto — respuesta fija, sin LLM
        if _is_budget_decision(content, history):
            ai_response = BUDGET_DECISION_RESPONSE
            logger.info("Budget decision intercepted (Chatwoot), returning fixed response.")
        else:
        # Generate AI response
            ai_response = await openai_svc.generate_response(
                user_message=content,
                history=history,
                extra_context=extra_context,
                brand_faq=brand_faq,
            )
        logger.info("RAW AI RESPONSE (Chatwoot):\n%s", ai_response)

        # Check if AI wants to transfer to agent (product purchase)
        if "TRANSFERIR_AGENTE" in ai_response:
            if is_within_business_hours():
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                full_msg = f"{clean_response}\n\n{HANDOFF_MESSAGE}" if clean_response else HANDOFF_MESSAGE
                await db.save_message(history_key, "assistant", full_msg)
                await chatwoot_svc.send_message(conversation_id, full_msg)
                await _handle_handoff(sender_key, conversation_id=conversation_id)
            else:
                # Outside hours: keep whatever the model already answered, append closing note
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                closing = get_outside_hours_message()
                full_msg = f"{clean_response}\n\n{closing}" if clean_response else closing
                await db.save_message(history_key, "assistant", full_msg)
                await chatwoot_svc.send_message(conversation_id, full_msg)
            return {"status": "handoff"}

        # Check if AI confirmed an appointment or pickup
        clean_response, created_event = await process_ai_calendar_command(
            calendar_service=calendar_svc,
            ai_response=ai_response,
            attendee_phone=phone or sender_key,
            history=history,
        )
        logger.info("CLEAN RESPONSE (Chatwoot):\n%s", clean_response)
        logger.info("CREATED EVENT (Chatwoot): %s", created_event)

        # Save bot response
        await db.save_message(history_key, "assistant", clean_response)

        # Send response via Chatwoot
        await chatwoot_svc.send_message(conversation_id, clean_response)

        logger.info(f"Response sent to conversation {conversation_id}: {clean_response[:100]}...")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing Chatwoot webhook: {e}", exc_info=True)
        return {"status": "error"}


# ── Admin: reset conversation mode ──────────────────────────────────────────


@app.post("/admin/reset-conversation/{conversation_id}")
async def reset_conversation_mode(conversation_id: int, request: Request):
    """
    Fuerza el restablecimiento al modo bot de una conversación de Chatwoot.
    Requiere el mismo token que usa Meta para verificar el webhook (VERIFY_TOKEN).
    Útil para corregir conversaciones que quedaron bloqueadas en human mode
    por mensajes automáticos de n8n antes de aplicar la exclusión de encuesta.
    """
    token = request.headers.get("x-admin-token", "")
    if token != settings.VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    sender_key = f"chatwoot_{conversation_id}"
    await db.set_conversation_mode(sender_key, "bot")
    logger.info(f"Admin reset: conversation {conversation_id} restored to bot mode")
    return {"status": "reset", "conversation_id": conversation_id, "mode": "bot"}
