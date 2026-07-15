"""Fuente única de verdad de permisos: membresías (planes) y roles.

Modelo de dos ejes, evaluado en cada petición:

1. Eje membresía (qué compró la empresa): PERMISOS_MEMBRESIA. El piso
   SIN_MEMBRESIA ya incluye la operación básica completa del CLM (clientes,
   contratos, catálogo, incidencias, usuarios — con cuotas chicas, ver
   plans.py). Cada categoría de pago SUMA BENEFICIOS sobre la anterior
   (acumulativo, nunca resta): módulos premium del CLM + ventajas de servicio
   (soporte, descargas developer, canal de sugerencias, etc.).
2. Eje rol (qué puede hacer cada persona): PERMISOS_ROL_TENANT recorta dentro
   del techo de la membresía.

   permisos_efectivos(user) = membresía(tenant) ∩ rol(user)

El staff de plataforma (tenant=None) no tiene membresía: sus permisos salen
solo de PERMISOS_ROL_PLATAFORMA.

Formato de permiso: 'modulo.accion' (p. ej. 'soporte.chat_24_7'). Vive en
código (git, no DB) igual que plans.py: cambios = deploy auditable, y un
upgrade de categoría surte efecto inmediato sin tocar usuarios ni sesiones.

Para añadir un beneficio nuevo: registrarlo en REGISTRO, decidir en qué
categoría de membresía entra y qué roles lo reciben. Nada más.
"""

from .models import CategoriaSuscripcion, EstadoTenant, RolPlataforma, RolTenant

# ---------------------------------------------------------------------------
# Registro: todas las acciones que existen en el sistema, por módulo.
# El prefijo (módulo) es la clave de feature que consumen RequiresFeature y el
# Sidebar del frontend; la acción habilita botones/endpoints puntuales.
# ---------------------------------------------------------------------------

REGISTRO = {
    # Operación básica del CLM (disponible desde SIN_MEMBRESIA).
    # catalogo/analytics/usuarios son SECCIONES DE ADMINISTRACIÓN: nunca se
    # otorgan al rol CLIENTE (portal externo) — su vitrina es 'softwares'.
    'clientes': {'ver', 'crear', 'editar', 'eliminar'},
    'contratos': {'ver', 'crear', 'editar', 'eliminar'},
    'catalogo': {'ver', 'crear', 'editar', 'eliminar'},
    'incidencias': {'ver', 'crear', 'gestionar'},
    'usuarios': {'ver', 'gestionar'},
    # Portal del cliente: ver la oferta de software de la empresa y solicitar
    # su contratación. Cara pública del catálogo — el CRUD admin es 'catalogo'.
    'softwares': {'ver', 'contratar'},
    # Módulos premium del CLM (se desbloquean por membresía)
    'plantillas': {'ver', 'crear', 'editar', 'eliminar', 'generar'},
    'requerimientos': {'ver', 'crear', 'editar', 'generar'},
    'documentos': {'exportar', 'importar'},
    'legal': {'ver', 'crear', 'editar', 'eliminar'},
    'analytics': {'ver'},
    'descarga_masiva': {'exportar'},
    # Beneficios de servicio (ventajas de membresía)
    'soporte': {
        'chat',        # chat directo con soporte, horario hábil
        'chat_24_7',   # canal websocket en tiempo real, 24/7
        'prioritario', # cola de atención prioritaria
        'dedicado',    # gestor de cuenta asignado
    },
    'sugerencias': {'canal'},       # canal de sugerencias de producto
    'descargas': {'developer'},     # versiones developer de los software del catálogo
    'alertas': {'renovacion'},      # alertas tempranas de renovación/vencimiento
    'reportes': {'programados'},    # reportes periódicos por correo
    'api': {'acceso'},              # API pública con token
}

# Módulos exclusivos del staff de plataforma: no forman parte de ninguna
# membresía ni aparecen como features de plan.
REGISTRO_PLATAFORMA = {
    'tenants': {'ver', 'crear', 'editar', 'eliminar'},
    'membresias': {'ver', 'gestionar'},
}


def _modulo(nombre, registro=None):
    """Todas las acciones de un módulo, como set de 'modulo.accion'."""
    acciones = (registro or REGISTRO)[nombre]
    return {f'{nombre}.{accion}' for accion in acciones}


def _todos(registro):
    permisos = set()
    for nombre in registro:
        permisos |= _modulo(nombre, registro)
    return frozenset(permisos)


TODOS = _todos(REGISTRO)
TODOS_PLATAFORMA = _todos(REGISTRO_PLATAFORMA)

# Acciones que no mutan datos del CLM: sobreviven a un rol de solo lectura.
_ACCIONES_LECTURA = {'ver', 'exportar'}


def _solo_lectura(permisos):
    return {p for p in permisos if p.rsplit('.', 1)[1] in _ACCIONES_LECTURA}


# ---------------------------------------------------------------------------
# Eje 1 — Membresías. Acumulativo: cada nivel = anterior + beneficios nuevos.
# ---------------------------------------------------------------------------

# Piso gratuito: operación básica completa del CLM. La diferenciación con las
# membresías de pago está en las cuotas (plans.py) y en los beneficios de abajo,
# no en recortar el CRUD básico.
_SIN_MEMBRESIA = (
    _modulo('clientes') | _modulo('contratos') | _modulo('catalogo')
    | _modulo('incidencias') | _modulo('usuarios') | _modulo('softwares')
)

