"""Estado persistente por proyecto FONDECYT.

Cada proyecto vive en un directorio:
  <root>/<codigo>/
    state.json       # Proyecto + gastos + metadata
    boletas/         # PDFs/JPGs originales (opcional, custodia del IR)

Operaciones principales:
  - cargar(root, codigo) → ProyectoEstado
  - guardar(estado) → persiste en disco
  - agregar_gasto(estado, gasto) → actualiza acumulados
  - disponibles(estado) → dict ítem → CLP restantes
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .schema import Gasto, Item, Proyecto

DEFAULT_ROOT = Path.home() / "Documents" / "anid-rendiciones"


class ProyectoEstado(BaseModel):
    """Estado completo de la rendición de un proyecto."""

    proyecto: Proyecto
    gastos: list[Gasto] = Field(default_factory=list)
    creado_en: datetime = Field(default_factory=datetime.now)
    actualizado_en: datetime = Field(default_factory=datetime.now)
    version_schema: int = 1

    def acumulado_por_item(self) -> dict[str, int]:
        out: dict[str, int] = {item.value: 0 for item in Item}
        for g in self.gastos:
            if g.item:
                out[g.item.value] = out.get(g.item.value, 0) + g.monto_rendido
        return out

    def disponibles(self) -> dict[str, int]:
        """Disponible por ítem = presupuesto inicial − rendido acumulado."""
        ac = self.acumulado_por_item()
        return {
            Item.PERSONAL.value: self.proyecto.presupuesto_personal - ac.get(Item.PERSONAL.value, 0),
            Item.EQUIPAMIENTO.value: self.proyecto.presupuesto_equipamiento - ac.get(Item.EQUIPAMIENTO.value, 0),
            Item.INFRAESTRUCTURA.value: self.proyecto.presupuesto_infraestructura - ac.get(Item.INFRAESTRUCTURA.value, 0),
            Item.OPERACION.value: self.proyecto.presupuesto_operacion - ac.get(Item.OPERACION.value, 0),
            Item.INDIRECTOS.value: self.proyecto.presupuesto_indirectos - ac.get(Item.INDIRECTOS.value, 0),
        }

    def acumulado_por_subitem(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for g in self.gastos:
            if g.item and g.subitem:
                key = (g.item.value, g.subitem)
                out[key] = out.get(key, 0) + g.monto_rendido
        return out


def directorio_proyecto(codigo: str, root: Optional[Path] = None) -> Path:
    root = root or DEFAULT_ROOT
    return root / codigo


def ruta_state(codigo: str, root: Optional[Path] = None) -> Path:
    return directorio_proyecto(codigo, root) / "state.json"


def existe_proyecto(codigo: str, root: Optional[Path] = None) -> bool:
    return ruta_state(codigo, root).exists()


def crear(proyecto: Proyecto, root: Optional[Path] = None, overwrite: bool = False) -> ProyectoEstado:
    """Crea el directorio + state.json inicial."""
    dir_ = directorio_proyecto(proyecto.codigo, root)
    state_path = dir_ / "state.json"
    if state_path.exists() and not overwrite:
        raise FileExistsError(f"Ya existe un proyecto en {state_path}. Usa overwrite=True para reemplazarlo.")
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "boletas").mkdir(exist_ok=True)
    estado = ProyectoEstado(proyecto=proyecto)
    guardar(estado, root)
    return estado


def cargar(codigo: str, root: Optional[Path] = None) -> ProyectoEstado:
    path = ruta_state(codigo, root)
    if not path.exists():
        raise FileNotFoundError(f"No existe estado para proyecto {codigo} en {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ProyectoEstado.model_validate(data)


def guardar(estado: ProyectoEstado, root: Optional[Path] = None) -> Path:
    estado.actualizado_en = datetime.now()
    path = ruta_state(estado.proyecto.codigo, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(estado.model_dump(mode="json"), f, ensure_ascii=False, indent=2, default=str)
    return path


def agregar_gasto(estado: ProyectoEstado, gasto: Gasto, root: Optional[Path] = None) -> ProyectoEstado:
    """Agrega un gasto al estado y persiste. Asigna n_correlativo si falta."""
    if gasto.n_correlativo is None:
        gasto = gasto.model_copy(update={"n_correlativo": len(estado.gastos) + 1})
    estado.gastos.append(gasto)
    guardar(estado, root)
    return estado


def eliminar_gasto(estado: ProyectoEstado, n_correlativo: int, root: Optional[Path] = None) -> ProyectoEstado:
    estado.gastos = [g for g in estado.gastos if g.n_correlativo != n_correlativo]
    # Reasignar correlativos
    for i, g in enumerate(estado.gastos):
        g.n_correlativo = i + 1
    guardar(estado, root)
    return estado


def listar_proyectos(root: Optional[Path] = None) -> list[str]:
    root = root or DEFAULT_ROOT
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "state.json").exists())


def resumen_disponibles(estado: ProyectoEstado) -> str:
    """Versión legible (markdown) del estado de disponibles."""
    disp = estado.disponibles()
    ac = estado.acumulado_por_item()
    p = estado.proyecto

    lines = [
        f"## Proyecto {p.codigo} — Etapa {p.etapa} (concurso {p.anio_concurso})",
        f"Período: {p.fecha_inicio_etapa.isoformat()} → {p.fecha_fin_etapa.isoformat()}",
        f"Monto transferido (cuota actual): ${p.monto_transferido:,} CLP",
        f"Gastos rendidos: **{len(estado.gastos)}**",
        "",
        "| Ítem | Presupuesto | Rendido | Disponible | % usado |",
        "|---|---:|---:|---:|---:|",
    ]
    items_info = [
        ("Gastos en Personal", p.presupuesto_personal),
        ("Equipamiento", p.presupuesto_equipamiento),
        ("Infraestructura y Mobiliario", p.presupuesto_infraestructura),
        ("Gastos de Operación", p.presupuesto_operacion),
        ("Gastos de Administración Indirectos", p.presupuesto_indirectos),
    ]
    for nombre, pres in items_info:
        rendido = ac.get(nombre, 0)
        disponible = disp.get(nombre, 0)
        pct = (rendido / pres * 100) if pres > 0 else 0
        lines.append(
            f"| {nombre} | ${pres:,} | ${rendido:,} | ${disponible:,} | {pct:.1f}% |"
        )
    return "\n".join(lines)
