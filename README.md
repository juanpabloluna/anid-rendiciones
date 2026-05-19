# anid-rendiciones

Asistente **local** para preparar rendiciones de cuentas FONDECYT (ANID — Chile).

Procesa 50–100 comprobantes (boletas, facturas, recibos) por etapa, extrae los datos con visión de Claude, clasifica cada gasto en el ítem correcto del presupuesto, valida contra la normativa vigente (REX 7/2026 + Anexo Concurso Regular 2022+), y genera el **Anexo N°1 (Formulario de Rendición)** listo para enviar.

> **Privacidad por diseño.** Toda la información se procesa localmente en tu computador. Las imágenes y datos extraídos viajan únicamente entre tu máquina y la API de Anthropic (con tu propia API key). No hay backend intermedio, no hay base de datos compartida.

## Instalación

```bash
pipx install anid-rendiciones
```

Requisitos:
- Python ≥ 3.11
- Una API key de Anthropic (https://console.anthropic.com)
- `poppler` instalado en el sistema (para convertir PDFs a imágenes):
  - macOS: `brew install poppler`
  - Linux: `sudo apt install poppler-utils`

## Uso

```bash
anid-rendiciones
```

Esto abre la app de Streamlit en `http://localhost:8501`.

Flujo:

1. **Configurar el proyecto**: código FONDECYT, etapa, año del concurso, fechas, presupuesto por ítem, institución patrocinante (IP).
2. **Cargar comprobantes**: arrastrar la carpeta con todos los PDFs/JPGs de la etapa.
3. **Revisar la extracción**: Claude lee cada documento y propone fila para el Anexo 1. Se editan los campos manualmente cuando es necesario.
4. **Validar**: el sistema corre la lista de chequeo (Anexo 9/10 según corresponda) y reporta gastos problemáticos (monto > $500.000 sin factura, fechas fuera de etapa, alcohol, duplicados, etc.).
5. **Generar Anexo 1**: descarga el XLSX completo y la carpeta con los comprobantes ordenados por ítem.

## Alcance v1

- **Concurso**: FONDECYT Regular (todas las etapas).
- **Normativa**: REX 7/2026 (Instructivo General) + Anexo Concurso Regular 2022 y siguientes.
- **Idioma**: español.
- **Modo**: local (no hay versión hospedada).

Futuras versiones: Iniciación, Postdoctorado, Fondef, generación de Anexos 3/5/6/7/8 automáticos, empaquetado de PDFs en orden Anexo 1 con fragmentación a 50 MB.

## Estructura del repo

```
anid-rendiciones/
├── src/anid_rendiciones/
│   ├── app.py              # Streamlit UI
│   ├── cli.py              # entry point para pipx
│   ├── schema.py           # modelos Pydantic
│   ├── extract.py          # OCR + extracción estructurada (Claude vision)
│   ├── classify.py         # asignación de ítem/sub-ítem
│   ├── validate.py         # motor de reglas
│   └── generate/
│       └── anexo1.py       # poblado del Anexo 1 XLSX
├── rules/
│   ├── general/rex_7_2026.yaml
│   └── concurso/fondecyt_regular_2022plus.yaml
├── templates/anid_2026/    # plantillas oficiales descargadas de ANID
└── tests/
```

## Licencia

MIT. Ver [LICENSE](LICENSE).

## Aviso

Este software es una ayuda. **La responsabilidad legal y administrativa de la rendición es del/la Investigador(a) Responsable (IR)**. Siempre revisa la salida contra las bases de tu concurso, el instructivo vigente y los requisitos de tu institución patrocinante antes de enviar.
