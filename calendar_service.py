import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MADRID_TZ = ZoneInfo("Europe/Madrid")

# Keep in sync with HOLIDAYS_2026 in main.py and _HOLIDAYS in openai_service.py
_HOLIDAYS = {
    # Nacionales
    "01-01", "01-06", "04-02", "04-03", "05-01",
    "08-15", "10-12", "11-02", "12-07", "12-08", "12-25",
    # Madrid
    "05-02", "05-15", "11-09",
}

# Validaciones de datos para citas/recogidas (sanity checks que el LLM puede saltarse).
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_PLACEHOLDER_NAMES = {"cliente", "anonimo", "anónimo", "sin nombre", "n/a", "test", "prueba"}

# Enlaces de pago para alquiler con envio a domicilio de 7 dias o mas (envio gratuito).
# Segun tipo de equipo: portatiles normales (Windows/Mac) vs Gaming/Microsoft Surface.
_RENTAL_PAYMENT_LINKS = {
    "normal": {
        "rental": "https://sis.redsys.es/tiendaWeb/item/NDk4OzU=",
        "deposit": "https://sis.redsys.es/tiendaWeb/item/NDk4OzY=",
    },
    "gaming_surface": {
        "rental": "https://sis.redsys.es/tiendaWeb/item/NDk4Ozc=",
        "deposit": "https://sis.redsys.es/tiendaWeb/item/NDk4Ozg=",
    },
}


def _rental_duration_is_7_plus_days(duracion: str) -> bool:
    """True si la duracion del alquiler equivale a 7 dias o mas (envio gratuito)."""
    text = (duracion or "").strip().lower()
    if "semana" in text or "mes" in text:
        return True
    match = re.search(r"(\d+)\s*d", text)
    if match:
        return int(match.group(1)) >= 7
    return False


def _rental_payment_links(tipo_equipo: str) -> dict:
    """CASO 2 (Gaming/Surface) o CASO 1 (resto) segun el tipo de equipo."""
    text = (tipo_equipo or "").lower()
    if "gaming" in text or "surface" in text:
        return _RENTAL_PAYMENT_LINKS["gaming_surface"]
    return _RENTAL_PAYMENT_LINKS["normal"]


