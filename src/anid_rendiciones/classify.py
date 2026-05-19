"""Asignación de ítem y sub-ítem para cada gasto extraído.

Estrategia:
1. Heurísticas baratas primero (tipo documento + keywords del detalle).
2. Si la heurística no es decisiva, llamada a Claude Haiku con el catálogo
   de ítems del concurso (prompt cacheable).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml
from anthropic import Anthropic
from rapidfuzz import process

from .schema import Gasto, Item, TipoDocumento

log = logging.getLogger(__name__)

MODEL_CLASSIFY = "claude-haiku-4-5-20251001"


# Heurísticas: keyword → (Item, sub-item sugerido)
# IMPORTANTE: el orden importa — la primera coincidencia gana.
# Por eso Equipamiento va ANTES que "libro/book" (para que "notebook" matchee
# como equipamiento y no como material bibliográfico).
HEURISTICAS = [
    # Personal
    (["honorarios", "boleta de honorarios"], Item.PERSONAL, None),
    (["liquidación de sueldo", "liquidacion sueldo"], Item.PERSONAL, "Personal Administrativo"),
    (["previred", "aportes patronales"], Item.PERSONAL, None),
    # Equipamiento (antes que Material Bibliográfico para evitar match "book" en "notebook")
    (["notebook", "laptop", "macbook", "computador", "computer pc", "tablet", "ipad",
      "impresora", "disco duro", "ssd", "monitor", "kit raspberry"], Item.EQUIPAMIENTO, "Nacionales"),
    (["instrumento de laboratorio", "espectrofotómetro", "microscopio", "balanza analítica"], Item.EQUIPAMIENTO, "Nacionales"),
    # Operación — viajes
    (["pasaje aéreo", "pasaje aereo", "boleto aéreo", "boarding pass", "latam", "sky airline", "jetsmart"], Item.OPERACION, "Pasajes Aéreos"),
    (["viático", "viatico"], Item.OPERACION, "Viáticos"),
    (["uber", "didi", "cabify", "taxi", "peaje", "tag", "bencina", "combustible"], Item.OPERACION, "Movilización y Traslados Terrestres"),
    # Operación — eventos
    (["congreso", "conference", "registration fee", "inscripción"], Item.OPERACION, "Inscripción Seminarios, Congresos, Talleres"),
    (["coffee break", "almuerzo reunión", "atención reunión"], Item.OPERACION, "Atención Reuniones"),
    # Operación — material
    (["fungibles", "reactivos", "papel", "tinta", "tóner", "toner", "oficina"], Item.OPERACION, "Bienes y Materiales"),
    (["publicación", "publication fee", "article processing", "apc", "open access"], Item.OPERACION, "Publicaciones"),
    (["libro", "book", "suscripción", "subscription"], Item.OPERACION, "Material Bibliográfico y Suscripciones"),
    (["software", "licencia", "license"], Item.OPERACION, "Softwares"),
    (["arriendo", "alquiler de sala"], Item.OPERACION, "Arriendos en General"),
    # Infraestructura
    (["mobiliario", "escritorio", "silla", "estantería"], Item.INFRAESTRUCTURA, "Mobiliario"),
    (["habilitación de espacio", "acondicionamiento"], Item.INFRAESTRUCTURA, "Acondicionamiento de Espacios Físicos"),
]


def _detalle_lower(g: Gasto) -> str:
    parts = [g.detalle_gasto or "", g.nombre_beneficiario or ""]
    return " ".join(parts).lower()


def _match_keyword(texto: str, keyword: str) -> bool:
    """Match con word boundaries para keywords cortos; substring para frases."""
    kw = keyword.lower()
    if " " in kw:
        return kw in texto
    # palabra entera (acentos y caracteres latinos ok)
    return bool(re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", texto, flags=re.UNICODE))


def clasificar_heuristica(g: Gasto) -> Optional[tuple[Item, Optional[str]]]:
    """Devuelve (Item, sub-item) si una heurística matchea, None si no."""
    texto = _detalle_lower(g)

    # Match por tipo documento + texto
    if g.tipo_documento == TipoDocumento.BOLETA_HONORARIOS:
        # Sub-ítem específico requiere más info; dejamos sólo Item.
        return (Item.PERSONAL, None)
    if g.tipo_documento == TipoDocumento.LIQUIDACION_SUELDO:
        return (Item.PERSONAL, "Personal Administrativo")
    if g.tipo_documento == TipoDocumento.CERTIFICADO_PREVIRED:
        return (Item.PERSONAL, None)
    if g.tipo_documento == TipoDocumento.RECIBO_VIATICOS:
        return (Item.OPERACION, "Viáticos")
    if g.tipo_documento == TipoDocumento.RECIBO_MOVILIZACION:
        return (Item.OPERACION, "Movilización y Traslados Terrestres")

    for keywords, item, subitem in HEURISTICAS:
        if any(_match_keyword(texto, k) for k in keywords):
            return (item, subitem)

    return None


PROMPT_CLASIFICACION = """Eres un asistente experto en rendiciones FONDECYT (ANID, Chile).