_COBRE = _SIN_MEMBRESIA | {
    'sugerencias.canal',    # voz directa en el roadmap del producto
    'soporte.chat',         # soporte directo por chat en horario hábil
    'alertas.renovacion',   # avisos tempranos de contratos por vencer
}

_PLATA = _COBRE | _modulo('plantillas') | _modulo('requerimientos') | _modulo('documentos') | {
    'descargas.developer',  # builds developer de los software del catálogo
}

_PLATINO = _PLATA | _modulo('legal') | {
    'soporte.chat_24_7',    # chat websocket en tiempo real, 24/7
    'soporte.prioritario',
}

_DIAMANTE = _PLATINO | _modulo('analytics') | _modulo('descarga_masiva') | {
    'reportes.programados',
    'api.acceso',
}

_OBSIDIANA = _DIAMANTE | {
    'soporte.dedicado',     # gestor de cuenta asignado — exclusivo Obsidiana
}

PERMISOS_MEMBRESIA = {
    CategoriaSuscripcion.SIN_MEMBRESIA: frozenset(_SIN_MEMBRESIA),
    CategoriaSuscripcion.COBRE: frozenset(_COBRE),
    CategoriaSuscripcion.PLATA: frozenset(_PLATA),
    CategoriaSuscripcion.PLATINO: frozenset(_PLATINO),
    CategoriaSuscripcion.DIAMANTE: frozenset(_DIAMANTE),
    CategoriaSuscripcion.OBSIDIANA: frozenset(_OBSIDIANA),
}

# Un tenant SUSPENDIDO queda en solo lectura, pero conserva los canales de
# soporte que su membresía incluya: los necesita para regularizar el servicio.
_PERMISOS_SUSPENDIDO_EXTRA = frozenset(_modulo('soporte') | {'sugerencias.canal'})

# ---------------------------------------------------------------------------
# Eje 2 — Roles internos del tenant. Recortan dentro del techo de membresía.
# ---------------------------------------------------------------------------

PERMISOS_ROL_TENANT = {
    RolTenant.TENANT_ADMIN: TODOS,
    # Opera todo el día a día y goza de los beneficios, pero no borra ni
    # administra cuentas.
    RolTenant.OPERADOR: frozenset(
        p for p in TODOS
        if not p.endswith('.eliminar') and p != 'usuarios.gestionar'
    ),
    # Solo lectura del CLM + canales de soporte (puede necesitar ayuda).
    RolTenant.AUDITOR: frozenset(_solo_lectura(TODOS) | {'soporte.chat', 'soporte.chat_24_7'}),
    # Usuario externo (portal del cliente): su alcance de datos lo recorta
    # scoped(cliente_field=...); esto define qué acciones tiene disponibles.
    # NUNCA recibe secciones de administración (catalogo, analytics/monitoreo,
    # usuarios, clientes): ve SUS contratos, la vitrina de softwares para
    # contratar, reporta incidencias y goza las ventajas de la membresía.
    RolTenant.CLIENTE: frozenset({
        'contratos.ver',
        'softwares.ver', 'softwares.contratar',
        'incidencias.ver', 'incidencias.crear',
        'soporte.chat', 'soporte.chat_24_7',
        'sugerencias.canal',
        'descargas.developer',
    }),
}

# ---------------------------------------------------------------------------
# Staff de plataforma (tenant=None): sin membresía, el rol lo es todo.
# ---------------------------------------------------------------------------

PERMISOS_ROL_PLATAFORMA = {
    RolPlataforma.SUPERADMIN: TODOS | TODOS_PLATAFORMA,
    # Soporte: gestiona tenants/usuarios/clientes/contratos y atiende los
    # canales de chat/sugerencias, pero no crea tenants ni toca membresías
    # (decisión comercial, solo SuperAdmin).
    RolPlataforma.MODERADOR: frozenset(
        _modulo('clientes') | _modulo('contratos') | _modulo('usuarios')
        | _modulo('incidencias') | _modulo('soporte') | _modulo('sugerencias')
        | {'tenants.ver', 'tenants.editar', 'membresias.ver'}
    ),
    # Solo lectura de los Clientes concedidos vía ClienteGrant.
    RolPlataforma.TRABAJADOR: frozenset({'clientes.ver', 'contratos.ver'}),
}


# ---------------------------------------------------------------------------
# Resolución
# ---------------------------------------------------------------------------

def permisos_membresia(tenant):
    """Techo de permisos que habilita la categoría efectiva del tenant."""
    return PERMISOS_MEMBRESIA[tenant.categoria]


def permisos_efectivos(user):
    """Set de permisos reales del usuario en este instante.

    Tenant user: membresía ∩ rol, degradado a solo lectura (+ soporte) si el
    tenant está SUSPENDIDO. Staff de plataforma: matriz de su platform_role.
    Superusuario de bootstrap (createsuperuser sin platform_role): todo.
    """
    if not user.is_authenticated:
        return frozenset()
    if user.tenant_id is None:
        if user.is_superadmin:
            return PERMISOS_ROL_PLATAFORMA[RolPlataforma.SUPERADMIN]
        matriz = PERMISOS_ROL_PLATAFORMA.get(user.platform_role)
        return matriz if matriz is not None else frozenset()
    efectivos = permisos_membresia(user.tenant) & PERMISOS_ROL_TENANT.get(user.role, frozenset())
    if user.tenant.estado == EstadoTenant.SUSPENDIDO:
        return frozenset(_solo_lectura(efectivos) | (efectivos & _PERMISOS_SUSPENDIDO_EXTRA))
    return efectivos


def tiene_permiso(user, permiso):
    return permiso in permisos_efectivos(user)
