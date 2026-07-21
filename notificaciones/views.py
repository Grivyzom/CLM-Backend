from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from tenants.models import RolTenant
from tenants.permissions import IsPlatformClienteAccess, IsTenantMember
from tenants.scoping import scoped

from .models import Notificacion, TipoNotificacion

# Sin RequiresFeature('clientes'): el usuario rol CLIENTE debe poder leer sus
# notificaciones aunque 'clientes' sea una feature de gestión del plan. La
# escritura se restringe explícitamente en cada vista.
PERMISOS_NOTIFICACIONES = [IsTenantMember | IsPlatformClienteAccess]


def _es_usuario_cliente(request):
    return (request.user.tenant_id is not None
            and getattr(request.user, 'role', None) == RolTenant.CLIENTE)


def _scope_notificaciones(qs, request):
    """Mismo criterio que incidencias: el rol CLIENTE solo ve lo dirigido a su
    propio Cliente; el resto se acota con scoped() (tenant / grants / global)."""
    return scoped(qs, request, cliente_field='cliente_id')


def _serialize(n):
    return {
        'id': n.id,
        'cliente_id': n.cliente_id,
        'titulo': n.titulo,
        'cuerpo': n.cuerpo,
        'tipo': n.tipo,
        'creado_por': n.creado_por.username if n.creado_por else None,
        'fecha_creacion': n.fecha_creacion,
        'leida': n.leida,
        'leida_en': n.leida_en,
        'para_staff': n.para_staff,
        'enlace': n.enlace,
    }


class NotificacionListCreateView(APIView):
    """
    GET  /api/notificaciones/?cliente=<id>&solo_no_leidas=1&limit=<n>
    POST /api/notificaciones/  {cliente_id, titulo, cuerpo, tipo?}
    """
    permission_classes = PERMISOS_NOTIFICACIONES

    def get(self, request):
        qs = _scope_notificaciones(
            Notificacion.objects.select_related('creado_por'), request
        )
        cliente = request.query_params.get('cliente')
        if cliente:
            qs = qs.filter(cliente_id=cliente)
        
        if request.query_params.get('para_staff') in ('1', 'true'):
            qs = qs.filter(para_staff=True)
        else:
            qs = qs.filter(para_staff=False)
            
        if request.query_params.get('solo_no_leidas') in ('1', 'true'):
            qs = qs.filter(leida=False)
        try:
            limit = min(100, max(1, int(request.query_params.get('limit', 50))))
        except (ValueError, TypeError):
            limit = 50
        return Response({'results': [_serialize(n) for n in qs[:limit]]})

    def post(self, request):
        if _es_usuario_cliente(request):
            return Response({'error': 'No tienes permiso para crear notificaciones.'},
                            status=status.HTTP_403_FORBIDDEN)

        cliente_id = request.data.get('cliente_id')
        titulo = (request.data.get('titulo') or '').strip()
        cuerpo = (request.data.get('cuerpo') or '').strip()
        tipo = (request.data.get('tipo') or TipoNotificacion.INFO).strip().upper()

        if not cliente_id or not titulo or not cuerpo:
            return Response({'error': 'cliente_id, titulo y cuerpo son requeridos'},
                            status=status.HTTP_400_BAD_REQUEST)
        if tipo not in TipoNotificacion.values:
            return Response({'error': f'tipo debe ser uno de {TipoNotificacion.values}'},
                            status=status.HTTP_400_BAD_REQUEST)

        # El cliente destino debe estar dentro del alcance del emisor.
        from clientes.models import Cliente
        try:
            cliente = scoped(Cliente.objects.all(), request, cliente_field='pk').get(pk=cliente_id)
        except (Cliente.DoesNotExist, ValueError):
            return Response({'error': 'Cliente no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        notif = Notificacion.objects.create(
            tenant=cliente.tenant,
            cliente=cliente,
            titulo=titulo,
            cuerpo=cuerpo,
            tipo=tipo,
            creado_por=request.user,
        )
        return Response(_serialize(notif), status=status.HTTP_201_CREATED)


class NotificacionMarcarLeidaView(APIView):
    """POST /api/notificaciones/<pk>/leer/"""
    permission_classes = PERMISOS_NOTIFICACIONES

    def post(self, request, pk):
        try:
            notif = _scope_notificaciones(Notificacion.objects.all(), request).get(pk=pk)
        except Notificacion.DoesNotExist:
            return Response({'error': 'Notificación no encontrada'}, status=status.HTTP_404_NOT_FOUND)
        if not notif.leida:
            notif.leida = True
            notif.leida_en = timezone.now()
            notif.save(update_fields=['leida', 'leida_en'])
        return Response(_serialize(notif))


class NotificacionLeerTodasView(APIView):
    """POST /api/notificaciones/leer-todas/ — marca leídas todas las del alcance."""
    permission_classes = PERMISOS_NOTIFICACIONES

    def post(self, request):
        qs = _scope_notificaciones(Notificacion.objects.filter(leida=False), request)
        if request.query_params.get('para_staff') in ('1', 'true'):
            qs = qs.filter(para_staff=True)
        else:
            qs = qs.filter(para_staff=False)
        
        actualizadas = qs.update(leida=True, leida_en=timezone.now())
        return Response({'actualizadas': actualizadas})


class NotificacionUnreadCountView(APIView):
    """GET /api/notificaciones/unread-count/ → {count} para la campana."""
    permission_classes = PERMISOS_NOTIFICACIONES

    def get(self, request):
        qs = _scope_notificaciones(Notificacion.objects.filter(leida=False), request)
        if request.query_params.get('para_staff') in ('1', 'true'):
            qs = qs.filter(para_staff=True)
        else:
            qs = qs.filter(para_staff=False)
        count = qs.count()
        return Response({'count': count})
