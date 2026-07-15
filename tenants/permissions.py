"""Capas de permisos multi-tenant para DRF.

Capa 1 (RBAC): IsTenantMember / IsSuperAdmin / DeleteRequiresTenantAdmin —
¿la identidad del usuario puede invocar este endpoint?
Capa 2 (ABAC): RequiresFeature — ¿el plan de su empresa incluye la función?

RequierePermiso unifica ambas capas sobre tenants/permisos.py (membresía ∩
rol) a nivel de acción; es el mecanismo preferido para endpoints nuevos, las
clases anteriores siguen vigentes para lo ya construido.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import EstadoTenant
from .permisos import permisos_efectivos
from .plans import features_for


class IsSuperAdmin(BasePermission):
    message = 'Requiere permisos de superadministrador de la plataforma.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user.is_authenticated and user.is_superadmin)


class IsTenantMember(BasePermission):
    """Usuario autenticado de un tenant, o superadmin global.

    Reglas transversales:
    - Tenant SUSPENDIDO ⇒ solo lectura para todos sus usuarios.
    - Rol AUDITOR ⇒ solo lectura siempre.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        if user.tenant_id is None:
            return user.is_superadmin
        if request.method in SAFE_METHODS:
            return True
        if user.tenant.estado == EstadoTenant.SUSPENDIDO:
            self.message = 'Cuenta suspendida: acceso de solo lectura hasta regularizar el servicio.'
            return False
        if user.is_auditor:
            self.message = 'El rol Auditor Legal es de solo lectura.'
            return False
        return True


class DeleteRequiresTenantAdmin(BasePermission):
    """DELETE reservado al Administrador de Cuenta (o superadmin)."""
    message = 'Solo el Administrador de Cuenta puede eliminar registros.'

    def has_permission(self, request, view):
        user = request.user
        if request.method != 'DELETE' or not user.is_authenticated:
            return True
        if user.tenant_id is None:
            return True
        return user.is_tenant_admin


def EditRequiresPermiso(modulo):
    """Factory: PUT/PATCH exigen '{modulo}.editar' en permisos_efectivos.

    Igual idea que DeleteRequiresTenantAdmin pero recortando por el permiso de
    edición del módulo en vez de por rol de tenant-admin fijo — así el rol
    CLIENTE (que no tiene ningún permiso 'clientes.*' ni 'contratos.editar' en
    tenants/permisos.py) no puede escribir sobre vistas cuyo queryset scoped()
    sí le deja leer/alcanzar (p. ej. su propio Cliente o Contrato).
    Deja pasar GET/HEAD/OPTIONS/DELETE/POST sin tocar (esos ya los gatean
    otras clases combinadas en permission_classes)."""

    class _EditRequiresPermiso(BasePermission):
        message = f"Tu membresía o rol no incluye: {modulo}.editar."

        def has_permission(self, request, view):
            if request.method not in ('PUT', 'PATCH'):
                return True
            user = request.user
            if not user.is_authenticated:
                return False
            if user.tenant_id is None:
                return True
            return f'{modulo}.editar' in permisos_efectivos(user)

    _EditRequiresPermiso.__name__ = f"EditRequiresPermiso_{modulo}"
    return _EditRequiresPermiso


class IsSuperAdminOrModerador(BasePermission):
    """Staff de plataforma con poder de gestión (no destructivo): SuperAdmin o
    Moderador. Usada en endpoints de administración de tenants/usuarios donde
    Moderador también puede operar (soporte), pero un usuario de tenant no."""
    message = 'Requiere permisos de Moderador o Super Administrador.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user.is_authenticated and (user.is_superadmin or user.is_moderador))


class TenantCreateRequiresSuperAdmin(BasePermission):
    """Crear tenants nuevos queda fuera del alcance de Moderador — es una
    decisión comercial, no de moderación. Solo restringe POST."""
    message = 'Solo el Super Administrador puede crear nuevos tenants.'

    def has_permission(self, request, view):
        if request.method != 'POST':
            return True
        return bool(request.user.is_authenticated and request.user.is_superadmin)


class IsPlatformClienteAccess(BasePermission):
    """Acceso de staff de plataforma a Cliente/Contrato. SuperAdmin y Moderador:
    total. Trabajador: solo lectura — el recorte a "solo lo concedido" lo aplica
    scoped(cliente_field=...) en la vista, esto solo decide el método HTTP."""
    message = 'Los Trabajadores tienen acceso de solo lectura.'

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated or user.tenant_id is not None:
            return False  # esta clase solo cubre staff de plataforma
        if user.is_superadmin or user.is_moderador:
            return True
        if user.is_trabajador:
            return request.method in SAFE_METHODS
        return False


def RequiresFeature(feature):
    """Factory de permiso ABAC: valida que el plan del tenant incluya la feature.

    Superadmin pasa siempre. Uso: permission_classes = [IsTenantMember, RequiresFeature('analytics')]
    """

    class _RequiresFeature(BasePermission):
        message = (f"Tu plan actual no incluye '{feature}'. "
                   "Requiere upgrade de categoría de suscripción.")

        def has_permission(self, request, view):
            user = request.user
            if not user.is_authenticated:
                return False
            if user.tenant_id is None:
                return True
            return feature in features_for(user.tenant)

    _RequiresFeature.__name__ = f"RequiresFeature_{feature}"
    return _RequiresFeature


def RequierePermiso(*permisos):
    """Factory de permiso unificado sobre tenants/permisos.py.

    Valida que el usuario tenga TODOS los permisos indicados según su
    membresía ∩ rol (o su rol de plataforma). Cubre en una sola clase lo que
    antes exigía combinar RBAC + RequiresFeature, y ya considera tenant
    SUSPENDIDO (solo lectura) vía permisos_efectivos.

    Uso: permission_classes = [RequierePermiso('contratos.crear')]
    """

    class _RequierePermiso(BasePermission):
        message = f"Tu membresía o rol no incluye: {', '.join(permisos)}."

        def has_permission(self, request, view):
            if not request.user.is_authenticated:
                return False
            efectivos = permisos_efectivos(request.user)
            return all(p in efectivos for p in permisos)

    _RequierePermiso.__name__ = f"RequierePermiso_{'_'.join(p.replace('.', '_') for p in permisos)}"
    return _RequierePermiso


# GET/HEAD/OPTIONS → ver; POST → crear; PUT/PATCH → editar; DELETE → eliminar.
_ACCION_POR_METODO = {
    'GET': 'ver', 'HEAD': 'ver', 'OPTIONS': 'ver',
    'POST': 'crear', 'PUT': 'editar', 'PATCH': 'editar', 'DELETE': 'eliminar',
}


def RequierePermisoModulo(modulo):
    """Variante CRUD: deduce la acción del método HTTP sobre un módulo del
    registro. Uso: permission_classes = [RequierePermisoModulo('contratos')]"""

    class _RequierePermisoModulo(BasePermission):
        def has_permission(self, request, view):
            if not request.user.is_authenticated:
                return False
            accion = _ACCION_POR_METODO.get(request.method)
            if accion is None:
                return False
            permiso = f'{modulo}.{accion}'
            self.message = f"Tu membresía o rol no incluye: {permiso}."
            return permiso in permisos_efectivos(request.user)

    _RequierePermisoModulo.__name__ = f"RequierePermisoModulo_{modulo}"
    return _RequierePermisoModulo
