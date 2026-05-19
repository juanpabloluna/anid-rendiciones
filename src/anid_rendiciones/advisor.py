"""Advisor consultivo — "¿cómo conviene rendir este gasto?".

Genera 2-3 opciones de clasificación válidas para una descripción de gasto,
con: documentos requeridos, anexos, disponible en cada ítem, y recomendación
fundada en (a) ajuste a las bases, (b) presupuesto remanente, (c) simplicidad
documental.

Estrategia híbrida:
  1) Filtrar candidatos con heurísticas (palabras clave + tipo documento típico).
  2) Pedir a Claude que rankee y argumente, dando como contexto:
     - las bases (ítems + sub-ítems del concurso)
     - los disponibles actuales por ítem
     - los topes y restricciones específicos del concurso
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from .schema import Item

log = logging.getLogger(__name__)

MODEL_ADVISOR = "claude-sonnet-4-5-20250929"


@dataclass
class OpcionRendicion:
    item: Item
    subitem: Optional[str]
    tipos_documento_validos: list[str]
    anexos_requeridos: list[str]
    disponible_clp: int
    nota_topes: Optional[str]
    pros: list[str]
    contras: list[str]
    recomendado: bool = False
    razonamiento: str = ""


def _opciones_validas_segun_reglas(
    descripcion: str,
    reglas_general: dict,
) -> list[tuple[Item, Optional[str]]]:
    """Devuelve combinaciones (Item, subitem) plausibles según keywords.

    Es deliberadamente permisivo — el ranking final lo hace Claude.
    """
    texto = descripcion.lower()
    candidatos: list[tuple[Item, Optional[str]]] = []

    # Personal
    if any(k in texto for k in ["asistente", "tesista", "estudiante", "doctoral", "magister", "magíster",
                                  "postdoc", "técnico", "tecnico", "investigador", "honorario", "sueldo",
                                  "remuneración", "remuneracion", "profesional"]):
        for sub in ["Investigadores(as)", "Profesionales", "Personal Técnico", "Personal de Apoyo",
                     "Tesistas", "Estudiantes de Doctorado", "Estudiantes de Magíster", "Postdoctorados"]:
            candidatos.append((Item.PERSONAL, sub))

    # Operación — viajes
    if any(k in texto for k in ["pasaje", "vuelo", "boleto aéreo", "boleto aereo", "viaje aéreo"]):
        candidatos.append((Item.OPERACION, "Pasajes Aéreos"))
    if any(k in texto for k in ["viático", "viatico", "alojamiento", "hotel", "alimentación", "alimentacion"]):
        candidatos.append((Item.OPERACION, "Viáticos"))
    if any(k in texto for k in ["uber", "taxi", "peaje", "bencina", "combustible", "movilización", "movilizacion"]):
        candidatos.append((Item.OPERACION, "Movilización y Traslados Terrestres"))
    if any(k in texto for k in ["congreso", "conference", "seminario", "inscripción", "inscripcion", "registration"]):
        candidatos.append((Item.OPERACION, "Inscripción Seminarios, Congresos, Talleres"))
    if any(k in texto for k in ["coffee", "café reunión", "almuerzo equipo", "atención reunión"]):
        candidatos.append((Item.OPERACION, "Atención Reuniones"))

    # Operación — materiales / servicios
    if any(k in texto for k in ["reactivo", "insumo", "fungible", "papel", "tinta", "tóner",
                                  "material", "consumible"]):
        candidatos.append((Item.OPERACION, "Bienes y Materiales"))
    if any(k in texto for k in ["publicación", "publicacion", "apc", "open access", "article processing"]):
        candidatos.append((Item.OPERACION, "Publicaciones"))
    if any(k in texto for k in ["libro", "book", "suscripción", "subscription"]):
        candidatos.append((Item.OPERACION, "Material Bibliográfico y Suscripciones"))
    if any(k in texto for k in ["software", "licencia", "license"]):
        candidatos.append((Item.OPERACION, "Softwares"))
    if any(k in texto for k in ["consultoría", "consultoria", "asesoría", "asesoria"]):
        candidatos.append((Item.OPERACION, "Consultorías y Asesorías"))
    if any(k in texto for k in ["arriendo", "alquiler de sala"]):
        candidatos.append((Item.OPERACION, "Arriendos en General"))
    if any(k in texto for k in ["traducción", "traduccion", "edición", "edicion", "proofreading"]):
        candidatos.append((Item.OPERACION, "Consultorías y Asesorías"))

    # Equipamiento
    if any(k in texto for k in ["computador", "computer", "notebook", "laptop", "macbook", "tablet", "ipad",
                                  "impresora", "disco duro", "ssd", "monitor", "equipo", "instrumento"]):
        candidatos.append((Item.EQUIPAMIENTO, "Nacionales"))
        candidatos.append((Item.EQUIPAMIENTO, "Importados"))

    # Infraestructura
    if any(k in texto for k in ["mobiliario", "escritorio", "silla", "estantería", "estanteria"]):
        candidatos.append((Item.INFRAESTRUCTURA, "Mobiliario"))
    if any(k in texto for k in ["habilitación", "habilitacion", "acondicionamiento"]):
        candidatos.append((Item.INFRAESTRUCTURA, "Acondicionamiento de Espacios Físicos"))

    # Si nada matcheó, ofrecer los ítems principales con sub-ítem nulo
    if not candidatos:
        candidatos = [
            (Item.OPERACION, None),
            (Item.PERSONAL, None),
            (Item.EQUIPAMIENTO, None),
        ]

    # Deduplicar conservando orden
    seen = set()
    out = []
    for c in candidatos:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _docs_y_anexos(item: Item, subitem: Optional[str], reglas_general: dict) -> tuple[list[str], list[str]]:
    item_cfg = reglas_general["items"].get(item.value, {})
    return (
        item_cfg.get("documentos_validos", []),
        item_cfg.get("requiere_anexos", []),
    )


PROMPT_ADVISOR = """Eres un asistente experto en rendiciones FONDECYT (ANID, Chile).
Un investigador te describe un gasto y necesita saber cómo conviene rendirlo.

