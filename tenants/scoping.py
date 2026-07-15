"""Aislamiento por tenant y cuotas dinámicas.

El filtrado es explícito (scoped(qs, request)) en vez de un manager mágico con
thread-locals: cada view declara su scoping y el code review lo puede verificar.
El superadmin global (tenant=None) ve todo sin filtro.
"""

from django.apps import apps
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from .plans import quota_for


def scoped(qs, request, field='tenant', cliente_field=None):
    """Filtra el queryset al tenant del usuario. Superadmin/Moderador: sin filtro.

    field admite lookups para modelos hijos, p. ej. 'contrato__tenant'.

    Trabajador (staff global sin tenant, con acceso concedido a Clientes
    puntuales vía ClienteGrant): si la vista pasa cliente_field (lookup hacia
    el Cliente del row, p. ej. 'pk' en Cliente o 'cliente_id' en Contrato),
    se filtra a lo concedido. Si la vista no lo pasa, no ve nada — más seguro
    que dejar pasar sin filtro.

    Usuario-cliente (rol CLIENTE dentro del tenant): comparte tenant_id con el
    staff que lo atiende, así que el filtro por tenant no basta — se restringe
    a su propio Cliente vía cliente_field, con el mismo fail-safe que
    Trabajador cuando la vista no lo pasa."""
    user = request.user
    if getattr(user, 'is_trabajador', False):
        if cliente_field is None:
            return qs.none()
        from .models import ClienteGrant
        granted = ClienteGrant.objects.filter(trabajador=user).values_list('cliente_id', flat=True)
        return qs.filter(**{f'{cliente_field}__in': granted})
    from .models import RolTenant
    if user.tenant_id is not None and getattr(user, 'role', None) == RolTenant.CLIENTE:
        if cliente_field is None or user.cliente_id is None:
            return qs.none()
        return qs.filter(**{cliente_field: user.cliente_id})
    if user.tenant_id is None:
        return qs
    return qs.filter(**{field: user.tenant_id})


def resolve_tenant_for_write(request, data=None):
    """Tenant al que se estampa un registro nuevo.

    Usuario de tenant: siempre el suyo (ignora cualquier tenant_id del payload).
    Superadmin: debe indicar tenant_id explícito en el payload."""
    user = request.user
    if user.tenant_id is not None:
        return user.tenant

    tenant_id = (data or {}).get('tenant_id')
    Tenant = apps.get_model('tenants', 'Tenant')
    if not tenant_id:
        # Fallback para superadmin en entorno de desarrollo/pruebas
        tenant = Tenant.objects.first()
        if tenant:
            return tenant
        raise ValidationError({'tenant_id': 'Superadmin debe indicar tenant_id para crear registros.'})
    try:
        return Tenant.objects.get(pk=tenant_id)
    except (Tenant.DoesNotExist, ValueError):
        raise ValidationError({'tenant_id': 'Tenant inexistente.'})


class QuotaExceeded(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'quota_exceeded'


# recurso → (app_label, modelo, campo tenant) para la agregación .count()
_QUOTA_MODELS = {
    'contratos': ('contratos', 'Contrato', 'tenant'),
    'clientes': ('clientes', 'Cliente', 'tenant'),
    'usuarios': ('tenants', 'User', 'tenant'),
}


def enforce_quota(tenant, recurso):
    """Bloquea la escritura si el plan del tenant llegó al límite del recurso."""
    limite = quota_for(tenant, recurso)
    if limite is None:
        return
    app_label, model_name, field = _QUOTA_MODELS[recurso]
    Model = apps.get_model(app_label, model_name)
    actual = Model.objects.filter(**{field: tenant.pk}).count()
    if actual >= limite:
        raise QuotaExceeded(detail={
            'error': f"Límite de {recurso} alcanzado ({actual}/{limite}) para el plan {tenant.get_categoria_display()}.",
            'code': 'quota_exceeded',
            'recurso': recurso,
            'limite': limite,
            'upgrade_requerido': True,
        })


def require_tenant_admin(request):
    """Guard imperativo para acciones destructivas dentro del tenant."""
    user = request.user
    if user.tenant_id is None:
        return  # superadmin
    if not user.is_tenant_admin:
        raise PermissionDenied('Solo el Administrador de Cuenta puede realizar esta acción.')
