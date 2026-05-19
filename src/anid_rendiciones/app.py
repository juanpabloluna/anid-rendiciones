"""Streamlit app — entrypoint de UI."""

from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path

import streamlit as st
import yaml
from anthropic import Anthropic

from .classify import cargar_reglas, clasificar
from .extract import extraer_comprobante
from .generate.anexo1 import generar_anexo1
from .schema import Gasto, Item, Moneda, Proyecto, Severidad, TipoDocumento
from .validate import resumen_validacion, validar_lote

PAQUETE_DIR = Path(__file__).parent
REPO_ROOT = PAQUETE_DIR.parent.parent if (PAQUETE_DIR.parent.parent / "rules").exists() else PAQUETE_DIR
RULES_GENERAL = REPO_ROOT / "rules" / "general" / "rex_7_2026.yaml"
RULES_CONCURSO = REPO_ROOT / "rules" / "concurso" / "fondecyt_regular_2022plus.yaml"
PLANTILLA_ANEXO1 = REPO_ROOT / "templates" / "anid_2026" / "Anexo_N1_Formulario_de_Rendicion.xls"


def init_state() -> None:
    st.session_state.setdefault("proyecto", None)
    st.session_state.setdefault("gastos", [])
    st.session_state.setdefault("anthropic_key", os.environ.get("ANTHROPIC_API_KEY", ""))


def sidebar_api_key() -> Anthropic | None:
    st.sidebar.header("Configuración")
    key = st.sidebar.text_input(
        "Anthropic API key",
        value=st.session_state.get("anthropic_key", ""),
        type="password",
        help="Tu key viaja sólo entre este computador y la API de Anthropic. No se guarda en disco.",
    )
    st.session_state["anthropic_key"] = key
    if not key:
        st.sidebar.warning("Ingresa una API key para procesar comprobantes.")
        return None
    return Anthropic(api_key=key)


def paso_1_configurar_proyecto() -> None:
    st.header("1. Configurar proyecto")
    with st.form("proyecto"):
        col1, col2 = st.columns(2)
        with col1:
            codigo = st.text_input("Código del proyecto", value="1240000", help="Ej: 1240123")
            anio_concurso = st.number_input("Año del concurso", 2019, 2030, 2024)
            etapa = st.number_input("Etapa actual", 1, 6, 1)
            fecha_ini = st.date_input("Inicio de etapa", value=date(2025, 4, 1))
            fecha_fin = st.date_input("Fin de etapa", value=date(2026, 3, 31))
            n_rendicion = st.number_input("N° de rendición", 1, 20, 1)
            n_cuota = st.number_input("N° cuota transferida", 1, 20, 1)
        with col2:
            ip = st.text_input("Institución patrocinante (IP)", value="Universidad de Chile")
            rut_ip = st.text_input("RUT IP", value="60.910.000-1")
            facultad = st.text_input("Facultad (si universidad)", value="")
            ir = st.text_input("Investigador Responsable (IR)", value="")
            rut_ir = st.text_input("RUT IR", value="")
            monto_transferido = st.number_input("Monto transferido en esta cuota (CLP)", 0, step=100000, value=0)

        st.subheader("Presupuesto aprobado por ítem (CLP)")
        pcol = st.columns(5)
        with pcol[0]:
            p_personal = st.number_input("Personal", 0, step=100000, value=0)
        with pcol[1]:
            p_equip = st.number_input("Equipamiento", 0, step=100000, value=0)
        with pcol[2]:
            p_infra = st.number_input("Infraestructura", 0, step=100000, value=0)
        with pcol[3]:
            p_oper = st.number_input("Operación", 0, step=100000, value=0)
        with pcol[4]:
            p_indir = st.number_input("Indirectos", 0, step=100000, value=0)

        guardar = st.form_submit_button("Guardar proyecto")
        if guardar:
            st.session_state["proyecto"] = Proyecto(
                codigo=codigo,
                anio_concurso=int(anio_concurso),
                etapa=int(etapa),
                fecha_inicio_etapa=fecha_ini,
                fecha_fin_etapa=fecha_fin,
                institucion_patrocinante=ip,
                rut_ip=rut_ip,
                facultad=facultad or None,
                investigador_responsable=ir,
                rut_ir=rut_ir,
                presupuesto_personal=int(p_personal),
                presupuesto_equipamiento=int(p_equip),
                presupuesto_infraestructura=int(p_infra),
                presupuesto_operacion=int(p_oper),
                presupuesto_indirectos=int(p_indir),
                n_rendicion=int(n_rendicion),
                n_cuota_transferida=int(n_cuota),
                monto_transferido=int(monto_transferido),
            )
            st.success("Proyecto guardado.")