class CalendarService:
    """Google Calendar integration."""

    def __init__(self):
        self.calendar_id = settings.GOOGLE_CALENDAR_ID
        self.subject = getattr(settings, "GOOGLE_CALENDAR_SUBJECT", None)
        self._service = None

    def _get_service(self):
        """Build the Calendar API service."""
        if self._service:
            return self._service

        creds = Credentials.from_service_account_file(
            settings.GOOGLE_CREDENTIALS_PATH,
            scopes=SCOPES,
        )

        # Solo usar impersonation si realmente tienes domain-wide delegation configurado
        if self.subject:
            try:
                creds = creds.with_subject(self.subject)
            except Exception:
                logger.warning(
                    "No se pudo aplicar with_subject; se usará la service account sin impersonation.",
                    exc_info=True,
                )

        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    async def create_event(
        self,
        title: str,
        start_iso: str,
        duration_minutes: int = 30,
        description: str = "",
        attendee_phone: str = "",
    ) -> dict | None:
        """Create a calendar event."""
        service = self._get_service()

        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            end_dt = start_dt + timedelta(minutes=duration_minutes)

            full_description = description.strip()
            if attendee_phone:
                full_description = f"{full_description}\nTeléfono: {attendee_phone}".strip()

            event_body = {
                "summary": title,
                "description": full_description,
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
            }

            logger.info(
                "Creating calendar event",
                extra={
                    "calendar_id": self.calendar_id,
                    "title": title,
                    "start_iso": start_dt.isoformat(),
                },
            )

            event = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event_body,
                )
                .execute()
            )

            logger.info(
                "Calendar event created successfully",
                extra={
                    "event_id": event.get("id"),
                    "htmlLink": event.get("htmlLink"),
                },
            )
            return event

        except Exception as e:
            logger.exception(f"Error creating calendar event: {e}")
            return None

    def get_busy_slots_for_day(self, datetime_iso: str) -> list[tuple]:
        """Return list of (start, end) datetime pairs for all events on the given day."""
        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        try:
            service = self._get_service()
            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy = []
            for event in result.get("items", []):
                s_str = event.get("start", {}).get("dateTime")
                e_str = event.get("end", {}).get("dateTime")
                if s_str and e_str:
                    s = datetime.fromisoformat(s_str).astimezone(MADRID_TZ)
                    e = datetime.fromisoformat(e_str).astimezone(MADRID_TZ)
                    busy.append((s, e))
            return busy
        except Exception as exc:
            logger.error(f"Error fetching busy slots: {exc}", exc_info=True)
            return []

    def get_available_slots(self, datetime_iso: str, duration_minutes: int = 30) -> list[str]:
        """Return free HH:MM slots within 10:00-17:00 for the given day."""
        busy = self.get_busy_slots_for_day(datetime_iso)

        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        free = []
        current = local.replace(hour=10, minute=0, second=0, microsecond=0)
        cutoff = local.replace(hour=17, minute=0, second=0, microsecond=0)

        while current <= cutoff:
            slot_end = current + timedelta(minutes=duration_minutes)
            if not _overlaps(current, slot_end, busy):
                free.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)

        return free

    def get_busy_slots_range(self, days_ahead: int = 14) -> dict[str, list[str]]:
        """Fetch busy HH:MM slots for the next `days_ahead` days in one API call.

        Returns { "YYYY-MM-DD": ["HH:MM", ...] }
        Days not present in the result have no events (all slots free).
        """
        try:
            service = self._get_service()
            now = datetime.now(MADRID_TZ)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=days_ahead + 1)

            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy: dict[str, list[str]] = {}
            for event in result.get("items", []):
                s_str = event.get("start", {}).get("dateTime")
                if not s_str:
                    continue
                s_dt = datetime.fromisoformat(s_str).astimezone(MADRID_TZ)
                day_key = s_dt.strftime("%Y-%m-%d")
                time_str = s_dt.strftime("%H:%M")
                busy.setdefault(day_key, []).append(time_str)

            return busy
        except Exception as exc:
            logger.error(f"Error fetching busy slots range: {exc}", exc_info=True)
            return {}

    def get_appointment_context(self) -> str:
        """Return appointment instructions as context for the AI."""
        now = datetime.now(MADRID_TZ)
        today = now.strftime("%A %d/%m/%Y")

        # One API call for all busy slots in the next 14 days
        busy_range = self.get_busy_slots_range(days_ahead=14)
        if busy_range:
            busy_lines = [
                f"  {day}: {', '.join(sorted(times))}"
                for day, times in sorted(busy_range.items())
            ]
            busy_block = (
                "[SLOTS OCUPADOS — PROXIMOS 14 DIAS]\n"
                + "\n".join(busy_lines)
                + "\nLos dias/horas que NO aparecen aqui estan LIBRES."
            )
        else:
            busy_block = (
                "[SLOTS OCUPADOS — PROXIMOS 14 DIAS]\n"
                "No hay citas registradas. Todos los horarios 10:00–17:00 estan disponibles."
            )

        return (
            f"[SISTEMA DE CITAS Y ENVIOS]\n"
            f"Fecha actual: {today}\n"
            f"Horario de atencion del local: Lunes a Viernes de 09:30 a 18:00.\n"
            f"Sabados, domingos y festivos cerrados.\n"
            f"\n{busy_block}\n"
            f"\n⚠️ REGLA DE DISPONIBILIDAD — OBLIGATORIO:\n"
            f"En cuanto el cliente proponga fecha y hora concreta para su cita:\n"
            f"  1. Comprueba si ese dia+hora aparece en SLOTS OCUPADOS.\n"
            f"  2. Si esta OCUPADO: comunica al cliente inmediatamente que ese horario no esta disponible y ofrece hasta 6 horas libres de ese mismo dia. NO muestres el resumen de cita hasta que el cliente elija un horario libre.\n"
            f"  3. Si esta LIBRE: entonces muestra el resumen completo para que el cliente confirme.\n"
            f"  4. Si el dia no aparece en la lista de ocupados, todos sus horarios estan disponibles.\n"
            f"\n[FESTIVOS OFICIALES 2026 — LISTA EXACTA]\n"
            f"SOLO bloquear estas fechas como festivos. NO añadir ninguna otra por tu cuenta:\n"
            f"  Nacionales: 1 enero, 6 enero, 3 abril (Viernes Santo), 1 mayo, 15 agosto, 12 octubre, 2 noviembre, 7 diciembre, 8 diciembre, 25 diciembre.\n"
            f"  Madrid: 2 mayo, 15 mayo, 9 noviembre.\n"
            f"❌ El 30 de abril NO es festivo en 2026. Cualquier fecha fuera de la lista anterior es laborable.\n"
            f"\n🚨 RESPUESTA INMEDIATA A FECHA FESTIVA:\n"
            f"Si el cliente menciona una fecha que es festivo (de la lista anterior), responde DE INMEDIATO en ese mismo mensaje:\n"
            f"  '❌ El [fecha] es festivo y permanecemos cerrados. ¿Qué otro día te viene bien? Podemos atenderte cualquier día laborable de lunes a viernes.'\n"
            f"NO esperes a recoger más datos. NO sigas con el flujo de cita. Primero corrige la fecha, luego continúa.\n"
            f"Ejemplo: cliente dice 'quiero cita el 15 de mayo' → responder inmediatamente que es festivo y pedir otra fecha.\n"
            f"\n🚨 DIFERENCIA CRITICA — LEE ANTES DE ACTUAR:\n"
            f"\nHay TRES situaciones distintas. NO las confundas:\n"
            f"\n1. WALK-IN (DEFAULT — el cliente solo quiere venir al local sin agendar):\n"
            f"   Senales: 'voy a llevar', 'me acerco al local', 'paso por la tienda', 'voy mañana',\n"
            f"   'puedo ir hoy?', '¿que horario tienen?', '¿donde estais?', '¿hay parking?'.\n"
            f"   ❌ NO pidas NINGUN dato (ni nombre ni email ni telefono ni cita).\n"
            f"   ✅ Solo da: direccion (C/ Joaquin Maria Lopez 26, Madrid), horario (L-V 09:30-18:00), parking.\n"
            f"   ✅ Recuerdale que puede pasar SIN CITA PREVIA dentro del horario.\n"
            f"   ✅ NO ofrezcas agendar cita salvo que el cliente lo pida explicitamente.\n"
            f"\n2. CITA EN EL LOCAL (solo si el cliente DICE LA PALABRA 'cita', 'agendar', 'reservar', 'programar'):\n"
            f"   Solo entonces entras en el flujo de pedir nombre+email+telefono+motivo+dia+hora.\n"
            f"\n3. RECOGIDA A DOMICILIO (mensajero) — solo si el cliente pide explicitamente que pasen a recoger:\n"
            f"   Senales: 'recogida', 'que lo recojan', 'mensajero', 'no puedo ir', 'quiero enviar', 'pasen a buscarlo'.\n"
            f"   Coste: 15€ por equipo, solo peninsula.\n"
            f"\n⚠️ REGLA ANTI-AGENDAMIENTO ACCIDENTAL:\n"
            f"- Si el cliente solo respondio 'si' a algo que TU le preguntaste y NO se ve en la conversacion una peticion explicita previa de cita/recogida del CLIENTE, NUNCA registres cita ni emitas CONFIRMAR_*.\n"
            f"- Si dudas si quiere walk-in o cita, pregunta de forma neutra: '¿Prefieres pasar directamente al local sin cita o agendar una cita con un tecnico?'\n"
            f"\nPROTOCOLO DE CITA (cliente viene al local):\n"
            f"1. Datos necesarios: nombre completo + correo electronico + numero de telefono + motivo (equipo + problema).\n"
            f"2. Si falta alguno, pidelo antes de continuar.\n"
            f"3. Pregunta fecha y hora preferida.\n"
            f"4. Las citas solo se pueden agendar de Lunes a Viernes entre las 10:00 y las 17:00. La hora MAXIMA es las 17:00. Ningun slot posterior a las 17:00 es valido.\n"
            f"   ❌ EJEMPLOS DE HORAS RECHAZADAS: 17:30, 18:00, 05:30, cualquier hora antes de las 10:00 o despues de las 17:00.\n"
            f"   Si el cliente pide 17:30 o cualquier hora despues de las 17:00 → decirle: 'Lo siento, la ultima cita disponible es a las 17:00. ¿Te viene bien esa hora u otra entre las 10:00 y las 17:00?'\n"
            f"   ❌ NUNCA generes CONFIRMAR_CITA con una hora fuera del rango 10:00-17:00.\n"
            f"5. En cuanto el cliente proponga hora: verifica disponibilidad en SLOTS OCUPADOS (arriba). Si esta ocupado, dilo y ofrece alternativas ANTES de mostrar el resumen. Si es festivo de la lista oficial, indica que esta cerrado y pide otro dia.\n"
            f"6. Cuando tengas TODOS los datos Y el slot este libre, muestra un RESUMEN para que el cliente confirme:\n"
            f"   '📋 *Resumen de tu cita:*\n"
            f"   👤 Nombre: [nombre]\n"
            f"   🔧 Motivo: [equipo + problema]\n"
            f"   📅 Fecha: [fecha y hora]\n"
            f"   📍 Lugar: C/ Joaquin Maria Lopez 26, Madrid\n"
            f"   ¿Es correcto?'\n"
            f"7. CUANDO EL CLIENTE CONFIRMA (dice si, correcto, ok, perfecto, dale, vale, etc.) tu respuesta DEBE contener SIEMPRE DOS PARTES (las dos, no una o la otra):\n"
            f"   PARTE A (texto visible al cliente): 'Perfecto 😊 Estoy gestionando tu cita. En cuanto quede registrada, te enviaremos la confirmacion final.'\n"
            f"   PARTE B (linea de comando interna, en una linea aparte al final, el cliente NO la ve): CONFIRMAR_CITA|<datetime_iso>|<nombre_cliente>|<motivo>\n"
            f"   EJEMPLO completo de respuesta valida cuando el cliente dice 'si':\n"
            f"   ---\n"
            f"   Perfecto 😊 Estoy gestionando tu cita. En cuanto quede registrada, te enviaremos la confirmacion final.\n"
            f"\n"
            f"   CONFIRMAR_CITA|2026-03-27T10:00:00+01:00|Juan Garcia|Reparacion portatil HP\n"
            f"   ---\n"
            f"8. NUNCA omitir la linea CONFIRMAR_CITA al confirmar. Sin esa linea, la cita NO se registra en el sistema.\n"
            f"\nPROTOCOLO DE ENVIO (mensajero recoge a domicilio):\n"
            f"⚠️ Este flujo NO requiere recopilar ningun dato del cliente por el chat. NUNCA pidas nombre, DNI/NIE/CIF, correo, telefono ni direccion. El cliente completa todos esos datos directamente en el enlace de pago.\n"
            f"1. La recogida esta disponible para cualquier equipo que Kelatos atiende. Si hacemos diagnostico de ese equipo, hay recogida. No hay restriccion adicional por tipo.\n"
            f"2. Informar del coste: *30€ (IVA incluido)* — recogida + envio de vuelta.\n"
            f"3. ⚠️ AVISO CORREOS: Actualmente Correos NO permite elegir dia de recogida. NO pidas dia preferido. NO prometas ni confirmes fechas ni horas de recogida al cliente.\n"
            f"4. EN CUANTO el cliente confirme que quiere la recogida (sin pedirle ningun dato antes), tu respuesta DEBE contener SIEMPRE DOS PARTES (las dos, no una o la otra):\n"
            f"   PARTE A (texto visible al cliente): 'Para tramitar la recogida, realiza el pago de *30€ (IVA incluido)* — este precio incluye la recogida en tu domicilio y el envío de vuelta una vez reparado — a través de este enlace, donde también completarás tus datos:\n"
            f"   💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n"
            f"   Una vez realizado el pago, envía el comprobante a soporte@kelatos.com y gestionamos la recogida con Correos. 🚚\n"
            f"   Recuerda embalar bien el equipo para protegerlo durante el transporte.'\n"
            f"   PARTE B (linea de comando interna, en una linea aparte al final, el cliente NO la ve): CONFIRMAR_ENVIO|<datetime_iso_actual>|Pendiente|<motivo>\n"
            f"   EJEMPLO completo de respuesta valida cuando el cliente confirma que quiere la recogida:\n"
            f"   ---\n"
            f"   Para tramitar la recogida, realiza el pago de *30€ (IVA incluido)* — este precio incluye la recogida en tu domicilio y el envío de vuelta una vez reparado — a través de este enlace, donde también completarás tus datos:\n"
            f"   💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n"
            f"   Una vez realizado el pago, envía el comprobante a soporte@kelatos.com y gestionamos la recogida con Correos. 🚚\n"
            f"   Recuerda embalar bien el equipo para protegerlo durante el transporte.\n"
            f"\n"
            f"   CONFIRMAR_ENVIO|2026-04-22T10:00:00+02:00|Pendiente|Dyson SV10 ruidos\n"
            f"   ---\n"
            f"5. NUNCA omitir la linea CONFIRMAR_ENVIO al confirmar. Sin esa linea, la recogida NO se registra en el sistema.\n"
            f"6. ❌ NUNCA muestres un resumen pidiendo nombre, DNI, email o direccion antes de enviar el enlace de pago. El unico dato que necesitas ya lo tienes: el equipo/motivo de la reparacion.\n"
            f"\nIMPORTANTE:\n"
            f"- NUNCA generes CONFIRMAR_CITA ni CONFIRMAR_ENVIO sin mostrar primero el resumen y recibir confirmacion explicita del cliente.\n"
            f"- Si el cliente dice que algun dato es incorrecto, corrigelo y muestra el resumen de nuevo.\n"
            f"- Si el cliente NO ha confirmado, NO incluyas ninguna linea CONFIRMAR.\n"
            f"- Cuando el cliente SI confirma, es OBLIGATORIO incluir la linea CONFIRMAR_CITA o CONFIRMAR_ENVIO. La linea es un comando interno que el sistema procesa para registrar la cita/recogida en Google Calendar. Si olvidas esa linea, la cita no se registra y el cliente se queda sin reserva.\n"
            f"- La linea CONFIRMAR siempre va al final, sola en su propia linea, separada por un salto de linea del resto del mensaje.\n"
            f"- No repitas el saludo inicial si ya estabas en la misma conversacion.\n"
            f"- Fuera de horario puedes seguir respondiendo consultas informativas; solo aclara el horario si el cliente quiere ir, entregar, recoger o agendar.\n"
            f"\n🚨 ALQUILER DE ORDENADORES — PROTOCOLO COMPLETAMENTE DIFERENTE:\n"
            f"Los protocolos CITA y ENVIO de arriba son EXCLUSIVAMENTE para reparaciones y diagnosticos.\n"
            f"Si el cliente quiere ALQUILAR un ordenador, NO uses el protocolo de cita ni el de envio.\n"
            f"❌ NUNCA pidas 'motivo (equipo + problema)' para un alquiler. El alquiler no tiene motivo de reparacion.\n"
            f"❌ NUNCA uses CONFIRMAR_CITA ni CONFIRMAR_ENVIO para un alquiler.\n"
            f"✅ Para alquiler, sigue las instrucciones del SERVICIO DE ALQUILER DE ORDENADORES (PASOS 1-5) del sistema principal.\n"
            f"✅ Para alquiler con envio a domicilio, envia directamente el enlace de pago (NO pidas datos personales) y usa CONFIRMAR_ALQUILER."
        )


