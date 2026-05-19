"""Motor de validación.

Toma cada `Gasto` + el `Proyecto` y aplica las reglas de los YAML de
`rules/general/` y `rules/concurso/`. Devuelve la lista de gastos con
`hallazgos` poblado.
"""

from __future__ import annotations

import logging
from typing import Any

from .schema import Gasto, Hallazgo, Item, Moneda, Proyecto, Severidad, TipoDocumento

log = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    return s.lower().strip()


def _contiene_alguno(texto: str, palabras: list[str]) -> bool:
    t = _normalize(texto)
    return any(_normalize(p) in t for p in palabras)


def _evaluar_condicion(g: Gasto, cond: dict[str, Any], proyecto: Proyecto, contexto: dict[str, Any]) -> bool:
    """Evalúa el bloque `condicion` de una regla. AND entre todos los keys.

    Retorna True si la regla "se cumple" (i.e. el gasto VIOLA la regla).
    """
    detalle = " ".join(filter(None, [g.detalle_gasto, g.nombre_beneficiario or ""]))

    if "monto_total_mayor_que" in cond:
        if not (g.monto_total > cond["monto_total_mayor_que"]):
            return False

    if "monto_total_mayor_que_utm" in cond:
        utm = contexto.get("utm_clp", 67000)  # default conservador; ANID actualiza esto
        if not (g.monto_total > cond["monto_total_mayor_que_utm"] * utm):
            return False

    if "tipo_documento_en" in cond:
        if g.tipo_documento is None:
            return False
        if g.tipo_documento.value not in cond["tipo_documento_en"]:
            return False

    if "tipo_documento_no_en" in cond:
        if g.tipo_documento is None:
            return True  # falta tipo_documento: trate como violación si todo lo demás aplica
        if g.tipo_documento.value in cond["tipo_documento_no_en"]:
            return False

    if "subitem_en" in cond:
        if g.subitem not in cond["subitem_en"]:
            return False

    if "detalle_contiene_alguno" in cond:
        if not _contiene_alguno(detalle, cond["detalle_contiene_alguno"]):
            return False

    if "nombre_beneficiario_contiene" in cond:
        if not g.nombre_beneficiario:
            return False
        if not _contiene_alguno(g.nombre_beneficiario, cond["nombre_beneficiario_contiene"]):
            return False

    if cond.get("fecha_fuera_de_etapa") is True:
        if g.fecha_documento is None:
            return False  # falta fecha: otra regla lo capturará
        if proyecto.fecha_inicio_etapa <= g.fecha_documento <= proyecto.fecha_fin_etapa:
            return False

    if cond.get("moneda_no_clp") is True:
        if g.moneda_original == Moneda.CLP:
            return False

    if cond.get("tipo_cambio_faltante") is True:
        if g.tipo_cambio is not None:
            return False

    if cond.get("anexo_3_pendiente") is True:
        # En v1 no podemos saberlo automáticamente; siempre flag para boletas honorarios.
        if g.tipo_documento != TipoDocumento.BOLETA_HONORARIOS:
            return False

    if cond.get("anexo_5_pendiente") is True:
        # Stub similar: para equipos > 3 UTM avisamos siempre.
        utm = contexto.get("utm_clp", 67000)
        if g.monto_total <= 3 * utm:
            return False

    if cond.get("rut_faltante") is True:
        if g.rut_beneficiario and len(g.rut_beneficiario) > 3:
            return False

    if cond.get("monto_rendido_cero") is True:
        if g.monto_rendido > 0:
            return False

    if cond.get("porcentaje_menor_100_sin_justificacion") is True:
        if g.porcentaje_rendido >= 100:
            return False
        if g.justificacion and g.justificacion.strip():
            return False

    return True


def _regla_aplica_a_item(regla: dict, g: Gasto) -> bool:
    aplica = regla.get("aplica_a", "*")
    if aplica == "*":
        return True
    if g.item is None:
        return True  # validar de todas formas; el faltante será detectado por otra regla
    return g.item.value in aplica