def paso_2_cargar_comprobantes(client: Anthropic) -> None:
    st.header("2. Cargar y procesar comprobantes")
    if st.session_state.get("proyecto") is None:
        st.info("Primero configura el proyecto.")
        return

    archivos = st.file_uploader(
        "Sube boletas, facturas, recibos, invoices (PDF / JPG / PNG)",
        type=["pdf", "jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )
    if not archivos:
        return

    if st.button(f"Procesar {len(archivos)} comprobantes con Claude"):
        progress = st.progress(0.0)
        nuevos: list[Gasto] = []
        reglas_general = cargar_reglas(RULES_GENERAL)
        for i, up in enumerate(archivos):
            try:
                tmp = Path(f"/tmp/{up.name}")
                with open(tmp, "wb") as f:
                    f.write(up.getbuffer())
                g = extraer_comprobante(tmp, client)
                g = clasificar(g, client, reglas_general)
                g = g.model_copy(update={"archivo_origen": up.name})
                nuevos.append(g)
            except Exception as e:
                st.error(f"Error procesando {up.name}: {e}")
            progress.progress((i + 1) / len(archivos))
        st.session_state["gastos"].extend(nuevos)
        st.success(f"Procesados {len(nuevos)} comprobantes.")


def _gasto_a_dict_editable(g: Gasto, idx: int) -> dict:
    return {
        "n_correlativo": idx + 1,
        "archivo_origen": g.archivo_origen or "",
        "item": g.item.value if g.item else "",
        "subitem": g.subitem or "",
        "rut_beneficiario": g.rut_beneficiario or "",
        "nombre_beneficiario": g.nombre_beneficiario or "",
        "detalle_gasto": g.detalle_gasto,
        "tipo_documento": g.tipo_documento.value if g.tipo_documento else "",
        "n_documento": g.n_documento or "",
        "fecha_documento": g.fecha_documento.isoformat() if g.fecha_documento else "",
        "monto_total": g.monto_total,
        "monto_rendido": g.monto_rendido,
        "porcentaje_rendido": g.porcentaje_rendido,
        "justificacion": g.justificacion or "",
        "confianza": g.confianza_extraccion,
        "hallazgos": "; ".join(f"[{h.severidad.value}] {h.regla_id}" for h in g.hallazgos),
    }


def paso_3_revisar_editar() -> None:
    st.header("3. Revisar y editar")
    gastos = st.session_state.get("gastos", [])
    if not gastos:
        st.info("Aún no hay gastos cargados.")
        return

    filas = [_gasto_a_dict_editable(g, i) for i, g in enumerate(gastos)]
    editado = st.data_editor(
        filas,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "monto_total": st.column_config.NumberColumn("Monto total CLP", format="$%d"),
            "monto_rendido": st.column_config.NumberColumn("Monto rendido CLP", format="$%d"),
            "porcentaje_rendido": st.column_config.NumberColumn("% rendido", min_value=0.0, max_value=100.0),
            "confianza": st.column_config.ProgressColumn("Confianza", min_value=0.0, max_value=1.0),
        },
        key="editor_gastos",
    )

    if st.button("Guardar cambios"):
        from dateutil.parser import isoparse

        nuevos: list[Gasto] = []
        for fila in editado:
            try:
                item = Item(fila["item"]) if fila.get("item") else None
            except ValueError:
                item = None
            try:
                tipo_doc = TipoDocumento(fila["tipo_documento"]) if fila.get("tipo_documento") else None
            except ValueError:
                tipo_doc = TipoDocumento.OTRO
            fecha = None
            if fila.get("fecha_documento"):
                try:
                    fecha = isoparse(fila["fecha_documento"]).date()
                except Exception:
                    fecha = None
            nuevos.append(
                Gasto(
                    item=item,
                    subitem=fila.get("subitem") or None,
                    rut_beneficiario=fila.get("rut_beneficiario") or None,
                    nombre_beneficiario=fila.get("nombre_beneficiario") or None,
                    detalle_gasto=fila.get("detalle_gasto") or "",
                    tipo_documento=tipo_doc,
                    n_documento=fila.get("n_documento") or None,
                    fecha_documento=fecha,
                    monto_total=int(fila.get("monto_total") or 0),
                    monto_rendido=int(fila.get("monto_rendido") or 0),
                    porcentaje_rendido=float(fila.get("porcentaje_rendido") or 100.0),
                    justificacion=fila.get("justificacion") or None,
                    archivo_origen=fila.get("archivo_origen"),
                    confianza_extraccion=float(fila.get("confianza") or 0.0),
                    revisado_humano=True,
                )
            )
        st.session_state["gastos"] = nuevos
        st.success("Cambios guardados.")


def paso_4_validar() -> None:
    st.header("4. Validar")
    proyecto = st.session_state.get("proyecto")
    gastos = st.session_state.get("gastos", [])
    if not proyecto or not gastos:
        st.info("Necesitas proyecto y al menos un gasto cargado.")
        return

    if st.button("Correr validación"):
        reglas_g = cargar_reglas(RULES_GENERAL)
        reglas_c = cargar_reglas(RULES_CONCURSO)
        validados = validar_lote(gastos, proyecto, reglas_g, reglas_c)
        st.session_state["gastos"] = validados

        resumen = resumen_validacion(validados)
        cols = st.columns(4)
        cols[0].metric("Gastos", resumen["n_gastos"])
        cols[1].metric("Errores", resumen["n_errores"])
        cols[2].metric("Advertencias", resumen["n_advertencias"])
        cols[3].metric("Total rendido (CLP)", f"${resumen['monto_total_rendido']:,}")

        st.subheader("Por ítem")
        st.dataframe(
            [
                {"ítem": it, "comprobantes": n, "monto rendido CLP": resumen["monto_por_item"].get(it, 0)}
                for it, n in resumen["por_item"].items()
            ]
        )

        st.subheader("Hallazgos")
        any_finding = False
        for i, g in enumerate(validados):
            if not g.hallazgos:
                continue
            any_finding = True
            with st.expander(f"Fila {i + 1}: {g.detalle_gasto[:60]} — ${g.monto_rendido:,}"):
                for h in g.hallazgos:
                    icon = "🔴" if h.severidad == Severidad.ERROR else "🟡"
                    st.write(f"{icon} **{h.regla_id}**: {h.mensaje}")
                    if h.sugerencia:
                        st.caption(f"💡 {h.sugerencia}")
        if not any_finding:
            st.success("Sin hallazgos. Todo el lote pasa la validación automática.")


def paso_5_generar() -> None:
    st.header("5. Generar Anexo N°1")
    proyecto = st.session_state.get("proyecto")
    gastos = st.session_state.get("gastos", [])
    if not proyecto or not gastos:
        st.info("Necesitas proyecto y gastos para generar el Anexo 1.")
        return

    if not PLANTILLA_ANEXO1.exists():
        st.error(f"No encuentro la plantilla en {PLANTILLA_ANEXO1}.")
        return

    if st.button("Generar Anexo N°1 (.xlsx)"):
        try:
            blob = generar_anexo1(proyecto, gastos, PLANTILLA_ANEXO1)
            nombre = f"Anexo1_{proyecto.codigo}_etapa{proyecto.etapa}_rend{proyecto.n_rendicion}.xlsx"
            st.download_button(
                "⬇ Descargar Anexo N°1",
                data=blob,
                file_name=nombre,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            st.success("Anexo N°1 generado. Revisa los totales contra tu SDGL antes de enviar.")
        except Exception as e:
            st.error(f"Error generando Anexo 1: {e}")


def main() -> None:
    st.set_page_config(
        page_title="ANID Rendiciones — FONDECYT Regular",
        page_icon="📄",
        layout="wide",
    )
    init_state()

    st.title("ANID Rendiciones — FONDECYT Regular")
    st.caption(
        "Asistente local para preparar el Anexo N°1 y la rendición de cuentas según REX 7/2026. "
        "Toda la información permanece en tu computador."
    )

    client = sidebar_api_key()

    tabs = st.tabs(
        [
            "1️⃣ Proyecto",
            "2️⃣ Cargar",
            "3️⃣ Revisar",
            "4️⃣ Validar",
            "5️⃣ Generar",
        ]
    )
    with tabs[0]:
        paso_1_configurar_proyecto()
    with tabs[1]:
        if client is None:
            st.warning("Configura la API key en la barra lateral.")
        else:
            paso_2_cargar_comprobantes(client)
    with tabs[2]:
        paso_3_revisar_editar()
    with tabs[3]:
        paso_4_validar()
    with tabs[4]:
        paso_5_generar()


if __name__ == "__main__":
    main()