def extract_confirmation_command(ai_response: str) -> dict | None:
    """
    Busca una línea CONFIRMAR_CITA|..., CONFIRMAR_ENVIO|... o CONFIRMAR_ALQUILER|...
    y devuelve un dict con los datos.
    """
    if not ai_response:
        return None

    lines = [line.strip() for line in ai_response.splitlines() if line.strip()]

    for line in lines:
        if line.startswith("CONFIRMAR_CITA|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                logger.warning("Formato inválido en CONFIRMAR_CITA", extra={"line": line})
                return None

            return {
                "type": "cita",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_ENVIO|"):
            # Formato minimo (actual): datetime|nombre(o "Pendiente")|motivo
            # Se aceptan campos extra (direccion/dni/email) por compatibilidad, pero no son obligatorios:
            # el cliente los completa directamente en el enlace de pago, no por el chat.
            parts = line.split("|", 6)
            if len(parts) not in (4, 5, 6, 7):
                logger.warning("Formato inválido en CONFIRMAR_ENVIO", extra={"line": line})
                return None

            return {
                "type": "envio",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "address": parts[4].strip() if len(parts) >= 5 else "",
                "dni_nie_cif": parts[5].strip() if len(parts) >= 6 else "",
                "email": parts[6].strip() if len(parts) == 7 else "",
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_ALQUILER|"):
            # Format: CONFIRMAR_ALQUILER|datetime_iso|nombre|tipo_equipo|duracion|modalidad|info_entrega
            parts = line.split("|", 6)
            if len(parts) != 7:
                logger.warning("Formato inválido en CONFIRMAR_ALQUILER", extra={"line": line})
                return None

            return {
                "type": "alquiler",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "tipo_equipo": parts[3].strip(),
                "duracion": parts[4].strip(),
                "modalidad": parts[5].strip(),
                "info_entrega": parts[6].strip(),
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_DEVOLUCION|"):
            # Format: CONFIRMAR_DEVOLUCION|datetime_iso|nombre|direccion|resguardo
            parts = line.split("|", 4)
            if len(parts) != 5:
                logger.warning("Formato inválido en CONFIRMAR_DEVOLUCION", extra={"line": line})
                return None

            return {
                "type": "devolucion",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "address": parts[3].strip(),
                "resguardo": parts[4].strip(),
                "raw_line": line,
            }

    return None


def strip_confirmation_command(ai_response: str) -> str:
    """
    Elimina la línea CONFIRMAR_* del texto para no enviársela al usuario.
    """
    if not ai_response:
        return ai_response

    clean_lines = []
    for line in ai_response.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("CONFIRMAR_CITA|")
            or stripped.startswith("CONFIRMAR_ENVIO|")
            or stripped.startswith("CONFIRMAR_ALQUILER|")
            or stripped.startswith("CONFIRMAR_DEVOLUCION|")
        ):
            continue
        clean_lines.append(line)

    return "\n".join(clean_lines).strip()


def _overlaps(slot_start: datetime, slot_end: datetime, busy: list[tuple]) -> bool:
    """True if [slot_start, slot_end) overlaps any (start, end) in busy."""
    for bs, be in busy:
        if slot_start < be and slot_end > bs:
            return True
    return False


def _conversation_text(history: list[dict] | None) -> str:
    """Concatena el contenido de todos los mensajes del historial."""
    if not history:
        return ""
    return "\n".join(msg.get("content", "") for msg in history if msg.get("content"))


def _is_real_name(name: str) -> bool:
    """Comprueba que un nombre sea plausible: 2+ palabras y no un placeholder."""
    name = (name or "").strip()
    if not name or len(name) < 3:
        return False
    if name.lower() in _PLACEHOLDER_NAMES:
        return False
    return len(name.split()) >= 2


def _validate_appointment(
    command: dict,
    history: list[dict] | None,
    sender_phone: str,
) -> tuple[bool, list[str]]:
    """Valida que el comando CONFIRMAR_* tenga todos los datos requeridos.

    Devuelve (is_valid, lista_de_datos_faltantes). El historial es la fuente
    de verdad para email, telefono y direccion (no se confia en lo que el LLM
    haya emitido en la linea CONFIRMAR_).
    """
    missing: list[str] = []
    history_text = _conversation_text(history)
    cmd_type = command.get("type", "")

    # Nombre: el LLM lo emite en la linea CONFIRMAR; comprobamos que sea real.
    # Para alquiler a domicilio y para recogida (envio) el cliente completa sus datos
    # directamente en el enlace de pago, no por el chat, asi que se acepta "Pendiente".
    customer_name = command.get("customer_name", "")
    es_alquiler_domicilio = (
        cmd_type == "alquiler"
        and "domicilio" in (command.get("modalidad") or "").strip().lower()
    )
    es_envio_domicilio = cmd_type == "envio"
    if not es_alquiler_domicilio and not es_envio_domicilio and not _is_real_name(customer_name):
        missing.append("nombre completo del cliente")

    # Motivo: solo para cita y envio (no para alquiler en tienda). Para envio, el motivo
    # ya se conoce por la conversacion previa sobre el equipo, no se pide como dato nuevo.
    if cmd_type in ("cita", "envio"):
        reason = (command.get("reason") or "").strip()
        if not reason or len(reason) < 3:
            missing.append("motivo (equipo + problema)")

    # Para alquiler (tienda o domicilio) y para recogida (envio): no se piden datos por chat.
    # El cliente completa sus datos (nombre, direccion, DNI, email) directamente en el enlace de pago.
    es_alquiler = cmd_type == "alquiler"

    if not es_alquiler and not es_envio_domicilio:
        # Email: tiene que estar en algun mensaje del cliente.
        if not _EMAIL_RE.search(history_text):
            missing.append("correo electronico")

        # Telefono: o ya lo tenemos del remitente o aparece en el historial.
        has_phone = False
        if sender_phone:
            digits = re.sub(r"\D", "", sender_phone)
            has_phone = len(digits) >= 9
        if not has_phone and not _PHONE_RE.search(history_text):
            missing.append("numero de telefono")

    # Fecha y hora: solo validar para citas (para recogidas/envios Correos decide la fecha).
    if cmd_type == "cita":
        try:
            dt = datetime.fromisoformat(command["datetime_iso"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
            now = datetime.now(MADRID_TZ)

            if local < now:
                missing.append("fecha y hora futura (la indicada ya paso)")
            elif local.weekday() >= 5:
                missing.append("dia de lunes a viernes (no se atiende fines de semana)")
            elif local.strftime("%m-%d") in _HOLIDAYS:
                missing.append(f"dia laborable (el {local.strftime('%d/%m')} es festivo, no se atiende)")
            else:
                # Ventana estricta: 10:00 – 17:00 (último slot válido a las 17:00 exactas).
                total_mins = local.hour * 60 + local.minute
                if not (10 * 60 <= total_mins <= 17 * 60):
                    missing.append("hora entre las 10:00 y las 17:00 (último slot a las 17:00 en punto)")
        except (ValueError, KeyError, TypeError):
            missing.append("fecha y hora valida")

    # Direccion: obligatoria para devoluciones (envio de vuelta al cliente tras reparar,
    # no tiene enlace de pago con formulario). Para recogida (envio) NO se pide: el cliente
    # completa la direccion directamente en el enlace de pago.
    if command.get("type") == "devolucion":
        address = (command.get("address") or "").strip()
        if not address or len(address.split()) < 3 or not any(c.isdigit() for c in address):
            missing.append("direccion completa (calle, numero, codigo postal y ciudad)")

    if command.get("type") == "alquiler":
        if not (command.get("tipo_equipo") or "").strip():
            missing.append("tipo de equipo (Windows, Mac, Surface, Gaming)")
        if not (command.get("duracion") or "").strip():
            missing.append("duracion del alquiler")
        modalidad = (command.get("modalidad") or "").strip().lower()
        if not modalidad:
            missing.append("modalidad de entrega (tienda o domicilio)")
        elif "domicilio" in modalidad or "envio" in modalidad:
            pass  # datos se completan en el enlace de pago

    return (len(missing) == 0, missing)


def _missing_data_message(command_type: str, missing: list[str]) -> str:
    """Construye un mensaje cordial pidiendo los datos que faltan."""
    if len(missing) == 1:
        falta = missing[0]
    else:
        falta = ", ".join(missing[:-1]) + f" y {missing[-1]}"

    if command_type == "envio":
        accion = "registrar la recogida"
    elif command_type == "devolucion":
        accion = "registrar el envío de vuelta"
    elif command_type == "alquiler":
        accion = "registrar tu solicitud de alquiler"
    else:
        accion = "registrar tu cita"

    return (
        f"Antes de {accion} necesito que me confirmes: {falta}. "
        f"¿Me lo facilitas, por favor? 😊"
    )


async def process_ai_calendar_command(
    calendar_service: CalendarService,
    ai_response: str,
    attendee_phone: str = "",
    history: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """
    1. Limpia el mensaje para el usuario
    2. Detecta si hay comando CONFIRMAR_*
    3. Valida que estan todos los datos requeridos en el historial
    4. Solo si la validacion pasa, crea el evento real en Google Calendar
    5. Devuelve:
       - user_message: texto limpio para enviar al cliente
       - created_event: evento creado o None
    """
    user_message = strip_confirmation_command(ai_response)
    command = extract_confirmation_command(ai_response)

    if not command:
        return user_message, None

    # Validacion en codigo: bloquea el LLM si trata de confirmar sin datos.
    is_valid, missing = _validate_appointment(command, history, attendee_phone)
    if not is_valid:
        logger.warning(
            "Bloqueado CONFIRMAR_%s por datos faltantes",
            command["type"].upper(),
            extra={"missing": missing, "command": command},
        )
        return _missing_data_message(command["type"], missing), None

    created_event = None

    if command["type"] == "cita":
        # Verificar disponibilidad del slot antes de crear el evento
        try:
            req_dt = datetime.fromisoformat(command["datetime_iso"])
            if req_dt.tzinfo is None:
                req_dt = req_dt.replace(tzinfo=MADRID_TZ)
            req_start = req_dt.astimezone(MADRID_TZ)
            req_end = req_start + timedelta(minutes=30)

            busy = calendar_service.get_busy_slots_for_day(command["datetime_iso"])
            if _overlaps(req_start, req_end, busy):
                req_time = req_start.strftime("%H:%M")
                req_date = req_start.strftime("%d/%m/%Y")
                available = calendar_service.get_available_slots(command["datetime_iso"])
                if available:
                    alts = ", ".join(available[:6])
                    return (
                        f"Lo siento 😊 La hora *{req_time}* del {req_date} ya está ocupada.\n"
                        f"Horas disponibles ese día: *{alts}*\n"
                        f"¿Cuál de estas horas te vendría mejor?"
                    ), None
                else:
                    return (
                        f"Lo siento 😊 El día {req_date} no tenemos horas disponibles entre las 10:00 y las 17:00.\n"
                        f"¿Te gustaría intentar con otro día?"
                    ), None
        except Exception as exc:
            logger.error(f"Error checking slot availability: {exc}", exc_info=True)

        created_event = await calendar_service.create_event(
            title=f"CITA: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=command["reason"],
            attendee_phone=attendee_phone,
        )

        if created_event:
            start_dt = datetime.fromisoformat(command["datetime_iso"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            pretty_date = start_dt.astimezone(MADRID_TZ).strftime("%d/%m/%Y")
            pretty_time = start_dt.astimezone(MADRID_TZ).strftime("%H:%M")

            user_message = (
                f"✅ Tu cita ha sido registrada para el {pretty_date} a las {pretty_time}.\n"
                f"Te esperamos en C/ Joaquín María López 26, Madrid."
            )
        else:
            user_message = (
                "Perfecto 😊 He recibido tu solicitud de cita, pero ahora mismo no he podido registrarla automáticamente.\n"
                "Un técnico la revisará y te confirmará en breve."
            )

    elif command["type"] == "envio":
        # El cliente completa nombre, direccion, DNI y email en el enlace de pago,
        # no por el chat, asi que esos campos suelen venir vacios ("Pendiente"/"").
        address_info = f"\nDirección: {command['address']}" if command.get("address") else ""
        dni_info = f"\nDNI/NIE/CIF: {command['dni_nie_cif']}" if command.get("dni_nie_cif") else ""
        email_info = f"\nEmail: {command['email']}" if command.get("email") else ""
        created_event = await calendar_service.create_event(
            title=f"RECOGIDA: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=f"{command['reason']}{address_info}{dni_info}{email_info}",
            attendee_phone=attendee_phone,
        )

        payment_msg = (
            "Para tramitar la recogida con Correos es necesario realizar el pago de *30€ (IVA incluido)* "
            "— recogida + envío de vuelta — a través del siguiente enlace:\n\n"
            "💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n\n"
            "Una vez realizado el pago, envía el comprobante a soporte@kelatos.com y gestionamos la recogida con Correos. 🚚"
        )

        if created_event:
            user_message = f"✅ ¡Solicitud registrada!\n\n{payment_msg}"
        else:
            user_message = f"✅ Hemos recibido tu solicitud de recogida.\n\n{payment_msg}"

    elif command["type"] == "alquiler":
        description = (
            f"Tipo de equipo: {command['tipo_equipo']}\n"
            f"Duración: {command['duracion']}\n"
            f"Modalidad: {command['modalidad']}\n"
            f"Entrega: {command['info_entrega']}\n"
            f"Teléfono cliente: {attendee_phone}"
        )
        created_event = await calendar_service.create_event(
            title=f"ALQUILER: {command['customer_name']} — {command['tipo_equipo']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=description,
            attendee_phone=attendee_phone,
        )

        envio_gratis = (
            "domicilio" in (command.get("modalidad") or "").strip().lower()
            and _rental_duration_is_7_plus_days(command.get("duracion", ""))
        )

        if envio_gratis:
            links = _rental_payment_links(command.get("tipo_equipo", ""))
            payment_msg = (
                "Para tramitar el envío a domicilio, realiza estos dos pagos:\n\n"
                f"💳 Pago del alquiler: {links['rental']}\n"
                f"💳 Pago de la fianza: {links['deposit']}\n\n"
                "El envío está incluido, no se cobra aparte por alquilar 7 días o más. 🚚\n\n"
                "Una vez realizados ambos pagos, envía los comprobantes a soporte@kelatos.com y nos pondremos en contacto contigo para coordinar la entrega."
            )
        else:
            payment_msg = (
                "Para tramitar el envío a domicilio, realiza el pago de *30€ (IVA incluido)* "
                "— envío + recogida al finalizar el alquiler — a través de este enlace, donde también completarás tus datos:\n\n"
                "💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n\n"
                "Una vez realizado el pago, envía el comprobante a soporte@kelatos.com y nos pondremos en contacto contigo para coordinar la entrega. 🚚"
            )

        if created_event:
            user_message = (
                f"✅ Tu solicitud de alquiler ha sido registrada.\n\n"
                f"📋 *Resumen:*\n"
                f"💻 Equipo: {command['tipo_equipo']}\n"
                f"📅 Duración: {command['duracion']}\n"
                f"🚚 Modalidad: {command['modalidad']}\n\n"
                f"{payment_msg}"
            )
        else:
            user_message = f"✅ Hemos recibido tu solicitud de alquiler.\n\n{payment_msg}"

    elif command["type"] == "devolucion":
        resguardo_info = f"\nNº Resguardo: {command['resguardo']}" if command.get("resguardo") else ""
        description = (
            f"Dirección de envío: {command['address']}"
            f"{resguardo_info}\n"
            f"Teléfono cliente: {attendee_phone}"
        )
        created_event = await calendar_service.create_event(
            title=f"ENVIO: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=description,
            attendee_phone=attendee_phone,
        )

        if created_event:
            user_message = (
                f"✅ Tu solicitud de envío ha sido registrada.\n\n"
                f"📦 El equipo se enviará a: {command['address']}\n\n"
                f"Un asistente de Kelatos se pondrá en contacto contigo para gestionar el pago y coordinar el envío. ¡Gracias! 😊"
            )
        else:
            user_message = (
                "✅ Hemos recibido tu solicitud de envío.\n\n"
                "Un asistente de Kelatos se pondrá en contacto contigo para gestionar el pago y coordinar el envío. ¡Gracias! 😊"
            )

    return user_message, created_event