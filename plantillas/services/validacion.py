"""Validación del .docx subido por legal antes de guardarlo como PlantillaDocumento."""
import zipfile

from django.conf import settings
from django.core.exceptions import ValidationError


def validar_docx_subido(archivo):
    """archivo: UploadedFile de Django (request.FILES). Lanza ValidationError
    con mensaje claro si el archivo no es un .docx válido y seguro."""
    nombre = (archivo.name or '').lower()
    if not nombre.endswith('.docx'):
        raise ValidationError("La plantilla debe ser un archivo .docx (Word).")

    max_bytes = settings.PLANTILLAS_MAX_UPLOAD_MB * 1024 * 1024
    if archivo.size > max_bytes:
        raise ValidationError(
            f"La plantilla supera el tamaño máximo permitido ({settings.PLANTILLAS_MAX_UPLOAD_MB} MB)."
        )

    if not zipfile.is_zipfile(archivo):
        raise ValidationError("El archivo no es un .docx válido (no es un paquete OOXML/zip).")

    archivo.seek(0)
    with zipfile.ZipFile(archivo) as zf:
        nombres_internos = zf.namelist()
        if any('vbaProject' in n for n in nombres_internos):
            raise ValidationError(
                "La plantilla contiene macros (VBA), lo cual no está permitido por seguridad."
            )
        if 'word/document.xml' not in nombres_internos:
            raise ValidationError("El archivo no tiene la estructura interna esperada de un .docx.")

    archivo.seek(0)
