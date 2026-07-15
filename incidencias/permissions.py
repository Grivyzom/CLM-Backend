from rest_framework.permissions import BasePermission

from tenants.models import RolTenant


class CanManageIncidencia(BasePermission):
    """Cambiar estado / asignar: solo staff interno del tenant (TENANT_ADMIN,
    OPERADOR) o staff de plataforma con poder de gestión. CLIENTE y AUDITOR
    nunca pueden gestionar, solo reportar/comentar."""
    message = 'Solo el staff interno puede gestionar el estado o la asignación de una incidencia.'

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        if user.tenant_id is None:
            return user.is_superadmin or user.is_moderador
        return user.role in (RolTenant.TENANT_ADMIN, RolTenant.OPERADOR)
