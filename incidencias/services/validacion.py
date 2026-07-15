"""Validación de adjuntos (capturas/logs) subidos a una Incidencia o Comentario."""
from django.conf import settings
from django.core.exceptions import ValidationError

EXTENSIONES_PERMITIDAS = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.pdf', '.txt', '.log', '.zip',
}


def validar_adjunto_incidencia(archivo):
    """archivo: UploadedFile de Django (request.FILES). Lanza ValidationError
    con mensaje claro si el archivo no cumple tipo/tamaño permitido."""
    nombre = (archivo.name or '').lower()
    extension = nombre[nombre.rfind('.'):] if '.' in nombre else ''
    if extension not in EXTENSIONES_PERMITIDAS:
        raise ValidationError(
            f"Tipo de archivo no permitido ({extension or 'sin extensión'}). "
            f"Permitidos: {', '.join(sorted(EXTENSIONES_PERMITIDAS))}."
        )

    max_bytes = settings.INCIDENCIAS_MAX_UPLOAD_MB * 1024 * 1024
    if archivo.size > max_bytes:
        raise ValidationError(
            f"El adjunto supera el tamaño máximo permitido ({settings.INCIDENCIAS_MAX_UPLOAD_MB} MB)."
        )
