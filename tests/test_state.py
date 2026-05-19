"""Tests del state persistente."""

from datetime import date
from pathlib import Path

import pytest

from anid_rendiciones import state
from anid_rendiciones.schema import Gasto, Item, Proyecto, TipoDocumento


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def proyecto() -> Proyecto:
    return Proyecto(
        codigo="1240000",
        anio_concurso=2024,
        etapa=1,
        fecha_inicio_etapa=date(2025, 4, 1),
        fecha_fin_etapa=date(2026, 3, 31),
        institucion_patrocinante="UCh",
        rut_ip="60.910.000-1",
        investigador_responsable="X",
        rut_ir="10-9",
        presupuesto_personal=10_000_000,
        presupuesto_equipamiento=5_000_000,
        presupuesto_operacion=8_000_000,
        monto_transferido=23_000_000,
    )


def _g(item, sub, monto, n_doc="1") -> Gasto:
    return Gasto(
        item=item,
        subitem=sub,
        rut_beneficiario="12345678-9",
        detalle_gasto="x",
        tipo_documento=TipoDocumento.FACTURA,
        n_documento=n_doc,
        fecha_documento=date(2025, 5, 1),
        monto_total=monto,
        monto_rendido=monto,
    )


def test_crear_y_cargar(proyecto, root):
    e = state.crear(proyecto, root)
    e2 = state.cargar(proyecto.codigo, root)
    assert e2.proyecto.codigo == proyecto.codigo
    assert e2.gastos == []


def test_no_sobrescribe_sin_force(proyecto, root):
    state.crear(proyecto, root)
    with pytest.raises(FileExistsError):
        state.crear(proyecto, root)
    state.crear(proyecto, root, overwrite=True)  # OK con force


def test_agregar_gasto_asigna_correlativo(proyecto, root):
    e = state.crear(proyecto, root)
    e = state.agregar_gasto(e, _g(Item.OPERACION, "Bienes y Materiales", 100_000), root)
    e = state.agregar_gasto(e, _g(Item.OPERACION, "Publicaciones", 200_000, "2"), root)
    assert [g.n_correlativo for g in e.gastos] == [1, 2]


def test_disponibles(proyecto, root):
    e = state.crear(proyecto, root)
    e = state.agregar_gasto(e, _g(Item.OPERACION, "Bienes y Materiales", 1_000_000), root)
    e = state.agregar_gasto(e, _g(Item.PERSONAL, "Tesistas", 2_500_000, "2"), root)
    e = state.agregar_gasto(e, _g(Item.EQUIPAMIENTO, "Nacionales", 500_000, "3"), root)
    disp = e.disponibles()
    assert disp[Item.OPERACION.value] == 7_000_000
    assert disp[Item.PERSONAL.value] == 7_500_000
    assert disp[Item.EQUIPAMIENTO.value] == 4_500_000


def test_eliminar_reasigna_correlativos(proyecto, root):
    e = state.crear(proyecto, root)
    for i in range(3):
        e = state.agregar_gasto(e, _g(Item.OPERACION, "Publicaciones", 100_000, str(i + 1)), root)
    e = state.eliminar_gasto(e, 2, root)
    assert [g.n_correlativo for g in e.gastos] == [1, 2]
    assert [g.n_documento for g in e.gastos] == ["1", "3"]


def test_listar_proyectos(proyecto, root):
    state.crear(proyecto, root)
    proyecto2 = proyecto.model_copy(update={"codigo": "9999999"})
    state.crear(proyecto2, root)
    assert sorted(state.listar_proyectos(root)) == ["1240000", "9999999"]


def test_resumen_markdown_no_falla(proyecto, root):
    e = state.crear(proyecto, root)
    e = state.agregar_gasto(e, _g(Item.OPERACION, "Bienes y Materiales", 500_000), root)
    md = state.resumen_disponibles(e)
    assert "Gastos de Operación" in md
    assert "$500,000" in md or "$500000" in md