# Gasto descrito
{descripcion}

{contexto_adicional}

# Contexto del proyecto
- Etapa: {etapa} ({fecha_ini} → {fecha_fin})
- Concurso: FONDECYT Regular {anio_concurso}

# Disponibles actuales por ítem (CLP)
{disponibles_md}

# Topes y restricciones específicas del concurso Regular
{topes_md}

# Opciones de rendición a considerar (con docs y anexos requeridos por REX 7/2026)

{opciones_md}

# Tarea

Devuelve un JSON con la siguiente estructura, SIN markdown fences:

{{
  "opciones": [
    {{
      "item": "Gastos en Personal" | "Equipamiento" | "Infraestructura y Mobiliario" | "Gastos de Operación",
      "subitem": "sub-ítem exacto del catálogo, o null",
      "pros": ["razón 1", "razón 2"],
      "contras": ["limitación 1", "limitación 2"],
      "recomendado": true | false,
      "razonamiento": "una o dos frases que expliquen por qué"
    }}
  ],
  "advertencias_normativas": ["si hay algo del REX 7/2026 que el investigador debe tener en cuenta"],
  "documentos_requeridos_si_se_aplica_recomendado": ["lista concreta"],
  "anexos_requeridos_si_se_aplica_recomendado": ["lista concreta"]
}}

Reglas para el ranking:
- Marca recomendado=true SOLO para UNA opción.
- Penaliza opciones donde el disponible del ítem es insuficiente.
- Prefiere opciones con documentación más simple (factura > boleta honorarios > recibo simple).
- Considera topes del concurso (Tesistas $2.5M/estudiante anual; honorarios IR/COI no aumentables).
- Si el gasto es claramente improcedente (alcohol, multas, celulares, gift cards), marca todas las opciones como recomendado=false y explícalo en advertencias_normativas.

