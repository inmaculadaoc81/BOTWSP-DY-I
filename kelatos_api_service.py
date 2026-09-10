import logging

import httpx

from config import settings
from sheets_service import REPAIR_COLUMNS, _extract_repair, phones_match, normalize_phone

logger = logging.getLogger(__name__)


def _inferir_tipo_equipo(marca: str, modelo: str, caracteristicas: str) -> str:
    """El Sheet 'Equipos' original tenia una columna tipo con valores como
    'Gamer'/'Surface' que _categoria() (sheets_service.format_equipos_for_prompt)
    usa para clasificar. La tabla kelatos_app.equipos no trajo ese dato con el
    mismo significado (solo 'Portatil'/'Normal'), asi que se reconstruye aqui
    a partir de marca/modelo/caracteristicas — _categoria() sigue intacta, solo
    cambia lo que le llega en 'tipo'. Nunca debe devolver "" (una tipo vacia
    hace que _categoria() devuelva "" y el equipo se descarte silenciosamente
    en format_equipos_for_prompt)."""
    texto = f"{marca} {modelo} {caracteristicas}".lower()
    if "surface" in marca.lower():
        return "Microsoft Surface"
    if any(k in texto for k in ("gaming", "gamer", "rtx", "gtx", "radeon rx")):
        return "Gamer"
    return "Portátil"


def _candidate_phones(phone: str) -> list[str]:
    """Reproduce la tolerancia de phones_match() (Sheets) contra un filtro de
    igualdad exacta (la API del dashboard no soporta LIKE) — genera varias
    variantes plausibles del mismo numero (con/sin '+', con/sin espacio tras
    el prefijo de pais, con/sin el propio prefijo 34) y las prueba en orden
    hasta encontrar coincidencia. cliente_telefono en Postgres no tiene un
    formato unico — se ha visto guardado como "+34 XXXXXXXXX" (con espacio,
    el mas comun en datos reales) y como "34XXXXXXXXX" (sin espacio) segun
    el origen del alta. No es tan tolerante como phones_match() (que compara
    TODOS los registros ya normalizados), pero cubre los formatos reales mas
    comunes sin tener que traer toda la tabla al bot."""
    base = normalize_phone(phone)
    if not base:
        return []
    sin_prefijo = base[2:] if base.startswith("34") and len(base) > 9 else base
    con_prefijo = base if base.startswith("34") else f"34{base}"
    candidatos = [
        con_prefijo,
        f"+{con_prefijo}",
        f"+34 {sin_prefijo}",
        f"34 {sin_prefijo}",
        sin_prefijo,
    ]
    vistos: set[str] = set()
    resultado = []
    for c in candidatos:
        if c and c not in vistos:
            vistos.add(c)
            resultado.append(c)
    return resultado


class KelatosApiService:
    """Sustituye a SheetsService para la busqueda de reparaciones por
    resguardo/telefono — el Sheet "Reparaciones" que usaba este bot ya no es
    la fuente real: los datos viven ahora en Postgres, servidos por la API
    del dashboard (kelatos-rep-back) en KELATOS_API_BASE_URL. El resto del
    bot (precios, equipos, Chatwoot, Calendar, EspoCRM) sigue igual, sin
    tocar — solo esta pieza cambia de origen de datos.

    Mismo contrato publico que SheetsService.get_repair_by_resguardo() /
    get_repairs_by_phone() (misma forma de dict, misma lista REPAIR_COLUMNS)
    para no tener que tocar main.py mas alla de instanciar esta clase en
    vez de SheetsService — format_repairs_for_prompt() sigue viviendo en
    sheets_service.py (es un formateador puro, no toca Sheets ni la API).
    """

    def __init__(self):
        self.base_url = settings.KELATOS_API_BASE_URL.rstrip("/")
        self.token = settings.KELATOS_API_TOKEN

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.base_url or not self.token:
            logger.error("KELATOS_API_BASE_URL/KELATOS_API_TOKEN no configurados")
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    params=params,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Kelatos API HTTP error on {path}: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Error calling Kelatos API {path}: {e}", exc_info=True)
            return None

    async def get_repair_by_resguardo(self, resguardo: str, phone: str | None = None) -> dict | None:
        """Reproduce SheetsService.get_repair_by_resguardo() contra
        GET /v1/reparaciones/:resguardo (fila cruda completa) — se filtra
        aqui mismo a REPAIR_COLUMNS antes de devolver, igual que
        _extract_repair() hacia con las filas del Sheet."""
        resguardo_clean = resguardo.strip()
        data = await self._get(f"/v1/reparaciones/{resguardo_clean}")
        if not data or not data.get("ok") or not data.get("row"):
            return None
        row = data["row"]
        if phone is not None:
            record_phone = str(row.get("cliente_telefono", ""))
            if not record_phone or not phones_match(record_phone, phone):
                return None
        return _extract_repair(row)

    async def get_repairs_by_phone(self, phone: str) -> list[dict]:
        """Reproduce SheetsService.get_repairs_by_phone() — la API del
        dashboard solo filtra por igualdad exacta (GET /v1/reparaciones?
        cliente_telefono=...), asi que se prueban unas pocas variantes
        normalizadas del numero (ver _candidate_phones) en vez de traer
        toda la tabla para comparar en Python."""
        vistos: set[str] = set()
        resultado: list[dict] = []
        for candidato in _candidate_phones(phone):
            data = await self._get("/v1/reparaciones", {"cliente_telefono": candidato, "limit": 50})
            if not data or not data.get("ok"):
                continue
            for row in data.get("rows", []):
                resguardo = str(row.get("resguardo", ""))
                if resguardo and resguardo not in vistos:
                    vistos.add(resguardo)
                    resultado.append(_extract_repair(row))
        logger.info(f"Found {len(resultado)} repairs by phone for {phone}")
        return resultado

    async def get_available_equipos(self) -> list[dict]:
        """Reproduce SheetsService.get_available_equipos() — antes leia la
        pestana "Equipos" del Sheet de reparaciones (ya no existe / no es la
        fuente real); ahora usa GET /v1/equipos, el mismo endpoint canonico
        que ya usa el dashboard (kelatos-rep-back server.js) para el modulo
        de Alquileres. Mismo filtro de disponibilidad que el original: activo,
        estado=DISPONIBLE, sin defectos, sin observaciones. Devuelve la misma
        forma de dict (marca/modelo/tipo/sistema_operativo/caracteristicas)
        que espera format_equipos_for_prompt() (sheets_service.py, sin tocar)."""
        data = await self._get("/v1/equipos")
        if not data or not data.get("ok"):
            return []
        disponibles: list[dict] = []
        for e in data.get("equipos", []):
            estado = str(e.get("estado", "")).strip().upper()
            defectos = str(e.get("defectos", "")).strip()
            observaciones = str(e.get("observaciones", "")).strip()
            if estado != "DISPONIBLE" or defectos or observaciones:
                continue
            marca = str(e.get("marca", "")).strip()
            modelo = str(e.get("modelo", "")).strip()
            sistema_operativo = str(e.get("sistemaOperativo", "")).strip()
            caracteristicas = str(e.get("caracteristicas", "")).strip()
            disponibles.append({
                "marca": marca,
                "modelo": modelo,
                "tipo": _inferir_tipo_equipo(marca, modelo, caracteristicas),
                "sistema_operativo": sistema_operativo,
                "caracteristicas": caracteristicas,
            })
        logger.info(f"Found {len(disponibles)} available rental equipos")
        return disponibles
