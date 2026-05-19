"""Modelos de datos para rendiciones FONDECYT.

El schema central es `Gasto`, que mapea 1:1 con una fila del Anexo N°1
"Detalle Gastos". `Proyecto` y `Etapa` capturan el contexto del proyecto
para validación.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Item(str, Enum):
    PERSONAL = "Gastos en Personal"
    EQUIPAMIENTO = "Equipamiento"
    INFRAESTRUCTURA = "Infraestructura y Mobiliario"
    OPERACION = "Gastos de Operación"
    INDIRECTOS = "Gastos de Administración Indirectos"


class TipoDocumento(str, Enum):
    BOLETA_HONORARIOS = "Boleta de Honorarios"
    BOLETA_VENTAS = "Boleta de Ventas y Servicios"
    BOLETA_TERCEROS = "Boleta de Prestación de Servicios a Terceros"
    FACTURA = "Factura"
    FACTURA_AFECTA = "Factura Afecta"
    FACTURA_EXENTA = "Factura Exenta"
    INVOICE = "Invoice (internacional)"
    LIQUIDACION_SUELDO = "Liquidación de Sueldo"
    RECIBO_SIMPLE = "Recibo Simple"
    RECIBO_VIATICOS = "Recibo Simple Viáticos (Anexo N°6)"
    RECIBO_MOVILIZACION = "Recibo Simple Movilización (Anexo N°7)"
    RECIBO_EXTRANJERO = "Recibo Simple Personal Extranjero (Anexo N°4)"
    COMPROBANTE_ELECTRONICO = "Comprobante Electrónico de Compra"
    CERTIFICADO_PREVIRED = "Certificado de Pagos Aportes Patronales (PreviRed)"
    CARTOLA_BANCARIA = "Cartola Bancaria"
    FORMULARIO_ADUANAS = "Formulario de Aduanas"
    OTRO = "Otro"


class Moneda(str, Enum):
    CLP = "CLP"
    USD = "USD"
    EUR = "EUR"
    OTRA = "Otra"


class Severidad(str, Enum):
    ERROR = "ERROR"
    ADVERTENCIA = "ADVERTENCIA"
    INFO = "INFO"


class Hallazgo(BaseModel):
    """Issue detectado por el motor de validación."""

    regla_id: str = Field(..., description="ID de la regla (e.g. 'rex7.factura_500k')")
    severidad: Severidad
    mensaje: str
    sugerencia: Optional[str] = None


class Proyecto(BaseModel):
    """Configuración del proyecto FONDECYT para una rendición."""

    codigo: str = Field(..., description='Ej. "1240123"')
    concurso: Literal["Regular"] = "Regular"
    anio_concurso: int = Field(..., ge=2019, le=2030)
    etapa: int = Field(..., ge=1, le=6)
    fecha_inicio_etapa: date
    fecha_fin_etapa: date

    institucion_patrocinante: str = Field(..., description="Nombre de la IP o 'Persona Natural'")
    rut_ip: Optional[str] = Field(None, description="RUT de la IP (con guión y DV)")
    facultad: Optional[str] = Field(None, description="Sólo para universidades")

    investigador_responsable: str
    rut_ir: str

    presupuesto_personal: int = 0
    presupuesto_equipamiento: int = 0
    presupuesto_infraestructura: int = 0
    presupuesto_operacion: int = 0
    presupuesto_indirectos: int = 0

    n_rendicion: int = 1
    n_cuota_transferida: int = 1
    monto_transferido: int = 0

    @property
    def presupuesto_total(self) -> int:
        return (
            self.presupuesto_personal
            + self.presupuesto_equipamiento
            + self.presupuesto_infraestructura
            + self.presupuesto_operacion
            + self.presupuesto_indirectos
        )


class Gasto(BaseModel):
    """Una línea del Anexo N°1 "Detalle Gastos".

    Mapeo a columnas del template oficial:
      n_correlativo, item, subitem, rut_beneficiario, nombre_beneficiario,
      detalle_gasto, tipo_documento, n_documento, fecha_documento,
      monto_total, monto_rendido, porcentaje_rendido, justificacion
    """

    n_correlativo: Optional[int] = None
    item: Optional[Item] = None
    subitem: Optional[str] = None
    rut_beneficiario: Optional[str] = None
    nombre_beneficiario: Optional[str] = None
    detalle_gasto: str = ""
    tipo_documento: Optional[TipoDocumento] = None
    n_documento: Optional[str] = None
    fecha_documento: Optional[date] = None

    monto_total: int = 0
    monto_rendido: int = 0
    porcentaje_rendido: float = 100.0
    justificacion: Optional[str] = None

    moneda_original: Moneda = Moneda.CLP
    monto_moneda_original: Optional[float] = None
    tipo_cambio: Optional[float] = None

    archivo_origen: Optional[str] = Field(None, description="Ruta al PDF/JPG original")
    pagina_origen: Optional[int] = None
    confianza_extraccion: float = Field(0.0, ge=0.0, le=1.0)
    hallazgos: list[Hallazgo] = Field(default_factory=list)
    revisado_humano: bool = False

    @field_validator("rut_beneficiario")
    @classmethod
    def normalizar_rut(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        rut = v.replace(".", "").replace(" ", "").upper()
        if "-" not in rut and len(rut) > 1:
            rut = f"{rut[:-1]}-{rut[-1]}"
        return rut

    @field_validator("porcentaje_rendido")
    @classmethod
    def validar_porcentaje(cls, v: float) -> float:
        if not 0 <= v <= 100:
            raise ValueError("porcentaje_rendido debe estar entre 0 y 100")
        return v

    def tiene_errores(self) -> bool:
        return any(h.severidad == Severidad.ERROR for h in self.hallazgos)
