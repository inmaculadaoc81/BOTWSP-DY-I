import json
import logging
import re
import unicodedata
from dataclasses import dataclass

from openai import AsyncOpenAI

from faq_service import list_available_brands

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "gpt-4o-mini"
CLASSIFIER_MAX_TOKENS = 100


# Algunos nombres que usa el cliente no coinciden con el nombre del archivo FAQ.
# La deteccion local evita que una respuesta variable del clasificador elimine el
# contexto de una marca que aparece literalmente en el mensaje.
_BRAND_ALIASES = {
    "cecotec": "conga",
}


def _normalize_brand_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _detect_explicit_brand(user_message: str, brands: list[str]) -> str | None:
    message = _normalize_brand_text(user_message)
    candidates = {}
    for brand in brands:
        normalized = _normalize_brand_text(brand)
        candidates[normalized] = brand
    candidates.update(_BRAND_ALIASES)

    # Longer names first prevents a short name from winning inside a compound name.
    for name in sorted(candidates, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", message):
            return candidates[name]
    return None


@dataclass
class IntentResult:
    needs_repair_lookup: bool = False
    needs_prices: bool = False
    wants_appointment: bool = False
    needs_human: bool = False
    needs_rental_lookup: bool = False
    brand: str | None = None


async def classify_intent(
    client: AsyncOpenAI,
    user_message: str,
    history: list[dict] | None = None,
) -> IntentResult:
    """Lightweight classification to determine what context is needed."""
    brands = list_available_brands()
    brands_str = ", ".join(brands)

    system_prompt = (
        "Eres un clasificador para un bot de WhatsApp de un servicio técnico de reparaciones. "
        "Dado el mensaje del usuario, devuelve un JSON con estos campos:\n"
        '- "needs_repair_lookup": true si el usuario pregunta por el estado de su reparación, '
        "seguimiento, resguardo, o un equipo que dejó para reparar.\n"
        '- "needs_prices": true si el usuario pregunta por precios, costes, cuánto cuesta, '
        "tarifas, presupuesto, o información de precios de reparación. "
        "También true si menciona una marca/modelo específico y pregunta qué le costaría arreglarlo, "
        "qué precio tiene un servicio concreto, o cuánto vale una reparación o pieza. "
        "En caso de duda, marcar true.\n"
        '- "wants_appointment": true SOLO si el usuario pide EXPLICITAMENTE agendar/reservar/programar/coger una cita, '
        "o pide que le recojan el equipo a domicilio (recogida, mensajero, que pasen a buscarlo). "
        "IMPORTANTE: marcar FALSE si el usuario solo dice que va a ir, va a pasar, va a llevar, "
        "quiere acercarse, o pregunta por horarios/direccion/parking. Eso es walk-in (sin cita) y "
        "el bot no debe pedir datos. Solo true cuando el cliente usa la palabra 'cita', 'reservar', "
        "'agendar', 'recogida', 'recoger', 'mensajero' o equivalente claro.\n"
        '- "needs_human": true SOLO si el usuario pide EXPLICITAMENTE hablar con una persona real, '
        "un agente humano, quiere ser transferido, o expresa frustracion clara con el bot "
        "(ej: 'quiero hablar con alguien de verdad', 'pasame con una persona', 'no me entiendes'). "
        "IMPORTANTE: NO marcar needs_human cuando el usuario describe un problema tecnico, una averia, "
        "o quiere comprar un producto/pieza. Esos casos los maneja el bot. Esto aplica AUNQUE la averia "
        "suene grave, tenga varias piezas/componentes afectados (varios vasos, varias cuchillas, disco "
        "borrado sin copia, etc.), o el usuario ya haya intentado arreglarlo el mismo y le haya dado un error. "
        "Describir una averia — por complicada, inusual o grave que parezca — NUNCA es una peticion de humano. "
        "NUNCA marcar needs_human para preguntas sobre diagnostico, presupuesto, horarios, "
        "direccion, precios o servicios — esas preguntas las responde el bot directamente. "
        "NUNCA marcar needs_human solo porque el mensaje sea ambiguo, incompleto, mencione un "
        "codigo de error, un modelo, una cantidad o un detalle tecnico poco claro. La ambiguedad "
        "NO es una peticion de humano: el bot debe interpretar y, en caso de duda, PREGUNTAR para "
        "confirmar o aclarar el problema antes de nada — nunca transferir por incertidumbre o por "
        "no tener claro un detalle. Solo marcar true ante una peticion EXPLICITA de persona/agente. "
        "Ejemplos que NO son needs_human: 'me podeis dar un diagnostico previo', "
        "'cuanto cuesta reparar', 'podeis diagnosticarlo', 'que precio tiene', "
        "'cuando abris', 'donde estais', 'me aparece el error C347, que puedo hacer', "
        "'no se de que tipo son mis cintas, creo que es VHS o video8', "
        "'tengo una thermomix tm31 y dos vasos, no me funciona, se han desbasculado las cuchillas', "
        "'quisiera reinstalar macos a mi macbook pro 2010, borre el disco y no tenia copia, no me deja "
        "hacerlo manualmente porque me sale error'.\n"
        "IMPORTANTE: si el usuario menciona un numero de resguardo/codigo (normalmente 4 a 6 digitos), "
        "pregunta por el estado o seguimiento de su reparacion/equipo, o RECLAMA porque su reparacion o "
        "envio se esta demorando o no ha llegado en el plazo que le dijeron, eso es needs_repair_lookup=true "
        "y needs_human=FALSE, incluso si el mensaje suena molesto, urgente o con reclamo por el retraso. "
        "La frustracion por una demora del PEDIDO/REPARACION no es lo mismo que frustracion con el bot: "
        "solo marca needs_human=true si el usuario pide explicitamente hablar con una persona o se queja "
        "de que el propio chat/bot no le entiende. Ejemplos que son needs_repair_lookup=true y needs_human=false: "
        "'tengo este codigo 18215', 'queria saber situacion de un ordenador que deje, el resguardo es el "
        "numero 18215, me indicaron que estaria antes del 17 de agosto y no es asi, decirme algo'.\n"
        '- "needs_rental_lookup": true si el usuario pregunta qué equipos hay disponibles para alquilar, '
        "qué modelos tienen, si tienen gaming/Mac/Windows/Surface para alquiler, qué portátiles tienen, "
        "disponibilidad de equipos de alquiler, o cualquier consulta sobre el catálogo o stock de alquiler.\n"
        '- "brand": el slug de la marca si el usuario menciona o pregunta sobre una marca específica. '
        f"Valores válidos: {brands_str}, o null si no menciona ninguna marca.\n"
        "Devuelve SOLO JSON válido, sin explicación."
    )

    messages = [{"role": "system", "content": system_prompt}]

    # Include last 2 history messages for follow-up context
    if history:
        for msg in history[-2:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=messages,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        logger.info(f"Intent classification: {data}")

        result = IntentResult(
            needs_repair_lookup=bool(data.get("needs_repair_lookup", False)),
            needs_prices=bool(data.get("needs_prices", False)),
            wants_appointment=bool(data.get("wants_appointment", False)),
            needs_human=bool(data.get("needs_human", False)),
            needs_rental_lookup=bool(data.get("needs_rental_lookup", False)),
            brand=data.get("brand"),
        )

        explicit_brand = _detect_explicit_brand(user_message, brands)
        if explicit_brand:
            result.brand = explicit_brand
        elif result.brand:
            result.brand = _BRAND_ALIASES.get(_normalize_brand_text(result.brand), result.brand)

        # Validate brand against known list
        if result.brand and _normalize_brand_text(result.brand) not in brands:
            logger.warning(f"Unknown brand '{result.brand}', ignoring")
            result.brand = None
        elif result.brand:
            result.brand = _normalize_brand_text(result.brand)

        # Fallback: si el mensaje contiene palabras de precio y no se activó needs_prices, forzarlo.
        # Cubre casos donde gpt-4o-mini clasifica como consulta de reparación pero no de precio.
        if not result.needs_prices:
            _PRICE_KEYWORDS = (
                "precio", "coste", "cuesta", "cuanto", "cuánto",
                "tarifa", "presupuesto", "vale", "cobran", "cobráis",
                "reparación cuesta", "repair cost",
            )
            msg_lower = user_message.lower()
            if any(kw in msg_lower for kw in _PRICE_KEYWORDS):
                result.needs_prices = True
                logger.info("needs_prices forced True via keyword fallback")

        return result

    except Exception as e:
        logger.error(f"Intent classification failed: {e}", exc_info=True)
        # Fail-open: fetch everything (current behavior)
        return IntentResult(needs_repair_lookup=True, needs_prices=True, brand=None)
