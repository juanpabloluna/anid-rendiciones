"""Extracción estructurada de comprobantes con Claude vision.

Toma un archivo (PDF / JPG / PNG) y devuelve un `Gasto` parcialmente poblado.
La clasificación final (ítem/sub-ítem) se hace en `classify.py`.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from anthropic.types import MessageParam
from pdf2image import convert_from_path
from PIL import Image
import io

from .schema import Gasto, Moneda, TipoDocumento

log = logging.getLogger(__name__)

MODEL_VISION = "claude-sonnet-4-5-20250929"
MAX_PAGES_PER_DOC = 4
IMAGE_MAX_DIM = 1568  # Claude vision sweet spot

PROMPT_EXTRACCION = """Eres un asistente experto en rendiciones de cuentas de FONDECYT (ANID, Chile).

Te entrego un comprobante de gasto (boleta, factura, recibo, invoice, etc.).
Extrae los siguientes campos en JSON estricto. Si un campo no aparece, devuélvelo como null.

{
  "tipo_documento": "Boleta de Honorarios" | "Boleta de Ventas y Servicios" | "Boleta de Prestación de Servicios a Terceros" | "Factura" | "Factura Afecta" | "Factura Exenta" | "Invoice (internacional)" | "Liquidación de Sueldo" | "Recibo Simple" | "Recibo Simple Viáticos (Anexo N°6)" | "Recibo Simple Movilización (Anexo N°7)" | "Recibo Simple Personal Extranjero (Anexo N°4)" | "Comprobante Electrónico de Compra" | "Certificado de Pagos Aportes Patronales (PreviRed)" | "Cartola Bancaria" | "Formulario de Aduanas" | "Otro",
  "n_documento": "número/folio del documento, como string",
  "fecha_documento": "YYYY-MM-DD",
  "rut_beneficiario": "RUT del emisor/proveedor en formato 12345678-9 (con guion). Para invoices internacionales usar el TAX ID o null",
  "nombre_beneficiario": "nombre completo o razón social del proveedor / emisor",
  "detalle_gasto": "descripción específica del bien o servicio (1 línea, en español)",
  "monto_total": número entero en CLP (sin separadores ni moneda),
  "moneda_original": "CLP" | "USD" | "EUR" | "Otra",
  "monto_moneda_original": número decimal si la moneda original no es CLP, null si es CLP,
  "tipo_cambio": número decimal si aparece explícito en el documento, null si no aparece,
  "confianza_extraccion": número entre 0.0 y 1.0 reflejando qué tan seguro estás de los campos extraídos
}

Reglas:
- Para boletas chilenas en CLP: monto_total = monto bruto/total a pagar en pesos chilenos.
- Si el documento es internacional en otra moneda: monto_total = conversión a CLP usando tipo_cambio (si está en el documento), si no, deja monto_total en 0 y registra el valor original en monto_moneda_original.
- RUT chileno: siempre formato "12345678-9" o "12.345.678-9" → normaliza a "12345678-9".
- Si ves un código electrónico de compra (Chilecompra), úsalo como n_documento.
- Devuelve ÚNICAMENTE el JSON, sin texto adicional, sin markdown fences.
"""


def _pdf_to_images(pdf_path: Path, max_pages: int = MAX_PAGES_PER_DOC) -> list[Image.Image]:
    images = convert_from_path(pdf_path, dpi=200, first_page=1, last_page=max_pages)
    return images


def _load_image(path: Path) -> list[Image.Image]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_to_images(path)
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return [Image.open(path).convert("RGB")]
    raise ValueError(f"Formato no soportado: {suffix}")


def _resize(img: Image.Image, max_dim: int = IMAGE_MAX_DIM) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _image_to_b64(img: Image.Image, fmt: str = "JPEG", quality: int = 85) -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # strip markdown fences if model emitted them despite instructions
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        log.warning("fecha mal formada: %r", s)
        return None


def extraer_comprobante(
    archivo: Path,
    client: Anthropic,
    model: str = MODEL_VISION,
) -> Gasto:
    """Extrae un Gasto desde un comprobante.

    El Gasto retornado NO tiene ítem/sub-ítem (eso lo hace `classify.py`)
    ni n_correlativo (eso lo asigna el orquestador al ensamblar el Anexo 1).
    """
    images = _load_image(archivo)
    images = [_resize(img) for img in images[:MAX_PAGES_PER_DOC]]

    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _image_to_b64(img),
            },
        }
        for img in images
    ]
    content.append({"type": "text", "text": PROMPT_EXTRACCION})

    messages: list[MessageParam] = [{"role": "user", "content": content}]

    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=messages,
    )

    text = resp.content[0].text  # type: ignore[union-attr]
    try:
        data = _parse_json_response(text)
    except json.JSONDecodeError as e:
        log.error("Claude devolvió JSON inválido para %s: %s", archivo.name, e)
        return Gasto(
            archivo_origen=str(archivo),
            confianza_extraccion=0.0,
            detalle_gasto=f"[ERROR extracción: {e}]",
        )

    tipo_doc = data.get("tipo_documento")
    try:
        tipo_documento = TipoDocumento(tipo_doc) if tipo_doc else None
    except ValueError:
        log.warning("tipo_documento no reconocido: %r", tipo_doc)
        tipo_documento = TipoDocumento.OTRO

    moneda_str = data.get("moneda_original") or "CLP"
    try:
        moneda = Moneda(moneda_str)
    except ValueError:
        moneda = Moneda.OTRA

    return Gasto(
        tipo_documento=tipo_documento,
        n_documento=data.get("n_documento"),
        fecha_documento=_parse_date(data.get("fecha_documento")),
        rut_beneficiario=data.get("rut_beneficiario"),
        nombre_beneficiario=data.get("nombre_beneficiario"),
        detalle_gasto=data.get("detalle_gasto", "") or "",
        monto_total=int(data.get("monto_total") or 0),
        monto_rendido=int(data.get("monto_total") or 0),  # por defecto rinde 100%
        moneda_original=moneda,
        monto_moneda_original=data.get("monto_moneda_original"),
        tipo_cambio=data.get("tipo_cambio"),
        archivo_origen=str(archivo),
        confianza_extraccion=float(data.get("confianza_extraccion") or 0.5),
    )
