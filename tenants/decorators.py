"""Guards multi-tenant para vistas function-based (documentos/views.py).

Equivalentes FBV de las permission classes DRF (tenants/permissions.py):
mismo criterio, distinta forma de engancharse.
"""

from functools import wraps

from django.http import JsonResponse

from .models import EstadoTenant
from .plans import features_for


def require_feature(feature):
    """El plan del tenant debe incluir la feature. Superadmin pasa siempre.

    Moderador/Trabajador (tenant_id None, sin plan) NO pasan: import/export de
    documentos queda fuera de su alcance (Trabajador es solo lectura de Cliente/
    Contrato concedidos; Moderador no gestiona este módulo). Antes de este check
    tenant_id is None se trataba como "es superadmin", lo cual dejaba de ser
    cierto al introducir estos dos roles."""

    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if user.is_authenticated and not user.is_superadmin:
                if user.tenant_id is None:
                    return JsonResponse({'error': 'No tienes acceso a este módulo.'}, status=403)
                if feature not in features_for(user.tenant):
                    return JsonResponse({
                        'error': f"Tu plan actual no incluye '{feature}'. Requiere upgrade de categoría.",
                        'upgrade_requerido': True,
                    }, status=403)
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def require_tenant_write(view):
    """Bloquea escrituras del rol Auditor y de tenants suspendidos."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and user.tenant_id is not None:
            if user.tenant.estado == EstadoTenant.SUSPENDIDO:
                return JsonResponse(
                    {'error': 'Cuenta suspendida: acceso de solo lectura.'}, status=403)
            if user.is_auditor:
                return JsonResponse(
                    {'error': 'El rol Auditor Legal es de solo lectura.'}, status=403)
        return view(request, *args, **kwargs)
    return wrapper
