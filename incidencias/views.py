from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from catalogo.models import Producto
from clientes.models import Cliente
from contratos.models import Contrato
from tenants.models import RolTenant, User
from tenants.permissions import IsPlatformClienteAccess, IsTenantMember, RequiresFeature
from tenants.scoping import scoped

from .models import (
    AdjuntoIncidencia,
    ComentarioIncidencia,
    EstadoIncidencia,
    Incidencia,
    SeveridadIncidencia,
)
from .permissions import CanManageIncidencia
from .services.validacion import validar_adjunto_incidencia

STAFF_ROLES = (RolTenant.TENANT_ADMIN, RolTenant.OPERADOR)


# ─── Scoping ──────────────────────────────────────────────────────────────────
def _scope_incidencias(qs, request):
    """scoped() por tenant no basta: un usuario role=CLIENTE comparte tenant_id
    con el resto de empleados/clientes de la empresa que lo atiende, así que sin
    este filtro adicional vería incidencias de otros clientes del mismo tenant."""
    user = request.user
    if user.tenant_id is not None and user.role == RolTenant.CLIENTE:
        if not user.cliente_id:
            return qs.none()
        return qs.filter(cliente_id=user.cliente_id)
    return scoped(qs, request, cliente_field='cliente_id')


def _es_staff(user):
    if user.tenant_id is None:
        return user.is_platform_staff
    return user.role in STAFF_ROLES


# ─── Helpers de serialización (dicts manuales, mismo estilo que contratos/views.py) ─
def _incidencia_list_dict(inc):
    return {
        'id': inc.id,
        'titulo': inc.titulo,
        'severidad': inc.severidad,
        'estado': inc.estado,
        'cliente_id': inc.cliente_id,
        'cliente_nombre': str(inc.cliente),
        'contrato_id': inc.contrato_id,
        'software_id': inc.software_id,
        'software_nombre': inc.software.nombre if inc.software_id else None,
        'reportado_por_id': inc.reportado_por_id,
        'asignado_a_id': inc.asignado_a_id,
        'asignado_a_nombre': inc.asignado_a.get_full_name() or inc.asignado_a.username if inc.asignado_a_id else None,
        'fecha_creacion': inc.fecha_creacion,
        'fecha_actualizacion': inc.fecha_actualizacion,
    }


def _adjunto_dict(adj):
    return {
        'id': adj.id,
        'nombre': adj.nombre,
        'archivo': adj.archivo.url if adj.archivo else None,
        'subido_por_id': adj.subido_por_id,
        'fecha_subida': adj.fecha_subida,
    }


def _comentario_dict(com):
    return {
        'id': com.id,
        'autor_id': com.autor_id,
        'autor_nombre': (com.autor.get_full_name() or com.autor.username) if com.autor_id else None,
        'mensaje': com.mensaje,
        'es_interno': com.es_interno,
        'fecha_creacion': com.fecha_creacion,
        'adjuntos': [_adjunto_dict(a) for a in com.adjuntos.all()],
    }


def _sla_block(inc):
    if not (inc.contrato_id and inc.contrato.sla_id):
        return None
    sla = inc.contrato.sla
    plazo_horas = sla.tiempo_respuesta_horas
    vencimiento = inc.fecha_creacion + timedelta(hours=plazo_horas)
    primera_respuesta = (
        inc.comentarios
        .filter(autor__role__in=STAFF_ROLES)
        .order_by('fecha_creacion')
        .first()
    )
    cumplido = bool(primera_respuesta and primera_respuesta.fecha_creacion <= vencimiento)
    en_riesgo = (not primera_respuesta) and timezone.now() > vencimiento and inc.estado == EstadoIncidencia.ABIERTO
    return {
        'plazo_horas': plazo_horas,
        'vencimiento': vencimiento,
        'cumplido': cumplido,
        'en_riesgo': en_riesgo,
    }


def _incidencia_detail_dict(inc, request):
    es_cliente = request.user.tenant_id is not None and request.user.role == RolTenant.CLIENTE
    comentarios_qs = inc.comentarios.select_related('autor').prefetch_related('adjuntos')
    if es_cliente:
        comentarios_qs = comentarios_qs.filter(es_interno=False)

    data = _incidencia_list_dict(inc)
    data.update({
        'descripcion': inc.descripcion,
        'fecha_resolucion': inc.fecha_resolucion,
        'adjuntos': [_adjunto_dict(a) for a in inc.adjuntos.filter(comentario__isnull=True)],
        'comentarios': [_comentario_dict(c) for c in comentarios_qs],
        'historial_estados': [
            {
                'estado_anterior': h.estado_anterior,
                'estado_nuevo': h.estado_nuevo,
                'usuario_id': h.usuario_id,
                'fecha_cambio': h.fecha_cambio,
            }
            for h in inc.historial_estados.all()
        ],
        'sla': _sla_block(inc),
    })
    return data