Necesito que clasifiques un gasto en el ítem y sub-ítem correctos según el catálogo del Instructivo General REX 7/2026.

# Catálogo de ítems y sub-ítems

{catalogo}

# Gasto a clasificar

- Tipo de documento: {tipo_documento}
- Proveedor / beneficiario: {nombre_beneficiario}
- Detalle: {detalle_gasto}
- Monto CLP: {monto_total}

Devuelve JSON estricto, sin markdown:

{{
  "item": "Gastos en Personal" | "Equipamiento" | "Infraestructura y Mobiliario" | "Gastos de Operación" | "Gastos de Administración Indirectos",
  "subitem": "uno de los sub-ítems exactos del catálogo, o null si no estás seguro",
  "razonamiento": "una frase breve (máx 20 palabras)"
}}
"""


def _catalogo_str(rules_general: dict) -> str:
    lines = []
    for item, conf in rules_general["items"].items():
        lines.append(f"## {item}")
        subs = conf.get("subitems", [])
        if subs:
            for s in subs:
                lines.append(f"  - {s}")
        else:
            lines.append("  (sin sub-ítems)")
    return "\n".join(lines)


def clasificar_con_claude(
    g: Gasto,
    client: Anthropic,
    rules_general: dict,
    model: str = MODEL_CLASSIFY,
) -> tuple[Optional[Item], Optional[str]]:
    import json

    prompt = PROMPT_CLASIFICACION.format(
        catalogo=_catalogo_str(rules_general),
        tipo_documento=g.tipo_documento.value if g.tipo_documento else "Desconocido",
        nombre_beneficiario=g.nombre_beneficiario or "—",
        detalle_gasto=g.detalle_gasto or "—",
        monto_total=g.monto_total,
    )

    resp = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()  # type: ignore[union-attr]
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Claude classify devolvió JSON inválido: %r", text[:200])
        return (None, None)

    try:
        item = Item(data["item"]) if data.get("item") else None
    except ValueError:
        item = None
    subitem = data.get("subitem")
    return (item, subitem)


def normalizar_subitem(subitem: str, item: Item, rules_general: dict) -> Optional[str]:
    """Fuzzy-match contra los sub-ítems oficiales del catálogo."""
    if not subitem:
        return None
    candidatos = rules_general["items"].get(item.value, {}).get("subitems", [])
    if not candidatos:
        return None
    match = process.extractOne(subitem, candidatos, score_cutoff=70)
    return match[0] if match else None


def clasificar(
    g: Gasto,
    client: Anthropic,
    rules_general: dict,
) -> Gasto:
    """Clasifica un gasto y devuelve una nueva copia con item/sub-item poblados."""
    heur = clasificar_heuristica(g)
    if heur is not None:
        item, subitem = heur
    else:
        item, subitem = clasificar_con_claude(g, client, rules_general)

    if item and subitem:
        subitem = normalizar_subitem(subitem, item, rules_general)

    return g.model_copy(update={"item": item, "subitem": subitem})


def clasificar_solo_heuristica(
    g: Gasto,
    rules_general: dict,
) -> Gasto:
    """Clasifica usando SÓLO heurísticas (sin llamar a Claude).

    Útil cuando la app huésped (e.g. el skill `/rendicion` corriendo dentro
    de Claude Code) ya tiene un modelo y queremos evitar la llamada duplicada
    a la API. Si las heurísticas no deciden, deja item/subitem en None y
    el usuario los completa.
    """
    if g.item is not None:
        # Ya viene clasificado; sólo normalizar subitem si vino texto libre
        if g.subitem:
            normalizado = normalizar_subitem(g.subitem, g.item, rules_general)
            if normalizado:
                return g.model_copy(update={"subitem": normalizado})
        return g

    heur = clasificar_heuristica(g)
    if heur is None:
        return g
    item, subitem = heur
    if item and subitem:
        subitem = normalizar_subitem(subitem, item, rules_general)
    return g.model_copy(update={"item": item, "subitem": subitem})


def cargar_reglas(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
