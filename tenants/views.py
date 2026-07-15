from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .membresias import asignar_membresia, cancelar_membresia
from .models import CategoriaSuscripcion, EstadoTenant, RolTenant, Tenant, User
from .permisos import tiene_permiso
from .permissions import (
    IsSuperAdmin, IsSuperAdminOrModerador, IsTenantMember, RequierePermiso,
    TenantCreateRequiresSuperAdmin,
)
from .plans import plan_payload
from .scoping import enforce_quota


def _serialize_tenant(tenant, include_stats=False):
    data = {
        'id': str(tenant.id),
        'razon_social': tenant.razon_social,
        'categoria': tenant.categoria,
        'estado': tenant.estado,
        'fecha_creacion': tenant.fecha_creacion.isoformat(),
        'plan': plan_payload(tenant),
    }
    if include_stats:
        data['total_usuarios'] = tenant.usuarios.count()
    return data


def _serialize_membresia(membresia):
    return {
        'id': membresia.id,
        'categoria': membresia.categoria,
        'estado': membresia.estado,
        'vigente': membresia.vigente,
        'fecha_inicio': membresia.fecha_inicio.isoformat(),
        'fecha_expiracion': membresia.fecha_expiracion.isoformat() if membresia.fecha_expiracion else None,
        'otorgada_por': membresia.otorgada_por.username if membresia.otorgada_por else None,
        'notas': membresia.notas,
    }


def _serialize_user(user):
    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'role': user.role,
        'is_active': user.is_active,
        'tenant_id': str(user.tenant_id) if user.tenant_id else None,
    }


class TenantListCreateView(APIView):
    """Nivel global: SuperAdmin y Moderador listan; crear tenants queda
    reservado a SuperAdmin (decisión comercial, no de moderación)."""
    permission_classes = [IsSuperAdminOrModerador, TenantCreateRequiresSuperAdmin]

    def get(self, request):
        tenants = Tenant.objects.all()
        return Response([_serialize_tenant(t, include_stats=True) for t in tenants])

    def post(self, request):
        razon_social = (request.data.get('razon_social') or '').strip()
        if not razon_social:
            return Response({'error': 'razon_social es obligatoria'}, status=status.HTTP_400_BAD_REQUEST)
        if Tenant.objects.filter(razon_social__iexact=razon_social).exists():
            return Response({'error': 'Ya existe un tenant con esa razón social'}, status=status.HTTP_400_BAD_REQUEST)

        categoria = request.data.get('categoria', CategoriaSuscripcion.SIN_MEMBRESIA)
        if categoria not in CategoriaSuscripcion.values:
            return Response({'error': f'Categoría inválida. Opciones: {CategoriaSuscripcion.values}'},
                            status=status.HTTP_400_BAD_REQUEST)

        # El tenant nace SIN_MEMBRESIA; una categoría de pago se materializa
        # como Membresia (queda historial de quién la otorgó).
        with transaction.atomic():
            tenant = Tenant.objects.create(razon_social=razon_social)
            if categoria != CategoriaSuscripcion.SIN_MEMBRESIA:
                asignar_membresia(tenant, categoria, otorgada_por=request.user)
        return Response(_serialize_tenant(tenant), status=status.HTTP_201_CREATED)