def _guardar_adjuntos(request, incidencia, comentario=None):
    archivos = request.FILES.getlist('adjuntos')
    for archivo in archivos:
        try:
            validar_adjunto_incidencia(archivo)
        except DjangoValidationError as e:
            raise ValidationErrorAdjunto(str(e.message if hasattr(e, 'message') else e))
        AdjuntoIncidencia.objects.create(
            incidencia=incidencia,
            comentario=comentario,
            archivo=archivo,
            nombre=archivo.name,
            subido_por=request.user,
        )


class ValidationErrorAdjunto(Exception):
    pass


# ─── Vistas ───────────────────────────────────────────────────────────────────
class IncidenciaListCreateView(APIView):
    """
    GET /api/incidencias/
    Lista paginada de incidencias, scoped por rol (CLIENTE ve solo las suyas,
    staff ve las de su tenant).

    POST /api/incidencias/
    Crea una incidencia. CLIENTE: siempre para su propio Cliente (ignora
    cualquier cliente_id del payload). Staff: debe indicar cliente_id.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('incidencias')) | IsPlatformClienteAccess]

    def get(self, request):
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page, page_size = 1, 20

        qs = _scope_incidencias(Incidencia.objects.select_related('cliente', 'software', 'asignado_a'), request)

        estado = request.query_params.get('estado')
        if estado:
            qs = qs.filter(estado=estado)
        severidad = request.query_params.get('severidad')
        if severidad:
            qs = qs.filter(severidad=severidad)
        contrato_id = request.query_params.get('contrato')
        if contrato_id:
            qs = qs.filter(contrato_id=contrato_id)
        asignado_a = request.query_params.get('asignado_a')
        if asignado_a:
            qs = qs.filter(asignado_a_id=asignado_a)
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(Q(titulo__icontains=search) | Q(descripcion__icontains=search))

        total = qs.count()
        offset = (page - 1) * page_size
        items = qs[offset: offset + page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, -(-total // page_size)),
            'results': [_incidencia_list_dict(i) for i in items],
        })

    def post(self, request):
        user = request.user
        titulo = (request.data.get('titulo') or '').strip()
        descripcion = (request.data.get('descripcion') or '').strip()
        if not titulo or not descripcion:
            return Response({'error': 'titulo y descripcion son requeridos'}, status=status.HTTP_400_BAD_REQUEST)

        severidad = request.data.get('severidad', SeveridadIncidencia.MEDIA)
        if severidad not in SeveridadIncidencia.values:
            return Response({'error': 'severidad inválida'}, status=status.HTTP_400_BAD_REQUEST)

        if user.tenant_id is not None and user.role == RolTenant.CLIENTE:
            if not user.cliente_id:
                return Response(
                    {'error': 'Tu cuenta no está vinculada a un Cliente. Contacta a soporte.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cliente = user.cliente
            tenant = user.tenant
        else:
            cliente_id = request.data.get('cliente_id')
            if not cliente_id:
                return Response({'error': 'cliente_id es requerido'}, status=status.HTTP_400_BAD_REQUEST)
            cliente = scoped(Cliente.objects.all(), request, cliente_field='pk').filter(pk=cliente_id).first()
            if not cliente:
                return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)
            tenant = cliente.tenant

        contrato = None
        contrato_id = request.data.get('contrato_id')
        if contrato_id:
            contrato = Contrato.objects.filter(pk=contrato_id, cliente_id=cliente.id).first()
            if not contrato:
                return Response({'error': 'Contrato no encontrado para este cliente'}, status=status.HTTP_400_BAD_REQUEST)

        software = None
        software_id = request.data.get('software_id')
        if software_id:
            software = Producto.objects.filter(pk=software_id, tenant=tenant).first()
            if not software:
                return Response({'error': 'Software no encontrado'}, status=status.HTTP_400_BAD_REQUEST)
        elif contrato:
            software = contrato.software

        incidencia = Incidencia.objects.create(
            tenant=tenant,
            cliente=cliente,
            contrato=contrato,
            software=software,
            titulo=titulo,
            descripcion=descripcion,
            severidad=severidad,
            reportado_por=user,
        )

        try:
            _guardar_adjuntos(request, incidencia)
        except ValidationErrorAdjunto as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(_incidencia_detail_dict(incidencia, request), status=status.HTTP_201_CREATED)


class IncidenciaDetailView(APIView):
    """
    GET /api/incidencias/<id>/  — detalle completo (comentarios, historial, adjuntos, SLA).
    PATCH /api/incidencias/<id>/ — cambiar estado/asignado_a (requiere CanManageIncidencia)
                                    o severidad (staff).
    """
    permission_classes = [(IsTenantMember & RequiresFeature('incidencias')) | IsPlatformClienteAccess]

    def get(self, request, pk):
        inc = get_object_or_404(_scope_incidencias(Incidencia.objects.all(), request), pk=pk)
        return Response(_incidencia_detail_dict(inc, request))

    def patch(self, request, pk):
        inc = get_object_or_404(_scope_incidencias(Incidencia.objects.all(), request), pk=pk)

        gestiona_algo = 'estado' in request.data or 'asignado_a_id' in request.data or 'severidad' in request.data
        if gestiona_algo and not _es_staff(request.user):
            return Response(
                {'error': 'Solo el staff interno puede gestionar el estado, asignación o severidad.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        if 'severidad' in request.data:
            severidad = request.data.get('severidad')
            if severidad not in SeveridadIncidencia.values:
                return Response({'error': 'severidad inválida'}, status=status.HTTP_400_BAD_REQUEST)
            inc.severidad = severidad
            inc.save(update_fields=['severidad', 'fecha_actualizacion'])

        if 'asignado_a_id' in request.data:
            asignado_a_id = request.data.get('asignado_a_id')
            if asignado_a_id:
                asignado = User.objects.filter(pk=asignado_a_id, tenant_id=inc.tenant_id).first()
                if not asignado:
                    return Response({'error': 'Usuario asignado no encontrado en este tenant'}, status=status.HTTP_400_BAD_REQUEST)
                inc.asignado_a = asignado
            else:
                inc.asignado_a = None
            inc.save(update_fields=['asignado_a', 'fecha_actualizacion'])

        if 'estado' in request.data:
            nuevo_estado = request.data.get('estado')
            if nuevo_estado not in EstadoIncidencia.values:
                return Response({'error': 'estado inválido'}, status=status.HTTP_400_BAD_REQUEST)
            inc.transicionar_estado(nuevo_estado, usuario=request.user)

        inc.refresh_from_db()
        return Response(_incidencia_detail_dict(inc, request))


class ComentarioListCreateView(APIView):
    """
    GET /api/incidencias/<pk>/comentarios/
    POST /api/incidencias/<pk>/comentarios/
    """
    permission_classes = [(IsTenantMember & RequiresFeature('incidencias')) | IsPlatformClienteAccess]

    def get(self, request, pk):
        inc = get_object_or_404(_scope_incidencias(Incidencia.objects.all(), request), pk=pk)
        es_cliente = request.user.tenant_id is not None and request.user.role == RolTenant.CLIENTE
        qs = inc.comentarios.select_related('autor').prefetch_related('adjuntos')
        if es_cliente:
            qs = qs.filter(es_interno=False)
        return Response([_comentario_dict(c) for c in qs])

    def post(self, request, pk):
        inc = get_object_or_404(_scope_incidencias(Incidencia.objects.all(), request), pk=pk)
        mensaje = (request.data.get('mensaje') or '').strip()
        if not mensaje:
            return Response({'error': 'mensaje es requerido'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        es_cliente = user.tenant_id is not None and user.role == RolTenant.CLIENTE
        es_interno = bool(request.data.get('es_interno')) and not es_cliente

        comentario = ComentarioIncidencia.objects.create(
            incidencia=inc,
            autor=user,
            mensaje=mensaje,
            es_interno=es_interno,
        )

        try:
            _guardar_adjuntos(request, inc, comentario=comentario)
        except ValidationErrorAdjunto as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if _es_staff(user) and inc.estado == EstadoIncidencia.ABIERTO:
            inc.transicionar_estado(EstadoIncidencia.EN_PROGRESO, usuario=user)

        return Response(_comentario_dict(comentario), status=status.HTTP_201_CREATED)


class IncidenciaStatsView(APIView):
    """
    GET /api/incidencias/stats/
    Solo para staff: agregados usados por el badge de Sidebar y el header de la bandeja.
    """
    permission_classes = [(IsTenantMember & RequiresFeature('incidencias')) | IsPlatformClienteAccess]

    def get(self, request):
        if not _es_staff(request.user) and not request.user.is_platform_staff:
            return Response({'error': 'Solo disponible para staff interno.'}, status=status.HTTP_403_FORBIDDEN)

        qs = _scope_incidencias(Incidencia.objects.all(), request)
        return Response({
            'total': qs.count(),
            'abiertas': qs.filter(estado=EstadoIncidencia.ABIERTO).count(),
            'en_progreso': qs.filter(estado=EstadoIncidencia.EN_PROGRESO).count(),
            'resueltas': qs.filter(estado=EstadoIncidencia.RESUELTO).count(),
            'cerradas': qs.filter(estado=EstadoIncidencia.CERRADO).count(),
            'criticas_abiertas': qs.filter(
                severidad=SeveridadIncidencia.CRITICA,
                estado__in=[EstadoIncidencia.ABIERTO, EstadoIncidencia.EN_PROGRESO],
            ).count(),
        })
