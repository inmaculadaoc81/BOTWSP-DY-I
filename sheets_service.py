import logging
import time
import re

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = logging.getLogger(__name__)

# Columnas seguras que SE EXPONEN al modelo (sin datos sensibles ni privados).
# Cualquier columna fuera de esta lista se ignora al formatear el contexto.
# Las cabeceras de la hoja deben coincidir exactamente con estos nombres.
REPAIR_COLUMNS = [
    "resguardo",
    "fecha_recepcion",
    "cliente_nombre",
    "equipo_modelo",
    "sintoma",
    "estado",
    "presupuesto_aceptado_id",
    "tecnico_asignado",
    "fecha_reparacion",
    "resultado_reparacion",
    "motivo_sin_reparacion",
    "fecha_entrega",
    "estado_entrega",
    "tipo_recepcion",
    "entrega_mensajeria",
]

# Mapeo opcional de cabeceras "humanas" a los nombres internos snake_case.
# La hoja actual usa directamente snake_case, por lo que esta tabla queda
# vacia. Si en el futuro alguien cambia las cabeceras a formato "amigable",
# añadir aqui el mapeo (p. ej. "Nombre de Cliente" → "cliente_nombre").
HEADER_ALIASES: dict[str, str] = {}


def _normalize_headers(record: dict) -> dict:
    """Translate human headers to snake_case internal keys (HEADER_ALIASES).
    Keys not in the map are kept as-is so existing sheets with snake_case keep working."""
    normalized = {}
    for key, value in record.items():
        canonical = HEADER_ALIASES.get(key, key)
        normalized[canonical] = value
    return normalized


# Estados que significan que la reparacion ya esta cerrada para el cliente.
# Se comparan en mayusculas. Si algun dia se introducen estados nuevos de cierre,
# anadirlos aqui.
CLOSED_STATES = {
    "ENTREGADO",
    "RECICLAJE",
    "REPARADO Y ENTREGADO",
    "ANULADO",
}


def normalize_phone(phone: str) -> str:
    """Remove +, spaces, and leading zeros to normalize phone numbers."""
    return re.sub(r"[+\s\-]", "", phone).lstrip("0")


def phones_match(a: str, b: str) -> bool:
    """Compare two phone numbers, tolerating missing country code 34."""
    na, nb = normalize_phone(a), normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Try adding/removing Spanish country code (34)
    if na.startswith("34") and na[2:] == nb:
        return True
    if nb.startswith("34") and nb[2:] == na:
        return True
    return False


def _extract_repair(record: dict) -> dict:
    """Extract only safe columns from a sheet record."""
    repair = {}
    for col in REPAIR_COLUMNS:
        value = record.get(col, "")
        if value is not None and str(value).strip():
            repair[col] = str(value).strip()
    return repair


def _is_active(repair: dict) -> bool:
    """A repair is active unless its delivery/estado indicates closure.
    Si la hoja tiene estado_entrega, manda ese. Si no, caemos a `estado`."""
    estado_entrega = (repair.get("estado_entrega") or "").upper().strip()
    if estado_entrega:
        return estado_entrega not in CLOSED_STATES
    estado = (repair.get("estado") or "").upper().strip()
    return estado not in CLOSED_STATES