Devuelve SOLO el JSON.
"""


def _formatear_opciones_md(opciones: list[OpcionRendicion]) -> str:
    lines = []
    for i, o in enumerate(opciones, 1):
        sub = f" / {o.subitem}" if o.subitem else " (sub-ítem por definir)"
        lines.append(f"## Opción {i}: {o.item.value}{sub}")
        lines.append(f"- Disponible en este ítem: ${o.disponible_clp:,} CLP")
        lines.append(f"- Documentos válidos: {', '.join(o.tipos_documento_validos) or '—'}")
        if o.anexos_requeridos:
            lines.append(f"- Anexos requeridos: {', '.join(o.anexos_requeridos)}")
        if o.nota_topes:
            lines.append(f"- Topes: {o.nota_topes}")
        lines.append("")
    return "\n".join(lines)


def _formatear_disponibles_md(disponibles: dict[str, int]) -> str:
    lines = ["| Ítem | Disponible CLP |", "|---|---:|"]
    for item, monto in disponibles.items():
        emoji = "🔴" if monto < 0 else ("🟡" if monto < 500000 else "✅")
        lines.append(f"| {item} | {emoji} ${monto:,} |")
    return "\n".join(lines)


def _formatear_topes(reglas_concurso: dict) -> str:
    lines = []
    topes = reglas_concurso.get("items_topes", {})
    for item, cfg in topes.items():
        notas = []
        if "monto_maximo_anual" in cfg:
            notas.append(f"máx ${cfg['monto_maximo_anual']:,}/año")
        if "monto_maximo_por_estudiante_anual" in cfg:
            notas.append(f"máx ${cfg['monto_maximo_por_estudiante_anual']:,} por estudiante/año")
        if cfg.get("aumentar") is False:
            notas.append("no se puede aumentar")
        if cfg.get("sin_movilidad_a_otros_items"):
            notas.append("sin movilidad presupuestaria")
        if notas:
            lines.append(f"- **{item}**: {'; '.join(notas)}")
    no_perm = reglas_concurso.get("gastos_no_permitidos", [])
    if no_perm:
        lines.append("")
        lines.append("**Gastos NO permitidos (lista negra del concurso):**")
        for g in no_perm:
            lines.append(f"  - {g}")
    return "\n".join(lines) if lines else "(sin topes específicos registrados)"


def aconsejar(
    descripcion: str,
    estado,  # ProyectoEstado — import circular si lo tipamos
    reglas_general: dict,
    reglas_concurso: dict,
    client: Anthropic,
    contexto_adicional: Optional[str] = None,
    monto_estimado: Optional[int] = None,
) -> dict:
    """Devuelve dict con opciones + advertencias.

    Estructura:
      {
        "opciones": [OpcionRendicion-like dicts...],
        "advertencias_normativas": [...],
        "documentos_requeridos_si_se_aplica_recomendado": [...],
        "anexos_requeridos_si_se_aplica_recomendado": [...]
      }
    """
    candidatos = _opciones_validas_segun_reglas(descripcion, reglas_general)
    disponibles = estado.disponibles()

    opciones_obj: list[OpcionRendicion] = []
    for item, subitem in candidatos[:6]:  # máximo 6 candidatos a Claude
        docs, anexos = _docs_y_anexos(item, subitem, reglas_general)
        nota = None
        topes_concurso = reglas_concurso.get("items_topes", {})
        if subitem == "Tesistas":
            nota = "Tope $2.5M por estudiante/año, $7.5M total anual; sin movilidad presupuestaria"
        elif subitem == "Investigadores(as)":
            nota = "Honorarios IR/COI: NO se pueden aumentar (sólo disminuir vía solicitud a FONDECYT)"
        opciones_obj.append(
            OpcionRendicion(
                item=item,
                subitem=subitem,
                tipos_documento_validos=docs,
                anexos_requeridos=anexos,
                disponible_clp=disponibles.get(item.value, 0),
                nota_topes=nota,
                pros=[],
                contras=[],
            )
        )

    contexto = contexto_adicional or ""
    if monto_estimado:
        contexto += f"\nMonto estimado: ${monto_estimado:,} CLP"

    prompt = PROMPT_ADVISOR.format(
        descripcion=descripcion,
        contexto_adicional=contexto,
        etapa=estado.proyecto.etapa,
        fecha_ini=estado.proyecto.fecha_inicio_etapa.isoformat(),
        fecha_fin=estado.proyecto.fecha_fin_etapa.isoformat(),
        anio_concurso=estado.proyecto.anio_concurso,
        disponibles_md=_formatear_disponibles_md(disponibles),
        topes_md=_formatear_topes(reglas_concurso),
        opciones_md=_formatear_opciones_md(opciones_obj),
    )

    resp = client.messages.create(
        model=MODEL_ADVISOR,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()  # type: ignore[union-attr]
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Claude advisor devolvió JSON inválido: %s\n%s", e, text[:500])
        return {
            "opciones": [],
            "advertencias_normativas": [f"ERROR parseando respuesta de Claude: {e}"],
            "raw": text,
        }


def formatear_consejo_md(consejo: dict, estado) -> str:
    """Convierte la respuesta del advisor a markdown legible."""
    lines = ["# Recomendación de rendición", ""]

    advertencias = consejo.get("advertencias_normativas", [])
    if advertencias:
        lines.append("## ⚠️ Advertencias normativas")
        for a in advertencias:
            lines.append(f"- {a}")
        lines.append("")

    opciones = consejo.get("opciones", [])
    if not opciones:
        lines.append("_No hay opciones recomendables para este gasto._")
        return "\n".join(lines)

    recomendada = next((o for o in opciones if o.get("recomendado")), None)
    if recomendada:
        sub = f" / {recomendada.get('subitem')}" if recomendada.get("subitem") else ""
        lines.append(f"## ✅ Opción recomendada: {recomendada['item']}{sub}")
        lines.append(f"_{recomendada.get('razonamiento', '')}_")
        lines.append("")
        docs = consejo.get("documentos_requeridos_si_se_aplica_recomendado", [])
        if docs:
            lines.append("**Documentos a presentar:**")
            for d in docs:
                lines.append(f"- {d}")
            lines.append("")
        anex = consejo.get("anexos_requeridos_si_se_aplica_recomendado", [])
        if anex:
            lines.append("**Anexos a adjuntar:**")
            for a in anex:
                lines.append(f"- {a}")
            lines.append("")

    lines.append("## Otras opciones consideradas")
    for o in opciones:
        if o.get("recomendado"):
            continue
        sub = f" / {o.get('subitem')}" if o.get("subitem") else ""
        lines.append(f"### {o['item']}{sub}")
        if o.get("razonamiento"):
            lines.append(f"_{o['razonamiento']}_")
        pros = o.get("pros") or []
        contras = o.get("contras") or []
        if pros:
            lines.append("**A favor:** " + "; ".join(pros))
        if contras:
            lines.append("**Limitaciones:** " + "; ".join(contras))
        lines.append("")

    return "\n".join(lines)
