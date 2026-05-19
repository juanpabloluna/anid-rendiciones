"""CLI compacta para el skill `/rendicion` de Claude Code.

Subcomandos:
  init          Crear proyecto nuevo (interactivo)
  list          Listar proyectos en el directorio raíz
  budget        Mostrar disponibles por ítem (markdown)
  add           Procesar una boleta (extrae con Claude, clasifica, valida, actualiza)
  add-dir       Procesar todas las boletas de una carpeta
  show          Mostrar gastos rendidos
  remove        Eliminar un gasto por n_correlativo
  validate      Correr validación completa sobre todos los gastos
  advise        Consulta — "¿cómo conviene rendir este gasto?"
  export        Generar Anexo N°1 XLSX
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from . import advisor as adv
from . import state as st
from .classify import cargar_reglas, clasificar
from .extract import extraer_comprobante
from .generate.anexo1 import generar_anexo1
from .schema import Item, Proyecto, Severidad
from .validate import resumen_validacion, validar_lote

PAQUETE_DIR = Path(__file__).parent
REPO_ROOT = PAQUETE_DIR.parent.parent if (PAQUETE_DIR.parent.parent / "rules").exists() else PAQUETE_DIR
RULES_GENERAL = REPO_ROOT / "rules" / "general" / "rex_7_2026.yaml"
RULES_CONCURSO = REPO_ROOT / "rules" / "concurso" / "fondecyt_regular_2022plus.yaml"
PLANTILLA_ANEXO1 = REPO_ROOT / "templates" / "anid_2026" / "Anexo_N1_Formulario_de_Rendicion.xls"


def _get_client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.stderr.write("ERROR: necesito ANTHROPIC_API_KEY en el entorno.\n")
        sys.exit(2)
    return Anthropic(api_key=key)


def _root_dir(args) -> Optional[Path]:
    return Path(args.root).expanduser() if args.root else None


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------

def cmd_init(args) -> None:
    """Crea un proyecto nuevo. Toma todos los parámetros desde args."""
    proyecto = Proyecto(
        codigo=args.codigo,
        anio_concurso=args.anio,
        etapa=args.etapa,
        fecha_inicio_etapa=date.fromisoformat(args.inicio),
        fecha_fin_etapa=date.fromisoformat(args.fin),
        institucion_patrocinante=args.ip or "—",
        rut_ip=args.rut_ip,
        facultad=args.facultad,
        investigador_responsable=args.ir or "—",
        rut_ir=args.rut_ir or "—",
        presupuesto_personal=args.p_personal or 0,
        presupuesto_equipamiento=args.p_equipamiento or 0,
        presupuesto_infraestructura=args.p_infraestructura or 0,
        presupuesto_operacion=args.p_operacion or 0,
        presupuesto_indirectos=args.p_indirectos or 0,
        n_rendicion=args.n_rendicion or 1,
        n_cuota_transferida=args.n_cuota or 1,
        monto_transferido=args.monto_transferido or 0,
    )
    estado = st.crear(proyecto, _root_dir(args), overwrite=args.force)
    print(f"✅ Proyecto {proyecto.codigo} creado en {st.directorio_proyecto(proyecto.codigo, _root_dir(args))}")
    print()
    print(st.resumen_disponibles(estado))


def cmd_list(args) -> None:
    proyectos = st.listar_proyectos(_root_dir(args))
    if not proyectos:
        print("No hay proyectos registrados.")
        return
    print("Proyectos:")
    for p in proyectos:
        try:
            e = st.cargar(p, _root_dir(args))
            print(f"  - {p} (etapa {e.proyecto.etapa}, {len(e.gastos)} gastos)")
        except Exception as exc:
            print(f"  - {p} (error: {exc})")


def cmd_budget(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    print(st.resumen_disponibles(estado))


def cmd_add(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    client = _get_client()
    archivo = Path(args.archivo).expanduser().resolve()
    if not archivo.exists():
        sys.stderr.write(f"No existe archivo: {archivo}\n")
        sys.exit(1)
    print(f"📄 Procesando {archivo.name} ...")
    g = extraer_comprobante(archivo, client)
    reglas_g = cargar_reglas(RULES_GENERAL)
    reglas_c = cargar_reglas(RULES_CONCURSO)
    g = clasificar(g, client, reglas_g)
    g = g.model_copy(update={"archivo_origen": str(archivo)})
    # Validar individualmente
    val = validar_lote([g], estado.proyecto, reglas_g, reglas_c)[0]
    # Guardar
    estado = st.agregar_gasto(estado, val, _root_dir(args))
    # Reportar
    print()
    print(f"✅ Gasto #{val.n_correlativo} agregado:")
    print(f"  Ítem:        {val.item.value if val.item else '—'} / {val.subitem or '—'}")
    print(f"  Proveedor:   {val.nombre_beneficiario or '—'} ({val.rut_beneficiario or '—'})")
    print(f"  Documento:   {val.tipo_documento.value if val.tipo_documento else '—'} N° {val.n_documento or '—'}")
    print(f"  Fecha:       {val.fecha_documento.isoformat() if val.fecha_documento else '—'}")
    print(f"  Monto:       ${val.monto_total:,} CLP")
    print(f"  Confianza:   {val.confianza_extraccion:.0%}")
    if val.hallazgos:
        print()
        print(f"⚠️  Hallazgos:")
        for h in val.hallazgos:
            icon = "🔴" if h.severidad == Severidad.ERROR else "🟡"
            print(f"  {icon} [{h.regla_id}] {h.mensaje}")
    print()
    print(st.resumen_disponibles(estado))


def cmd_add_dir(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    client = _get_client()
    carpeta = Path(args.carpeta).expanduser().resolve()
    archivos = sorted(
        f for f in carpeta.iterdir()
        if f.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    )
    if not archivos:
        print(f"No hay PDFs/JPGs en {carpeta}.")
        return
    print(f"Procesando {len(archivos)} archivos…")
    reglas_g = cargar_reglas(RULES_GENERAL)
    reglas_c = cargar_reglas(RULES_CONCURSO)
    for i, archivo in enumerate(archivos, 1):
        try:
            print(f"  [{i}/{len(archivos)}] {archivo.name}")
            g = extraer_comprobante(archivo, client)
            g = clasificar(g, client, reglas_g)
            g = g.model_copy(update={"archivo_origen": str(archivo)})
            val = validar_lote([g], estado.proyecto, reglas_g, reglas_c)[0]
            estado = st.agregar_gasto(estado, val, _root_dir(args))
        except Exception as exc:
            sys.stderr.write(f"    error: {exc}\n")
    print()
    print(st.resumen_disponibles(estado))


def cmd_show(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    if not estado.gastos:
        print("Sin gastos registrados.")
        return
    print(f"# Gastos de proyecto {estado.proyecto.codigo}")
    print()
    print("| # | Ítem | Sub-ítem | Proveedor | Fecha | Monto | Hallazgos |")
    print("|---|---|---|---|---|---:|---|")
    for g in estado.gastos:
        flags = "; ".join(f"[{h.severidad.value[0]}]" for h in g.hallazgos) or "—"
        print(
            f"| {g.n_correlativo} | {g.item.value if g.item else '?'} | {g.subitem or '—'} "
            f"| {(g.nombre_beneficiario or '—')[:30]} | "
            f"{g.fecha_documento.isoformat() if g.fecha_documento else '—'} "
            f"| ${g.monto_rendido:,} | {flags} |"
        )


def cmd_remove(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    estado = st.eliminar_gasto(estado, args.n, _root_dir(args))
    print(f"✅ Gasto #{args.n} eliminado. Quedan {len(estado.gastos)} gastos.")
    print()
    print(st.resumen_disponibles(estado))


def cmd_validate(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    reglas_g = cargar_reglas(RULES_GENERAL)
    reglas_c = cargar_reglas(RULES_CONCURSO)
    validados = validar_lote(estado.gastos, estado.proyecto, reglas_g, reglas_c)
    estado.gastos = validados
    st.guardar(estado, _root_dir(args))
    res = resumen_validacion(validados)
    print(f"Validación completa: {res['n_gastos']} gastos, "
          f"{res['n_errores']} errores, {res['n_advertencias']} advertencias.")
    for g in validados:
        if g.hallazgos:
            print(f"\n#{g.n_correlativo} — {g.detalle_gasto[:60]}")
            for h in g.hallazgos:
                icon = "🔴" if h.severidad == Severidad.ERROR else "🟡"
                print(f"  {icon} {h.regla_id}: {h.mensaje[:100]}")


def cmd_advise(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    client = _get_client()
    reglas_g = cargar_reglas(RULES_GENERAL)
    reglas_c = cargar_reglas(RULES_CONCURSO)
    consejo = adv.aconsejar(
        descripcion=args.descripcion,
        estado=estado,
        reglas_general=reglas_g,
        reglas_concurso=reglas_c,
        client=client,
        monto_estimado=args.monto,
        contexto_adicional=args.contexto,
    )
    if args.json:
        print(json.dumps(consejo, ensure_ascii=False, indent=2))
        return
    print(adv.formatear_consejo_md(consejo, estado))


def cmd_export(args) -> None:
    estado = st.cargar(args.codigo, _root_dir(args))
    if not PLANTILLA_ANEXO1.exists():
        sys.stderr.write(f"No encuentro plantilla en {PLANTILLA_ANEXO1}\n")
        sys.exit(1)
    blob = generar_anexo1(estado.proyecto, estado.gastos, PLANTILLA_ANEXO1)
    out_dir = st.directorio_proyecto(estado.proyecto.codigo, _root_dir(args))
    out_path = out_dir / f"Anexo1_{estado.proyecto.codigo}_etapa{estado.proyecto.etapa}_rend{estado.proyecto.n_rendicion}.xlsx"
    out_path.write_bytes(blob)
    print(f"✅ Anexo N°1 generado: {out_path}")


# ----------------------------------------------------------------------------
# argparse
# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anid-rendiciones-skill",
        description="Backend del skill /rendicion para Claude Code.",
    )
    p.add_argument("--root", help="Directorio raíz de proyectos (default: ~/Documents/anid-rendiciones)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    pi = sub.add_parser("init", help="Crear proyecto nuevo")
    pi.add_argument("codigo")
    pi.add_argument("--anio", type=int, required=True)
    pi.add_argument("--etapa", type=int, required=True)
    pi.add_argument("--inicio", required=True, help="YYYY-MM-DD")
    pi.add_argument("--fin", required=True, help="YYYY-MM-DD")
    pi.add_argument("--ip", help="Institución patrocinante")
    pi.add_argument("--rut-ip", dest="rut_ip")
    pi.add_argument("--facultad")
    pi.add_argument("--ir", help="Nombre del IR")
    pi.add_argument("--rut-ir", dest="rut_ir")
    pi.add_argument("--p-personal", type=int)
    pi.add_argument("--p-equipamiento", type=int)
    pi.add_argument("--p-infraestructura", type=int)
    pi.add_argument("--p-operacion", type=int)
    pi.add_argument("--p-indirectos", type=int)
    pi.add_argument("--n-rendicion", type=int, dest="n_rendicion")
    pi.add_argument("--n-cuota", type=int, dest="n_cuota")
    pi.add_argument("--monto-transferido", type=int, dest="monto_transferido")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_init)

    # list
    pl = sub.add_parser("list", help="Listar proyectos")
    pl.set_defaults(func=cmd_list)

    # budget
    pb = sub.add_parser("budget", help="Mostrar disponibles")
    pb.add_argument("codigo")
    pb.set_defaults(func=cmd_budget)

    # add
    pa = sub.add_parser("add", help="Procesar una boleta")
    pa.add_argument("codigo")
    pa.add_argument("archivo")
    pa.set_defaults(func=cmd_add)

    # add-dir
    pad = sub.add_parser("add-dir", help="Procesar una carpeta de boletas")
    pad.add_argument("codigo")
    pad.add_argument("carpeta")
    pad.set_defaults(func=cmd_add_dir)

    # show
    ps = sub.add_parser("show", help="Mostrar gastos del proyecto")
    ps.add_argument("codigo")
    ps.set_defaults(func=cmd_show)

    # remove
    pr = sub.add_parser("remove", help="Eliminar un gasto por n_correlativo")
    pr.add_argument("codigo")
    pr.add_argument("n", type=int)
    pr.set_defaults(func=cmd_remove)

    # validate
    pv = sub.add_parser("validate", help="Correr validación completa")
    pv.add_argument("codigo")
    pv.set_defaults(func=cmd_validate)

    # advise
    pad2 = sub.add_parser("advise", help="¿Cómo conviene rendir este gasto?")
    pad2.add_argument("codigo")
    pad2.add_argument("descripcion")
    pad2.add_argument("--monto", type=int, default=None)
    pad2.add_argument("--contexto", default=None)
    pad2.add_argument("--json", action="store_true")
    pad2.set_defaults(func=cmd_advise)

    # export
    pe = sub.add_parser("export", help="Generar Anexo N°1 XLSX")
    pe.add_argument("codigo")
    pe.set_defaults(func=cmd_export)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