def validar_gasto(
    g: Gasto,
    proyecto: Proyecto,
    reglas_general: dict,
    reglas_concurso: dict,
    contexto: dict[str, Any] | None = None,
) -> Gasto:
    """Devuelve una copia del gasto con `hallazgos` poblado."""
    contexto = contexto or {}
    hallazgos: list[Hallazgo] = []

    # Reglas del Instructivo General
    for regla in reglas_general.get("reglas", []):
        if not _regla_aplica_a_item(regla, g):
            continue
        if _evaluar_condicion(g, regla.get("condicion", {}), proyecto, contexto):
            hallazgos.append(
                Hallazgo(
                    regla_id=regla["id"],
                    severidad=Severidad(regla["severidad"]),
                    mensaje=regla["descripcion"].strip(),
                )
            )

    # Gastos no permitidos del concurso (lista negra de descripciones)
    no_permitidos = reglas_concurso.get("gastos_no_permitidos", [])
    detalle_g = (g.detalle_gasto or "").lower()
    for prohibido in no_permitidos:
        # match si alguna palabra clave significativa del prohibido aparece en el detalle
        keywords = [w for w in prohibido.lower().split() if len(w) > 5]
        if any(k in detalle_g for k in keywords[:3]):
            hallazgos.append(
                Hallazgo(
                    regla_id=f"concurso.no_permitido.{hash(prohibido) & 0xFFFF}",
                    severidad=Severidad.ADVERTENCIA,
                    mensaje=f"Posible gasto no permitido por el concurso: {prohibido}",
                    sugerencia="Revisar contra bases del concurso y solicitar autorización si corresponde.",
                )
            )
            break  # un match es suficiente

    return g.model_copy(update={"hallazgos": hallazgos})


def detectar_duplicados(gastos: list[Gasto]) -> list[Gasto]:
    """Marca como duplicados cuando coinciden (rut_beneficiario, n_documento, monto_total).

    Mutates by returning new copies con hallazgo "duplicado" agregado.
    """
    seen: dict[tuple, int] = {}
    out: list[Gasto] = []
    for i, g in enumerate(gastos):
        key = (g.rut_beneficiario, g.n_documento, g.monto_total)
        if all(k is not None for k in key) and key in seen:
            primer_idx = seen[key]
            nuevo = g.model_copy(
                update={
                    "hallazgos": g.hallazgos
                    + [
                        Hallazgo(
                            regla_id="rex7.duplicado",
                            severidad=Severidad.ERROR,
                            mensaje=f"Documento duplicado (mismo RUT, N° y monto que la fila {primer_idx + 1}).",
                            sugerencia="Eliminar uno de los dos antes de enviar la rendición.",
                        )
                    ]
                }
            )
            out.append(nuevo)
        else:
            if all(k is not None for k in key):
                seen[key] = i
            out.append(g)
    return out


def validar_lote(
    gastos: list[Gasto],
    proyecto: Proyecto,
    reglas_general: dict,
    reglas_concurso: dict,
    contexto: dict[str, Any] | None = None,
) -> list[Gasto]:
    validados = [validar_gasto(g, proyecto, reglas_general, reglas_concurso, contexto) for g in gastos]
    return detectar_duplicados(validados)


def resumen_validacion(gastos: list[Gasto]) -> dict[str, Any]:
    n_errores = sum(1 for g in gastos for h in g.hallazgos if h.severidad == Severidad.ERROR)
    n_adv = sum(1 for g in gastos for h in g.hallazgos if h.severidad == Severidad.ADVERTENCIA)
    por_item: dict[str, int] = {}
    monto_por_item: dict[str, int] = {}
    for g in gastos:
        clave = g.item.value if g.item else "Sin clasificar"
        por_item[clave] = por_item.get(clave, 0) + 1
        monto_por_item[clave] = monto_por_item.get(clave, 0) + g.monto_rendido
    return {
        "n_gastos": len(gastos),
        "n_errores": n_errores,
        "n_advertencias": n_adv,
        "monto_total_rendido": sum(g.monto_rendido for g in gastos),
        "por_item": por_item,
        "monto_por_item": monto_por_item,
    }
