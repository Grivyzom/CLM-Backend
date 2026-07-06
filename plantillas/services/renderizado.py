"""Motor de renderizado: plantilla .docx (docxtpl) + contexto -> DocumentoGenerado
(.docx interno + .pdf inmutable descargable).
"""
import hashlib
import io
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from docxtpl import DocxTemplate
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError, UndefinedError

class TolerantUndefined(StrictUndefined):
    """
    Permite el uso de variables como '____________' (líneas de firma) 
    sin arrojar error, asumiendo que son espacios para completar a mano.
    """
    def __str__(self):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return self._undefined_name
        return super().__str__()

    def __html__(self):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return self._undefined_name
        return super().__html__()

    def __bool__(self):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return False
        return super().__bool__()

    def __getattr__(self, name):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return self
        return super().__getattr__(name)

    def __getitem__(self, key):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return self
        return super().__getitem__(key)

    def __call__(self, *args, **kwargs):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return self
        return super().__call__(*args, **kwargs)

    def __iter__(self):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return iter([])
        return super().__iter__()

    def __len__(self):
        if self._undefined_name and self._undefined_name.startswith('___'):
            return 0
        return super().__len__()

from .contexto import construir_contexto
from ..models import DocumentoGenerado, PlantillaDocumento


class PlantillaRenderError(Exception):
    """Error genérico al renderizar una plantilla o convertirla a PDF."""


class VariablesFaltantesError(PlantillaRenderError):
    """La plantilla referencia una variable que no existe en el contexto del contrato."""


class ConversionPDFError(PlantillaRenderError):
    """Falló la conversión docx -> PDF vía LibreOffice."""


class SinPlantillaActivaError(PlantillaRenderError):
    """No hay ninguna PlantillaDocumento activa (ni específica ni global) para el tipo de contrato."""


def renderizar_docx(plantilla: PlantillaDocumento, contexto: dict) -> bytes:
    """Renderiza la plantilla .docx con el contexto dado. Devuelve los bytes del docx resultante."""
    try:
        doc = DocxTemplate(plantilla.archivo_docx.path)
        # TolerantUndefined: permite '____________' pero falla si es otra variable no resuelta.
        jinja_env = Environment(undefined=TolerantUndefined)
        doc.render(contexto, jinja_env=jinja_env)
    except UndefinedError as exc:
        raise VariablesFaltantesError(
            f"La plantilla usa una variable que no existe en los datos del contrato: {exc}"
        ) from exc
    except TemplateError as exc:
        raise PlantillaRenderError(f"Error de sintaxis en la plantilla: {exc}") from exc

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def convertir_a_pdf(docx_bytes: bytes) -> bytes:
    """Convierte bytes de un .docx a PDF usando LibreOffice headless.

    Cada invocación usa un perfil de usuario (-env:UserInstallation) único y
    temporal: LibreOffice usa un lock global sobre su perfil, así que si dos
    conversiones corren en paralelo compartiendo perfil, la segunda cuelga o
    falla. Esto es crítico bajo varios workers de gunicorn generando
    documentos al mismo tiempo.
    """
    binario = getattr(settings, 'LIBREOFFICE_BINARY', 'soffice')

    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / 'entrada.docx'
        docx_path.write_bytes(docx_bytes)

        perfil_dir = Path(tmpdir) / f'perfil-{uuid.uuid4().hex}'

        try:
            resultado = subprocess.run(
                [
                    binario, '--headless', '--norestore',
                    f'-env:UserInstallation=file://{perfil_dir}',
                    '--convert-to', 'pdf', '--outdir', tmpdir, str(docx_path),
                ],
                check=True, timeout=60, capture_output=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ConversionPDFError(f"Falló la conversión a PDF (LibreOffice): {exc}") from exc

        pdf_path = docx_path.with_suffix('.pdf')
        if not pdf_path.exists():
            detalle = resultado.stderr.decode(errors='replace') if resultado.stderr else ''
            raise ConversionPDFError(f"LibreOffice no generó el PDF esperado. {detalle}")

        return pdf_path.read_bytes()


def resolver_plantilla_activa(tipo_contrato: str, software_id) -> PlantillaDocumento:
    """Plantilla activa específica del software; si no hay, fallback a la activa global."""
    especifica = PlantillaDocumento.objects.filter(
        tipo_contrato=tipo_contrato, software_id=software_id, activa=True,
    ).first()
    if especifica:
        return especifica

    global_ = PlantillaDocumento.objects.filter(
        tipo_contrato=tipo_contrato, software__isnull=True, activa=True,
    ).first()
    if global_:
        return global_

    raise SinPlantillaActivaError(
        f"No hay ninguna plantilla activa para tipo_contrato={tipo_contrato} "
        f"(ni específica del software {software_id} ni global)."
    )


def generar_documento(contrato, plantilla: PlantillaDocumento = None, usuario=None) -> DocumentoGenerado:
    """Orquesta: resuelve plantilla si no viene dada, construye contexto,
    renderiza docx, convierte a PDF, calcula hash y crea el DocumentoGenerado
    (write-once — nunca actualiza un registro existente)."""
    if plantilla is None:
        plantilla = resolver_plantilla_activa(contrato.tipo_contrato, contrato.software_id)

    contexto = construir_contexto(contrato)
    docx_bytes = renderizar_docx(plantilla, contexto)
    pdf_bytes = convertir_a_pdf(docx_bytes)
    hash_pdf = hashlib.sha256(pdf_bytes).hexdigest()

    # El contexto se serializa para auditoría: valores no-JSON-nativos (Decimal, date)
    # se convierten a texto para poder guardarlos en el JSONField.
    contexto_serializable = _serializar_contexto(contexto)

    with transaction.atomic():
        documento = DocumentoGenerado.objects.create(
            contrato=contrato,
            plantilla=plantilla,
            hash_sha256=hash_pdf,
            contexto_usado=contexto_serializable,
            generado_por=usuario if (usuario and usuario.is_authenticated) else None,
        )
        nombre_base = f"contrato_{contrato.id}_{plantilla.version_codigo}_{documento.id}"
        documento.archivo_docx.save(f"{nombre_base}.docx", ContentFile(docx_bytes), save=False)
        documento.archivo_pdf.save(f"{nombre_base}.pdf", ContentFile(pdf_bytes), save=False)
        documento.save(update_fields=['archivo_docx', 'archivo_pdf'])

    return documento


def _serializar_contexto(contexto: dict):
    """Convierte recursivamente Decimal/date/etc. a tipos JSON-nativos para
    poder guardar el contexto usado como snapshot de auditoría."""
    import decimal
    import datetime

    if isinstance(contexto, dict):
        return {k: _serializar_contexto(v) for k, v in contexto.items()}
    if isinstance(contexto, list):
        return [_serializar_contexto(v) for v in contexto]
    if isinstance(contexto, (decimal.Decimal, datetime.date, datetime.datetime)):
        return str(contexto)
    return contexto
