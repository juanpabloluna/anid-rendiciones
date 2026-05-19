"""Tests del motor de validación."""

from datetime import date
from pathlib import Path

import pytest

from anid_rendiciones.classify import cargar_reglas
from anid_rendiciones.schema import Gasto, Item, Proyecto, Severidad, TipoDocumento
from anid_rendiciones.validate import detectar_duplicados, validar_lote

REPO_ROOT = Path(__file__).parent.parent
REGLAS_G = cargar_reglas(REPO_ROOT / "rules" / "general" / "rex_7_2026.yaml")
REGLAS_C = cargar_reglas(REPO_ROOT / "rules" / "concurso" / "fondecyt_regular_2022plus.yaml")


@pytest.fixture
def proyecto() -> Proyecto:
    return Proyecto(
        codigo="1240999",
        anio_concurso=2024,
        etapa=1,
        fecha_inicio_etapa=date(2025, 4, 1),
        fecha_fin_etapa=date(2026, 3, 31),
        institucion_patrocinante="Universidad Demo",
        rut_ip="60.000.000-1",
        investigador_responsable="Demo IR",
        rut_ir="10.000.000-9",
    )


def _ids(gasto: Gasto) -> set[str]:
    return {h.regla_id for h in gasto.hallazgos}


def test_gasto_valido_no_genera_hallazgos(proyecto):
    g = Gasto(
        item=Item.OPERACION,
        subitem="Bienes y Materiales",
        rut_beneficiario="76190370-5",
        nombre_beneficiario="Proveedor S.A.",
        detalle_gasto="Reactivos químicos",
        tipo_documento=TipoDocumento.FACTURA,
        n_documento="123",
        fecha_documento=date(2025, 5, 1),
        monto_total=150000,
        monto_rendido=150000,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert val.hallazgos == []


def test_factura_obligatoria_sobre_500k(proyecto):
    g = Gasto(
        item=Item.OPERACION,
        subitem="Bienes y Materiales",
        rut_beneficiario="76190370-5",
        nombre_beneficiario="X",
        detalle_gasto="Compra grande",
        tipo_documento=TipoDocumento.BOLETA_VENTAS,
        n_documento="999",
        fecha_documento=date(2025, 5, 1),
        monto_total=750000,
        monto_rendido=750000,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert "factura_obligatoria_500k" in _ids(val)


def test_alcohol_prohibido(proyecto):
    g = Gasto(
        item=Item.OPERACION,
        subitem="Atención Reuniones",
        rut_beneficiario="12345678-9",
        detalle_gasto="Vino y queso",
        tipo_documento=TipoDocumento.FACTURA,
        n_documento="1",
        fecha_documento=date(2025, 5, 1),
        monto_total=50000,
        monto_rendido=50000,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert "alcohol_prohibido" in _ids(val)


def test_fecha_fuera_de_etapa(proyecto):
    g = Gasto(
        item=Item.OPERACION,
        subitem="Publicaciones",
        rut_beneficiario="12345678-9",
        detalle_gasto="APC paper",
        tipo_documento=TipoDocumento.FACTURA,
        n_documento="1",
        fecha_documento=date(2024, 1, 1),  # antes de inicio etapa
        monto_total=100000,
        monto_rendido=100000,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert "fecha_dentro_etapa" in _ids(val)


def test_duplicado_detectado(proyecto):
    base = Gasto(
        item=Item.OPERACION,
        subitem="Bienes y Materiales",
        rut_beneficiario="76190370-5",
        detalle_gasto="X",
        tipo_documento=TipoDocumento.FACTURA,
        n_documento="555",
        fecha_documento=date(2025, 6, 1),
        monto_total=100000,
        monto_rendido=100000,
    )
    val = validar_lote([base, base.model_copy()], proyecto, REGLAS_G, REGLAS_C)
    assert "rex7.duplicado" in _ids(val[1])
    assert "rex7.duplicado" not in _ids(val[0])  # primer ocurrencia no se marca


def test_boleta_honorarios_advertencia_anexo3(proyecto):
    g = Gasto(
        item=Item.PERSONAL,
        subitem="Profesionales",
        rut_beneficiario="12345678-9",
        detalle_gasto="Asistencia investigación",
        tipo_documento=TipoDocumento.BOLETA_HONORARIOS,
        n_documento="1",
        fecha_documento=date(2025, 6, 1),
        monto_total=400000,
        monto_rendido=400000,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert any(
        h.regla_id == "boleta_honorarios_sin_anexo3" and h.severidad == Severidad.ADVERTENCIA
        for h in val.hallazgos
    )


def test_moneda_extranjera_sin_tipo_cambio(proyecto):
    from anid_rendiciones.schema import Moneda

    g = Gasto(
        item=Item.OPERACION,
        subitem="Publicaciones",
        rut_beneficiario="N/A",
        nombre_beneficiario="Elsevier",
        detalle_gasto="APC",
        tipo_documento=TipoDocumento.INVOICE,
        n_documento="INV-1",
        fecha_documento=date(2025, 5, 1),
        monto_total=0,
        monto_rendido=0,
        moneda_original=Moneda.USD,
        monto_moneda_original=2500.0,
        tipo_cambio=None,
    )
    val = validar_lote([g], proyecto, REGLAS_G, REGLAS_C)[0]
    assert "moneda_extranjera_sin_tipo_cambio" in _ids(val)