class TenantDetailView(APIView):
    """SuperAdmin: ver/editar categoría (upgrade/downgrade), razón social y estado.
    Moderador: mismo acceso salvo categoría/razón social (comercial/identidad) —
    ver check en patch()."""
    permission_classes = [IsSuperAdminOrModerador]

    def get_object(self, pk):
        try:
            return Tenant.objects.get(pk=pk)
        except (Tenant.DoesNotExist, ValueError):
            return None

    def get(self, request, pk):
        tenant = self.get_object(pk)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_tenant(tenant, include_stats=True))

    def patch(self, request, pk):
        tenant = self.get_object(pk)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        # Categoría (plan/billing) y razón social (identidad legal) quedan
        # reservadas a SuperAdmin; Moderador puede activar/suspender (estado),
        # que es la acción de moderación propiamente dicha.
        if ('categoria' in request.data or 'razon_social' in request.data) and not request.user.is_superadmin:
            return Response(
                {'error': 'Solo el Super Administrador puede cambiar la categoría o razón social del tenant.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        categoria = request.data.get('categoria')
        if categoria is not None:
            if categoria not in CategoriaSuscripcion.values:
                return Response({'error': f'Categoría inválida. Opciones: {CategoriaSuscripcion.values}'},
                                status=status.HTTP_400_BAD_REQUEST)
            # El cambio de categoría pasa por el ciclo de membresías para que
            # quede historial; upgrade/downgrade indefinido (sin expiración).
            if categoria == CategoriaSuscripcion.SIN_MEMBRESIA:
                cancelar_membresia(tenant)
            elif categoria != tenant.categoria:
                asignar_membresia(tenant, categoria, otorgada_por=request.user)

        estado = request.data.get('estado')
        if estado is not None:
            if estado not in EstadoTenant.values:
                return Response({'error': f'Estado inválido. Opciones: {EstadoTenant.values}'},
                                status=status.HTTP_400_BAD_REQUEST)
            tenant.estado = estado

        razon_social = request.data.get('razon_social')
        if razon_social:
            tenant.razon_social = razon_social.strip()

        tenant.save()
        # Las capas de permisos leen categoría/estado en cada petición: el
        # upgrade/downgrade aplica al instante para todos los usuarios del tenant.
        return Response(_serialize_tenant(tenant))


class TenantUserListCreateView(APIView):
    """Nivel tenant: el Tenant Admin gestiona los usuarios de su propia empresa.

    Superadmin y Moderador pueden operar sobre cualquier tenant pasando ?tenant=<uuid>
    (soporte)."""
    permission_classes = [IsTenantMember | IsSuperAdminOrModerador]

    def _target_tenant(self, request):
        if request.user.tenant_id is not None:
            return request.user.tenant
        tenant_id = request.query_params.get('tenant') or request.data.get('tenant_id')
        if not tenant_id:
            return None
        try:
            return Tenant.objects.get(pk=tenant_id)
        except (Tenant.DoesNotExist, ValueError):
            return None

    def get(self, request):
        tenant = self._target_tenant(request)
        if not tenant:
            return Response({'error': 'Indica ?tenant=<uuid>'}, status=status.HTTP_400_BAD_REQUEST)
        usuarios = User.objects.filter(tenant=tenant).order_by('username')
        return Response([_serialize_user(u) for u in usuarios])

    def post(self, request):
        user = request.user
        if user.tenant_id is not None and not user.is_tenant_admin:
            return Response({'error': 'Solo el Administrador de Cuenta puede crear usuarios'},
                            status=status.HTTP_403_FORBIDDEN)

        tenant = self._target_tenant(request)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_400_BAD_REQUEST)

        enforce_quota(tenant, 'usuarios')

        username = (request.data.get('username') or '').strip()
        password = request.data.get('password')
        role = request.data.get('role', RolTenant.OPERADOR)

        if not username or not password:
            return Response({'error': 'username y password son obligatorios'}, status=status.HTTP_400_BAD_REQUEST)
        if role not in RolTenant.values:
            return Response({'error': f'Rol inválido. Opciones: {RolTenant.values}'},
                            status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({'error': 'El nombre de usuario ya existe'}, status=status.HTTP_400_BAD_REQUEST)

        nuevo = User.objects.create_user(
            username=username,
            password=password,
            email=request.data.get('email', ''),
            first_name=request.data.get('first_name', ''),
            last_name=request.data.get('last_name', ''),
            tenant=tenant,
            role=role,
        )
        return Response(_serialize_user(nuevo), status=status.HTTP_201_CREATED)


class TenantUserDetailView(APIView):
    permission_classes = [IsTenantMember | IsSuperAdminOrModerador]

    def _get_target(self, request, pk):
        """Usuario objetivo, siempre dentro del alcance del solicitante."""
        qs = User.objects.filter(pk=pk, tenant__isnull=False)
        if request.user.tenant_id is not None:
            qs = qs.filter(tenant_id=request.user.tenant_id)
        return qs.first()

    def patch(self, request, pk):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            return Response({'error': 'Solo el Administrador de Cuenta puede editar usuarios'},
                            status=status.HTTP_403_FORBIDDEN)
        target = self._get_target(request, pk)
        if not target:
            return Response({'error': 'Usuario no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        role = request.data.get('role')
        if role is not None:
            if role not in RolTenant.values:
                return Response({'error': f'Rol inválido. Opciones: {RolTenant.values}'},
                                status=status.HTTP_400_BAD_REQUEST)
            target.role = role

        if 'is_active' in request.data:
            target.is_active = bool(request.data['is_active'])
        for field in ('email', 'first_name', 'last_name'):
            if field in request.data:
                setattr(target, field, request.data[field])

        password = request.data.get('password')
        if password:
            target.set_password(password)

        target.save()
        return Response(_serialize_user(target))

    def delete(self, request, pk):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            return Response({'error': 'Solo el Administrador de Cuenta puede eliminar usuarios'},
                            status=status.HTTP_403_FORBIDDEN)
        target = self._get_target(request, pk)
        if not target:
            return Response({'error': 'Usuario no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        if target.pk == request.user.pk:
            return Response({'error': 'No puedes eliminar tu propia cuenta'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TenantMembresiaView(APIView):
    """Ciclo de vida de la membresía de un tenant.

    GET: historial completo (Moderador puede ver — permiso membresias.ver).
    POST: asignar/renovar membresía de pago (solo membresias.gestionar, es
    decir SuperAdmin): {categoria, fecha_expiracion?, notas?}.
    DELETE: cancelar la ACTIVA y degradar a SIN_MEMBRESIA.
    """
    permission_classes = [RequierePermiso('membresias.ver')]

    def _get_tenant(self, pk):
        try:
            return Tenant.objects.get(pk=pk)
        except (Tenant.DoesNotExist, ValueError):
            return None

    def get(self, request, pk):
        tenant = self._get_tenant(pk)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'tenant_id': str(tenant.id),
            'categoria_efectiva': tenant.categoria,
            'membresias': [_serialize_membresia(m) for m in tenant.membresias.all()],
        })

    def post(self, request, pk):
        if not tiene_permiso(request.user, 'membresias.gestionar'):
            return Response({'error': 'Solo el Super Administrador puede gestionar membresías.'},
                            status=status.HTTP_403_FORBIDDEN)
        tenant = self._get_tenant(pk)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        fecha_expiracion = None
        raw = request.data.get('fecha_expiracion')
        if raw:
            fecha_expiracion = parse_datetime(str(raw))
            if fecha_expiracion is None:
                return Response({'error': 'fecha_expiracion inválida (usa ISO 8601).'},
                                status=status.HTTP_400_BAD_REQUEST)
            if timezone.is_naive(fecha_expiracion):
                fecha_expiracion = timezone.make_aware(fecha_expiracion)

        membresia = asignar_membresia(
            tenant,
            request.data.get('categoria'),
            otorgada_por=request.user,
            fecha_expiracion=fecha_expiracion,
            notas=(request.data.get('notas') or '').strip(),
        )
        return Response({
            'membresia': _serialize_membresia(membresia),
            'tenant': _serialize_tenant(tenant),
        }, status=status.HTTP_201_CREATED)

    def delete(self, request, pk):
        if not tiene_permiso(request.user, 'membresias.gestionar'):
            return Response({'error': 'Solo el Super Administrador puede gestionar membresías.'},
                            status=status.HTTP_403_FORBIDDEN)
        tenant = self._get_tenant(pk)
        if not tenant:
            return Response({'error': 'Tenant no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        canceladas = cancelar_membresia(tenant)
        if not canceladas:
            return Response({'error': 'El tenant no tiene membresía activa.'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(_serialize_tenant(tenant))
