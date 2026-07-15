"""Matriz de capacidades por categoría de suscripción.

Las FEATURES (módulos visibles) se DERIVAN de la matriz de permisos de
tenants/permisos.py — fuente única de verdad: un módulo está habilitado para
un plan si la membresía otorga al menos una acción de ese módulo. Acá solo se
declaran las CUOTAS numéricas.

Vive en código (versionada en git, cambios = deploy auditable); la DB solo
guarda la categoría efectiva del Tenant (sincronizada desde Membresia, ver
tenants/membresias.py). Las capas de permisos y cuotas consultan esta matriz
en cada petición, por lo que un upgrade/downgrade surte efecto inmediato sin
tocar usuarios ni sesiones.

Cuota None = ilimitado.
"""

from .models import CategoriaSuscripcion
from .permisos import PERMISOS_MEMBRESIA

_QUOTAS = {
    CategoriaSuscripcion.SIN_MEMBRESIA: {'contratos': 5, 'clientes': 10, 'usuarios': 1},
    CategoriaSuscripcion.COBRE: {'contratos': 20, 'clientes': 25, 'usuarios': 3},
    CategoriaSuscripcion.PLATA: {'contratos': 100, 'clientes': 250, 'usuarios': 10},
    CategoriaSuscripcion.PLATINO: {'contratos': 500, 'clientes': 1000, 'usuarios': 25},
    CategoriaSuscripcion.DIAMANTE: {'contratos': None, 'clientes': None, 'usuarios': 100},
    CategoriaSuscripcion.OBSIDIANA: {'contratos': None, 'clientes': None, 'usuarios': None},
}


def _features(permisos):
    """Módulos con al menos una acción habilitada."""
    return frozenset(p.split('.', 1)[0] for p in permisos)


PLAN_FEATURES = {
    categoria: {
        'features': _features(PERMISOS_MEMBRESIA[categoria]),
        'quotas': _QUOTAS[categoria],
    }
    for categoria in CategoriaSuscripcion.values
}


def features_for(tenant):
    """Set de features habilitadas para el plan del tenant."""
    return PLAN_FEATURES[tenant.categoria]['features']


def quota_for(tenant, recurso):
    """Límite numérico del recurso para el plan del tenant (None = ilimitado)."""
    return PLAN_FEATURES[tenant.categoria]['quotas'].get(recurso)


def plan_payload(tenant):
    """Representación serializable del plan, para auth/me y el frontend.

    `permisos` es el techo que otorga la membresía (eje empresa); los permisos
    efectivos del usuario (techo ∩ rol) van aparte en el payload de auth."""
    plan = PLAN_FEATURES[tenant.categoria]
    return {
        'categoria': tenant.categoria,
        'features': sorted(plan['features']),
        'quotas': plan['quotas'],
        'permisos': sorted(PERMISOS_MEMBRESIA[tenant.categoria]),
    }
