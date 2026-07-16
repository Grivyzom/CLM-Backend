import uuid

from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.views import enviar_correo_reset_password

from .membresias import asignar_membresia, cancelar_membresia
from .models import CategoriaSuscripcion, EstadoTenant, RolPlataforma, RolTenant, Tenant, User
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


def _serialize_platform_user(user):
    """Vista global de usuarios (cross-tenant): incluye de dónde viene la
    cuenta (staff de plataforma / empresa / cliente externo) para que
    /usuarios pueda mostrar y filtrar sin joins adicionales en el frontend."""
    if user.tenant_id is None:
        tipo_cuenta = 'PLATAFORMA'
    elif user.role == RolTenant.CLIENTE:
        tipo_cuenta = 'CLIENTE'
    else:
        tipo_cuenta = 'EMPRESA'

    data = _serialize_user(user)
    data.update({
        'platform_role': user.platform_role,
        'tenant_razon_social': user.tenant.razon_social if user.tenant_id else None,
        'cliente_id': user.cliente_id,
        'cliente_nombre': str(user.cliente) if user.cliente_id else None,
        'tipo_cuenta': tipo_cuenta,
        'date_joined': user.date_joined.isoformat(),
        'last_login': user.last_login.isoformat() if user.last_login else None,
    })
    return data


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


def _es_par_o_superior_plataforma(user):
    """SUPERADMIN/MODERADOR de plataforma: gestionarlos queda reservado a
    SuperAdmin (Moderador no gestiona pares ni superiores). TRABAJADOR y
    cuentas de tenant/cliente sí quedan al alcance de Moderador."""
    return user.tenant_id is None and user.platform_role in (RolPlataforma.SUPERADMIN, RolPlataforma.MODERADOR)


class PlatformUserListView(APIView):
    """Nivel plataforma: TODAS las cuentas (staff global + usuarios de
    cualquier tenant + clientes externos), sin scoping por tenant — a
    diferencia de TenantUserListCreateView, que exige ?tenant=<uuid>."""
    permission_classes = [IsSuperAdminOrModerador]

    def get(self, request):
        qs = User.objects.select_related('tenant', 'cliente').all()

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                models.Q(username__icontains=search)
                | models.Q(email__icontains=search)
                | models.Q(first_name__icontains=search)
                | models.Q(last_name__icontains=search)
                | models.Q(tenant__razon_social__icontains=search)
            )

        tipo_cuenta = request.query_params.get('tipo_cuenta', 'TODOS').upper()
        if tipo_cuenta == 'PLATAFORMA':
            qs = qs.filter(tenant__isnull=True)
        elif tipo_cuenta == 'CLIENTE':
            qs = qs.filter(tenant__isnull=False, role=RolTenant.CLIENTE)
        elif tipo_cuenta == 'EMPRESA':
            qs = qs.filter(tenant__isnull=False).exclude(role=RolTenant.CLIENTE)

        estado = request.query_params.get('estado', 'TODOS').upper()
        if estado == 'ACTIVO':
            qs = qs.filter(is_active=True)
        elif estado == 'INACTIVO':
            qs = qs.filter(is_active=False)

        tenant_id = request.query_params.get('tenant_id', '').strip()
        if tenant_id:
            try:
                qs = qs.filter(tenant_id=uuid.UUID(tenant_id))
            except ValueError:
                qs = qs.none()

        ordering = request.query_params.get('ordering', 'username')
        if ordering.lstrip('-') not in ('username', 'date_joined', 'last_login'):
            ordering = 'username'
        qs = qs.order_by(ordering)

        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page, page_size = 1, 20
        offset = (page - 1) * page_size

        total = qs.count()
        stats = {
            'total': total,
            'activos': qs.filter(is_active=True).count(),
            'inactivos': qs.filter(is_active=False).count(),
            'plataforma': qs.filter(tenant__isnull=True).count(),
            'empresa': qs.filter(tenant__isnull=False).exclude(role=RolTenant.CLIENTE).count(),
            'cliente': qs.filter(tenant__isnull=False, role=RolTenant.CLIENTE).count(),
        }

        page_items = qs[offset: offset + page_size]

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, -(-total // page_size)),
            'stats': stats,
            'results': [_serialize_platform_user(u) for u in page_items],
        })