class SheetsService:
    """Service for fetching repair data and prices from Google Sheets."""

    def __init__(self):
        self._client: gspread.Client | None = None
        self._cache: list[dict] = []
        self._cache_time: float = 0
        self._prices_cache: list[dict] = []
        self._prices_cache_time: float = 0
        self._equipos_cache: list[dict] = []
        self._equipos_cache_time: float = 0

    async def connect(self):
        """Authenticate with Google Sheets API using a service account."""
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ]
            creds = Credentials.from_service_account_file(
                settings.GOOGLE_CREDENTIALS_PATH, scopes=scopes
            )
            self._client = gspread.authorize(creds)
            logger.info("Connected to Google Sheets API")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}", exc_info=True)
            self._client = None

    def _is_cache_valid(self) -> bool:
        return (
            len(self._cache) > 0
            and (time.time() - self._cache_time) < settings.SHEETS_CACHE_TTL
        )

    async def _fetch_all_records(self) -> list[dict]:
        """Fetch all records from the sheet, using cache if valid."""
        if self._is_cache_valid():
            return self._cache

        if not self._client:
            logger.warning("Sheets client not connected, attempting reconnect")
            await self.connect()
            if not self._client:
                return []

        try:
            sheet = self._client.open_by_key(settings.GOOGLE_SHEETS_ID).worksheet("Reparaciones")
            # expected_headers evita que gspread rechace la hoja si hay columnas
            # duplicadas o celdas de cabecera vacias mas alla de las que usamos
            # (solo exige que REPAIR_COLUMNS exista, no que TODAS sean unicas).
            raw_records = sheet.get_all_records(expected_headers=REPAIR_COLUMNS)
            records = [_normalize_headers(r) for r in raw_records]
            self._cache = records
            self._cache_time = time.time()
            logger.info(f"Fetched {len(records)} records from Google Sheets")
            return records
        except Exception as e:
            logger.error(f"Error fetching sheet data: {e}", exc_info=True)
            if self._cache:
                logger.warning("Returning stale cache due to fetch error")
                return self._cache
            return []

    async def get_repairs_by_phone(self, phone: str) -> list[dict]:
        """Get all repairs for a given phone number."""
        records = await self._fetch_all_records()

        matches = []
        for record in records:
            record_phone = str(record.get("cliente_telefono", ""))
            if record_phone and phones_match(record_phone, phone):
                matches.append(_extract_repair(record))

        logger.info(f"Found {len(matches)} repairs for phone {phone}")
        return matches

    async def get_repair_by_resguardo(self, resguardo: str, phone: str | None = None) -> dict | None:
        """Get a specific repair by resguardo number.

        Si `phone` es None, no se valida la propiedad del resguardo y se
        devuelve la reparacion tal cual. Si `phone` se pasa, se exige que el
        telefono coincida (modo seguro legado).
        """
        records = await self._fetch_all_records()
        resguardo_clean = resguardo.strip()

        for record in records:
            if str(record.get("resguardo", "")).strip() == resguardo_clean:
                if phone is None:
                    return _extract_repair(record)
                record_phone = str(record.get("cliente_telefono", ""))
                if record_phone and phones_match(record_phone, phone):
                    return _extract_repair(record)
                return None

        return None

    async def _fetch_all_prices(self) -> list[dict]:
        """Fetch all records from the Prices sheet, using cache if valid."""
        if (
            len(self._prices_cache) > 0
            and (time.time() - self._prices_cache_time) < settings.SHEETS_CACHE_TTL
        ):
            return self._prices_cache

        if not self._client:
            await self.connect()
            if not self._client:
                return []

        if not settings.GOOGLE_PRICES_SHEET_ID:
            logger.error("GOOGLE_PRICES_SHEET_ID is not configured — prices will not be available")
            return []

        try:
            spreadsheet = self._client.open_by_key(settings.GOOGLE_PRICES_SHEET_ID)

            # Find the prices worksheet — try "Precios" first, then fall back to first sheet
            available_tabs = [ws.title for ws in spreadsheet.worksheets()]
            logger.info(f"Available tabs in prices spreadsheet: {available_tabs}")

            tab_name = None
            for candidate in ("Precios", "precios", "Prices", "prices", "Sheet1", "Hoja1"):
                if candidate in available_tabs:
                    tab_name = candidate
                    break
            if tab_name is None and available_tabs:
                tab_name = available_tabs[0]
                logger.warning(f"Tab 'Precios' not found — using first tab: '{tab_name}'")

            sheet = spreadsheet.worksheet(tab_name)
            all_values = sheet.get_all_values()

            if not all_values:
                logger.warning("Prices sheet is empty")
                return []

            # Auto-detect header row: find first row that looks like headers
            # (contains at least one non-empty cell that isn't purely numeric)
            header_row_idx = 0
            for i, row in enumerate(all_values[:5]):
                non_empty = [c.strip() for c in row if c.strip()]
                if len(non_empty) >= 3:
                    header_row_idx = i
                    break

            headers = all_values[header_row_idx]
            logger.info(f"Price sheet headers (row {header_row_idx + 1}): {headers}")

            records = []
            for row in all_values[header_row_idx + 1:]:
                if any(cell.strip() for cell in row):
                    record = dict(zip(headers, row))
                    records.append(record)

            self._prices_cache = records
            self._prices_cache_time = time.time()
            logger.info(f"Fetched {len(records)} price records from Google Sheets (tab: '{tab_name}')")
            return records
        except Exception as e:
            logger.error(f"Error fetching prices sheet: {e}", exc_info=True)
            if self._prices_cache:
                return self._prices_cache
            return []

    def _get_field(self, record: dict, *candidates: str) -> str:
        """Return first non-empty value from a list of possible column name candidates."""
        for key in candidates:
            val = str(record.get(key, "")).strip()
            if val:
                return val
        return ""

    async def get_all_prices(self) -> list[dict]:
        """Get all price records."""
        records = await self._fetch_all_prices()
        prices = []
        for r in records:
            prices.append({
                "categoria": self._get_field(r, "Categoria", "Categoría", "categoria", "CATEGORIA"),
                "marca": self._get_field(r, "Marca", "marca", "MARCA"),
                "modelo": self._get_field(r, "Modelo", "modelo", "MODELO"),
                "tipo_reparacion": self._get_field(
                    r, "Tipo_Reparacion", "Tipo de Reparacion", "Tipo de Reparación",
                    "Tipo Reparacion", "tipo_reparacion", "TipoReparacion", "Reparacion",
                ),
                "precio": self._get_field(
                    r, "Precio (S/)", "Precio (€)", "Precio", "precio", "Price",
                    "Precio EUR", "Precio (€ + IVA)", "Precio S/", "PRECIO",
                ),
                "disponible": self._get_field(r, "Disponible", "disponible", "DISPONIBLE", "Estado"),
            })
        # Filter out rows with no marca and no tipo_reparacion (likely blank separator rows)
        prices = [p for p in prices if p["marca"] or p["tipo_reparacion"]]
        return prices

    def format_prices_for_prompt(self, prices: list[dict]) -> str:
        """Format price data into context for the AI."""
        if not prices:
            return ""

        lines = ["[TABLA DE PRECIOS DE REPARACIONES]"]
        lines.append(f"Total de servicios disponibles: {len(prices)}\n")

        for p in prices:
            disponible = "DISPONIBLE" if p["disponible"].lower() == "si" else "NO DISPONIBLE"
            lines.append(
                f"- {p['marca']} {p['modelo']} | {p['tipo_reparacion']} | "
                f"{p['precio']} | {disponible}"
            )

        lines.append("\nINSTRUCCIONES — CÓMO USAR ESTA TABLA:")
        lines.append("⚠️ REGLA PRINCIPAL: Esta tabla SOLO se consulta cuando el cliente pregunta EXPLÍCITAMENTE por el precio ('¿cuánto cuesta?', '¿qué precio tiene?', '¿cuánto cobráis?'). Si el cliente solo describe un problema o avería SIN preguntar precio -aunque pregunte 'hay posibilidad de arreglo/reparación', 'se puede reparar', 'podrían verlo'-, ignora esta tabla y sigue el flujo normal de reparación (confirmar, causas, bloque de ventajas).")
        lines.append("  Ejemplo: 'se me ha roto el gatillo de mi Dyson V10, ¿habría posibilidad de arreglo?' → NO es una pregunta de precio → ignora la tabla, sigue el flujo normal.")
        lines.append("  Ejemplo: 'se me ha roto el gatillo de mi Dyson V10, ¿cuánto costaría el arreglo?' → SÍ es pregunta de precio → consulta la tabla (ver CASO A).")
        lines.append("")
        lines.append("Cuando SÍ corresponde dar precio, busca por SIGNIFICADO, no por texto exacto.")
        lines.append("Ejemplos: 'cambio de carcasa' = 'Reemplazo de carcasa'; 'gatillo roto' = 'Cambio de gatillo'; 'no carga' podría ser batería o placa.")
        lines.append("")
        lines.append("CASO A — El cliente pregunta precio y la coincidencia es clara:")
        lines.append("  Da el precio directamente. Formato: '🔧 [Tipo de reparación]: [precio]+IVA'.")
        lines.append("  ❌ No copies la fila entera. ❌ No digas 'prefiero no darte precio sin revisar' si el precio ya está aquí.")
        lines.append("  ⚠️ Dar el precio directo NO te exime del resto del protocolo: si el bloque de ventajas/garantía todavía no se mostró en esta conversación, inclúyelo igualmente (una sola vez) en la misma respuesta que da el precio.")
        lines.append("")
        lines.append("CASO B — El cliente pregunta precio pero no está claro qué servicio exacto necesita:")
        lines.append("  Muestra MÁXIMO 3 opciones numeradas de esa marca/modelo, elige las más probables:")
        lines.append("  '¿Te refieres a alguna de estas opciones?")
        lines.append("  1. [Tipo reparación A] — [precio]+IVA")
        lines.append("  2. [Tipo reparación B] — [precio]+IVA")
        lines.append("  ¿Es alguna de estas, u otra cosa?'")
        lines.append("")
        lines.append("CASO C — El servicio no está en la tabla:")
        lines.append("  Indica que ese servicio requiere revisión en tienda para dar presupuesto.")
        lines.append("")
        lines.append("- Si aparece 'NO DISPONIBLE': informa el precio pero aclara que no está disponible ahora.")
        lines.append("- NUNCA inventes precios. NUNCA muestres campos internos crudos.")

        return "\n".join(lines)

    def format_repairs_for_prompt(self, repairs: list[dict]) -> str:
        """Format repair data into context for the AI, separating active from closed."""
        if not repairs:
            return ""

        active = [r for r in repairs if _is_active(r)]
        closed = [r for r in repairs if not _is_active(r)]

        lines = ["[DATOS DE REPARACIÓN DEL CLIENTE]"]

        # Active repairs - full detail
        if active:
            lines.append(f"\nREPARACIONES ACTIVAS ({len(active)}):")
            lines.append("Estos equipos aún están en proceso o pendientes de recogida/envío.\n")
            for i, r in enumerate(active, 1):
                lines.append(f"--- Equipo {i} de {len(active)} ---")
                lines.extend(_format_single_repair(r))
                lines.append("")
        else:
            lines.append("\nNo tiene reparaciones activas en este momento.")

        # Closed repairs - minimal summary
        if closed:
            lines.append(f"\nREPARACIONES ANTERIORES FINALIZADAS: {len(closed)}")
            lines.append("Detalle disponible solo si el cliente pregunta por un resguardo concreto.\n")
            for r in closed:
                lines.append(
                    f"  Resguardo {r.get('resguardo', '?')} | "
                    f"{r.get('equipo_modelo', '?')} | "
                    f"{r.get('estado', '?')}"
                )
            lines.append("")

        # Instructions for GPT
        lines.append("INSTRUCCIONES:")
        lines.append("- Si el cliente pregunta en general, responde SOLO sobre las reparaciones ACTIVAS.")
        lines.append("- Si tiene varios equipos activos, informa de TODOS, no solo del primero.")
        lines.append("- Si no tiene activas pero si anteriores, informa: 'No tienes reparaciones activas. Tienes X reparaciones anteriores ya finalizadas.'")
        lines.append("- Si pregunta por un resguardo específico del historial, indícale su estado.")
        lines.append("- NUNCA inventes información que no esté en estos datos.")

        return "\n".join(lines)

    async def _fetch_all_equipos(self) -> list[dict]:
        """Fetch all rows from the 'Equipos' tab in the repairs spreadsheet."""
        if (
            len(self._equipos_cache) > 0
            and (time.time() - self._equipos_cache_time) < settings.SHEETS_CACHE_TTL
        ):
            return self._equipos_cache

        if not self._client:
            await self.connect()
            if not self._client:
                return []

        try:
            sheet = self._client.open_by_key(settings.GOOGLE_SHEETS_ID).worksheet("Equipos")
            raw_records = sheet.get_all_records()
            self._equipos_cache = raw_records
            self._equipos_cache_time = time.time()
            logger.info(f"Fetched {len(raw_records)} equipment records from Equipos sheet")
            return raw_records
        except Exception as e:
            logger.error(f"Error fetching Equipos sheet: {e}", exc_info=True)
            if self._equipos_cache:
                return self._equipos_cache
            return []

    async def get_available_equipos(self) -> list[dict]:
        """Return equipment that passes all availability filters:
        activo=SI, estado=disponible, sin defectos, sin observaciones."""
        records = await self._fetch_all_equipos()
        available = []
        for r in records:
            marca = str(r.get("marca", "")).strip()
            modelo = str(r.get("modelo", "")).strip()
            activo = str(r.get("activo", "")).strip().lower()
            estado = str(r.get("estado", "")).strip().lower()
            defectos = str(r.get("defectos", "")).strip()
            observaciones = str(r.get("observaciones", "")).strip()

            if activo not in ("si", "sí", "1", "true", "yes", "s"):
                logger.debug("Equipo excluido (activo=%r): %s %s", activo, marca, modelo)
                continue
            if estado != "disponible":
                logger.debug("Equipo excluido (estado=%r): %s %s", estado, marca, modelo)
                continue
            if defectos:
                logger.debug("Equipo excluido (defectos=%r): %s %s", defectos, marca, modelo)
                continue
            if observaciones:
                logger.debug("Equipo excluido (observaciones=%r): %s %s", observaciones, marca, modelo)
                continue

            available.append({
                "marca": str(r.get("marca", "")).strip(),
                "modelo": str(r.get("modelo", "")).strip(),
                "tipo": str(r.get("tipo", r.get("Tipo", r.get("TIPO", r.get("Categoria", r.get("categoria", "")))))).strip(),
                "sistema_operativo": str(r.get("sistema_operativo", "")).strip(),
                "caracteristicas": str(r.get("caracteristicas", "")).strip(),
            })
        return available

    def format_equipos_for_prompt(self, equipos: list[dict]) -> str:
        """Format available rental equipment as context for the AI."""
        if not equipos:
            return (
                "[EQUIPOS DISPONIBLES PARA ALQUILER]\n"
                "No hay equipos disponibles en este momento.\n"
                "INSTRUCCIONES: Informa al cliente de que actualmente no hay stock disponible "
                "y ofrécele dejar sus datos para ser contactado cuando haya disponibilidad."
            )

        # Group equipment by client-facing category → marca → list of caracteristicas
        # Category is derived from tipo + sistema_operativo + marca to avoid
        # misclassifications (e.g. Apple with sistema_operativo=Windows appearing under Windows)
        def _categoria(tipo: str, so: str, marca: str) -> str:
            t = tipo.lower()
            s = so.lower()
            m = marca.lower()
            if "gamer" in t or "gaming" in t:
                return "Ordenador Gamer"
            if "surface" in t:
                return "Microsoft Surface"
            if "apple" in m or "mac" in s:
                return "Mac"
            if "windows" in s:
                return "Windows"
            return tipo  # fallback: use raw tipo value

        tipo_data: dict[str, dict[str, list[tuple[str, str]]]] = {}
        for e in equipos:
            categoria = _categoria(
                e.get("tipo", "").strip(),
                e.get("sistema_operativo", "").strip(),
                e.get("marca", "").strip(),
            )
            marca = e.get("marca", "").strip()
            modelo = e.get("modelo", "").strip()
            modelo_corto = _shorten_model(modelo)
            caract = e.get("caracteristicas", "").strip()
            if not categoria or not marca:
                continue
            if categoria not in tipo_data:
                tipo_data[categoria] = {}
            if marca not in tipo_data[categoria]:
                tipo_data[categoria][marca] = []
            if caract:
                tipo_data[categoria][marca].append((modelo_corto, caract))

        lines = ["[EQUIPOS DISPONIBLES PARA ALQUILER]"]
        lines.append(f"Total equipos disponibles en stock: {len(equipos)}")
        lines.append("(Lista interna — usar identificador abreviado marca+modelo al inicio de cada opcion)\n")

        for tipo, marcas in tipo_data.items():
            lines.append(f"Tipo: {tipo}")
            for marca, items in marcas.items():
                lines.append(f"  Marca: {marca}")
                for modelo_corto, c in items:
                    if modelo_corto:
                        lines.append(f"    - [{marca} {modelo_corto}] {c}")
                    else:
                        lines.append(f"    - {c}")
            lines.append("")

        lines.append("INSTRUCCIONES OBLIGATORIAS — EQUIPOS ALQUILER:")
        lines.append("✅ FLUJO:")
        lines.append("  1. El cliente indica el tipo (Windows, Mac, Microsoft Surface, Ordenador Gamer).")
        lines.append("     → Filtra por ese tipo y lista TODAS las marcas disponibles para ese tipo, sin omitir ninguna.")
        lines.append("     ❌ PROHIBIDO mostrar solo algunas marcas. Deben aparecer TODAS las que figuran en la lista para ese tipo.")
        lines.append("  2. El cliente indica una marca.")
        lines.append("     → Muestra TODAS las características de los equipos de ESA marca usando este formato WhatsApp OBLIGATORIO:")
        lines.append("        *Equipos [Marca] disponibles:*")
        lines.append("        Opción 1️⃣: *[Marca ModeloCorto]* — [caracteristicas equipo 1 traducidas]")
        lines.append("        Opción 2️⃣: *[Marca ModeloCorto]* — [caracteristicas equipo 2 traducidas]")
        lines.append("        Opción 3️⃣: *[Marca ModeloCorto]* — [caracteristicas equipo 3 traducidas]")
        lines.append("        _(y así sucesivamente para cada equipo de esa marca)_")
        lines.append("        ¿Cuál de estas opciones prefieres y cuánto tiempo necesitarías el equipo?")
        lines.append("     IDENTIFICADOR DE EQUIPO OBLIGATORIO:")
        lines.append("        Cada opción DEBE empezar con *Marca ModeloCorto* en negrita, tal como aparece entre corchetes en la lista interna (ej: [HP 15-bc] → *HP 15-bc*).")
        lines.append("     TRADUCCIÓN OBLIGATORIA de términos técnicos a lenguaje comprensible:")
        lines.append("        - i3-xxx / i5-xxx / i7-xxx / i9-xxx / Ryzen X → 'procesador i3-xxx' / 'procesador i5-xxx' etc.")
        lines.append("        - SSD XXX GB → 'disco SSD de XXX GB'")
        lines.append("        - HDD XXX TB/GB → 'disco duro de XXX TB/GB'")
        lines.append("        - XX GB RAM → 'XX GB de memoria RAM'")
        lines.append("        - RTX/GTX/RX XXXX → 'tarjeta gráfica RTX/GTX XXXX'")
        lines.append("        - pantalla XX\" → 'pantalla de XX pulgadas'")
        lines.append("     ❌ PROHIBIDO omitir ninguna opción. Cada linea de 'caracteristicas' es un equipo distinto.")
        lines.append("     ❌ PROHIBIDO usar guiones, puntos o bullets. Usar siempre 'Opción 1️⃣:' 'Opción 2️⃣:' etc.")
        lines.append("  3. Continúa con el PASO 2 (duración).")
        lines.append("  4. Si no hay equipos del tipo solicitado, informa y menciona qué tipos sí tienen disponibilidad.")
        lines.append("- ✅ USA el identificador abreviado marca+modelo (ej: *HP 15-bc*) al inicio de cada opción. ❌ NUNCA uses el nombre completo del modelo.")
        lines.append("- ❌ NUNCA listes marcas ni características sin que el cliente haya indicado primero el tipo.")
        lines.append("- ❌ NUNCA ofrezcas un tipo diferente al solicitado sin antes informar de que no hay del tipo pedido.")
        lines.append("- La disponibilidad final siempre la confirma un asistente.")
        lines.append("")
        lines.append("REGLA — CONSULTA POR OPCIÓN MÁS ECONÓMICA:")
        lines.append("Si el cliente pregunta por la opción más barata, más económica o de menor precio:")
        lines.append("  1. Compara los precios/tarifas de las alternativas disponibles para el tipo solicitado.")
        lines.append("  2. Si TODAS las opciones tienen el mismo precio (como ocurre con las tarifas de alquiler: día 10€+IVA, semana 50€+IVA, mes 150€+IVA):")
        lines.append("     → Informa al cliente de que NO hay diferencias de precio entre las opciones, ya que todas comparten la misma tarifa.")
        lines.append("     → Menciona el precio común (ej: 'Todos los portátiles tienen la misma tarifa: día 10€+IVA, semana 50€+IVA, mes 150€+IVA').")
        lines.append("     → Sugiere elegir según características (rendimiento, tamaño, marca) en lugar de precio.")
        lines.append("  3. Si HAY diferencias de precio entre opciones:")
        lines.append("     a. Selecciona las 3 opciones de menor coste de marcas DISTINTAS dentro de ese tipo.")
        lines.append("     b. Muestra SOLO esas 3 alternativas con formato: *Marca ModeloCorto* — características traducidas.")
        lines.append("     c. ❌ NO listes todas las opciones ni catálogos completos.")
        lines.append("  4. ⚠️ SIEMPRE verifica los precios en la información disponible antes de responder. Nunca asumas precios sin comprobarlos.")
        lines.append("  5. Prioriza una respuesta breve y orientada a facilitar la decisión del cliente.")

        return "\n".join(lines)


def _shorten_model(model: str) -> str:
    """Shorten a full model identifier to its family prefix.

    Examples: '15-bc450ns' -> '15-bc', 'Pavilion-15CW1234' -> 'Pavilion-15CW'
    Short models (<= 8 chars) are returned as-is.
    """
    if not model or len(model) <= 8:
        return model
    shortened = re.sub(r"\d{3,}\w*$", "", model).rstrip("-_ ")
    if shortened and len(shortened) >= 3:
        return shortened
    return model[:8]


def _format_single_repair(r: dict) -> list[str]:
    """Format a single repair into readable lines."""
    return [
        f"Equipo: {r.get('equipo_modelo', 'N/A')}",
        f"Estado: {r.get('estado', 'N/A')}",
    ]
