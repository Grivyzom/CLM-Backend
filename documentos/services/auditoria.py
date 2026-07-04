"""
Trazabilidad de documentos exportados: nomenclatura estándar de archivo +
metadatos de auditoría (autor, IP, entorno, filtros aplicados) inyectados
en las propiedades nativas del documento (OpenXML docProps).
"""
import re
from django.conf import settings
from django.utils import timezone


def codigo_emisor(user):
    """Abreviatura estandarizada del emisor para el nombre de archivo (sin espacios/tildes)."""
    if getattr(user, 'is_superuser', False):
        return 'ADM-GLOBAL'
    if getattr(user, 'is_staff', False):
        return f'STAFF{user.id}'
    base = re.sub(r'[^A-Za-z0-9]', '', user.get_username() or '').upper()
    return f'EMP{base[:10] or user.id}'


def rol_emisor(user):
    """Rol legible del emisor, usado en el bloque de auditoría (Comments)."""
    if getattr(user, 'is_superuser', False):
        return 'Administrador Global'
    if getattr(user, 'is_staff', False):
        return 'Staff'
    return 'Usuario Estándar'


def get_client_ip(request):
    """IP real del cliente, respetando proxy/load balancer (X-Forwarded-For)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def entorno_version():
    """Entorno + versión de la app (ej. 'v1.0.4-prod')."""
    version = getattr(settings, 'APP_VERSION', '1.0.0')
    entorno = 'dev' if settings.DEBUG else 'prod'
    return f'v{version}-{entorno}'


def build_export_filename(nombre_base, user, ext):
    """
    [NombreBase]_[CodigoEmisor]_[FechaISO]_[Hora24h].[ext]
    Ej: Clientes_ADM-GLOBAL_20260704_1725.xlsx
    Sin espacios/tildes/ñ para máxima compatibilidad y orden cronológico exacto.
    """
    ahora = timezone.localtime()
    fecha = ahora.strftime('%Y%m%d')
    hora = ahora.strftime('%H%M')
    return f'{nombre_base}_{codigo_emisor(user)}_{fecha}_{hora}.{ext}'


def build_audit_meta(request, titulo, filtros_desc):
    """
    Metadatos de auditoría a inyectar en el documento exportado.
    Permiten trazar quién, cuándo, desde qué IP y con qué recorte de datos se
    generó el archivo, aunque este circule fuera de la plataforma.
    """
    user = request.user
    nombre = user.get_full_name() or user.get_username()
    return {
        'usuario_nombre': nombre,
        'usuario_id': user.id,
        'rol': rol_emisor(user),
        'ip': get_client_ip(request),
        'entorno': entorno_version(),
        'filtros': filtros_desc,
        'company': getattr(settings, 'APP_NAME', 'Enfoque Platform'),
        'titulo': titulo,
    }


def describir_filtros_clientes(request):
    """Resumen legible de los filtros GET activos, para dejar constancia de
    qué recorte de la base de datos se exportó."""
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        n = len([x for x in ids_param.split(',') if x.strip()])
        return f'Selección manual: {n} cliente(s)'

    partes = []
    search = request.GET.get('search', '').strip()
    estado = request.GET.get('estado', 'Todos').strip()
    tipo = request.GET.get('tipo', 'Todos').strip()
    fecha_desde = request.GET.get('fecha_desde', '').strip()
    fecha_hasta = request.GET.get('fecha_hasta', '').strip()

    if search:
        partes.append(f"Búsqueda: '{search}'")
    if estado not in ('Todos', ''):
        partes.append(f'Estado: {estado}')
    if tipo not in ('Todos', ''):
        partes.append(f'Tipo: {tipo}')
    if fecha_desde or fecha_hasta:
        partes.append(f"Fecha: {fecha_desde or '…'} → {fecha_hasta or '…'}")

    return '; '.join(partes) if partes else 'Sin filtros (todos los registros)'


def describir_filtros_contratos(request):
    """Resumen legible del recorte exportado (por ahora solo soporta selección
    manual vía 'ids'; sin filtros de tabla aún porque no existe esa UI)."""
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        n = len([x for x in ids_param.split(',') if x.strip()])
        return f'Selección manual: {n} contrato(s)'
    return 'Sin filtros (todos los registros)'