class PlatformUserDetailView(APIView):
    """Edición/eliminación de cualquier cuenta desde la vista global. Ver
    _es_par_o_superior_plataforma para el candado de Moderador sobre pares."""
    permission_classes = [IsSuperAdminOrModerador]

    def get_object(self, pk):
        try:
            return User.objects.select_related('tenant', 'cliente').get(pk=pk)
        except (User.DoesNotExist, ValueError):
            return None

    def patch(self, request, pk):
        target = self.get_object(pk)
        if not target:
            return Response({'error': 'Usuario no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        if _es_par_o_superior_plataforma(target) and not request.user.is_superadmin:
            return Response(
                {'error': 'Solo el Super Administrador puede gestionar cuentas de Moderador o Super Administrador.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        es_uno_mismo = target.pk == request.user.pk

        if 'platform_role' in request.data:
            if not request.user.is_superadmin:
                return Response({'error': 'Solo el Super Administrador puede asignar roles de plataforma.'},
                                status=status.HTTP_403_FORBIDDEN)
            if target.tenant_id is not None:
                return Response({'error': 'platform_role solo aplica a cuentas sin empresa asociada.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if es_uno_mismo:
                return Response({'error': 'No puedes cambiar tu propio rol de plataforma.'},
                                status=status.HTTP_400_BAD_REQUEST)
            platform_role = request.data['platform_role'] or None
            if platform_role is not None and platform_role not in RolPlataforma.values:
                return Response({'error': f'Rol de plataforma inválido. Opciones: {RolPlataforma.values}'},
                                status=status.HTTP_400_BAD_REQUEST)
            target.platform_role = platform_role

        if 'role' in request.data:
            if target.tenant_id is None:
                return Response({'error': 'role solo aplica a cuentas de una empresa.'},
                                status=status.HTTP_400_BAD_REQUEST)
            role = request.data['role']
            if role not in RolTenant.values:
                return Response({'error': f'Rol inválido. Opciones: {RolTenant.values}'},
                                status=status.HTTP_400_BAD_REQUEST)
            target.role = role

        if 'is_active' in request.data:
            is_active = bool(request.data['is_active'])
            if es_uno_mismo and not is_active:
                return Response({'error': 'No puedes desactivar tu propia cuenta.'},
                                status=status.HTTP_400_BAD_REQUEST)
            target.is_active = is_active

        for field in ('email', 'first_name', 'last_name'):
            if field in request.data:
                setattr(target, field, request.data[field])

        password = request.data.get('password')
        if password:
            target.set_password(password)

        target.save()
        return Response(_serialize_platform_user(target))

    def delete(self, request, pk):
        target = self.get_object(pk)
        if not target:
            return Response({'error': 'Usuario no encontrado'}, status=status.HTTP_404_NOT_FOUND)
        if target.pk == request.user.pk:
            return Response({'error': 'No puedes eliminar tu propia cuenta'}, status=status.HTTP_400_BAD_REQUEST)
        if _es_par_o_superior_plataforma(target) and not request.user.is_superadmin:
            return Response(
                {'error': 'Solo el Super Administrador puede eliminar cuentas de Moderador o Super Administrador.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        with transaction.atomic():
            target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlatformUserResetPasswordView(APIView):
    """Acción de Administrador/Moderador: envía al usuario el mismo correo de
    'restablecer contraseña' que dispara /recuperar (mismo token de un solo
    uso), sin pedirle el correo/usuario — el admin ya sabe que la cuenta
    existe, así que no aplica el mensaje anti-enumeración de esa vista."""
    permission_classes = [IsSuperAdminOrModerador]

    def post(self, request, pk):
        try:
            target = User.objects.get(pk=pk)
        except (User.DoesNotExist, ValueError):
            return Response({'error': 'Usuario no encontrado'}, status=status.HTTP_404_NOT_FOUND)

        if _es_par_o_superior_plataforma(target) and not request.user.is_superadmin:
            return Response(
                {'error': 'Solo el Super Administrador puede gestionar cuentas de Moderador o Super Administrador.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not target.is_active:
            return Response({'error': 'La cuenta está desactivada.'}, status=status.HTTP_400_BAD_REQUEST)
        if not target.email:
            return Response({'error': 'El usuario no tiene un correo asociado.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            enviar_correo_reset_password(target)
        except Exception:
            return Response({'error': 'No se pudo enviar el correo. Intenta nuevamente.'},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response({'success': f'Enviamos un enlace de restablecimiento a {target.email}.'})


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
