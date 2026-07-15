import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class CategoriaSuscripcion(models.TextChoices):
    # Piso gratuito: es la categoría efectiva de todo tenant sin una Membresia
    # ACTIVA vigente. No se asigna como membresía (no hay Membresia SIN_MEMBRESIA):
    # es el estado al que se vuelve al cancelar o expirar.
    SIN_MEMBRESIA = 'SIN_MEMBRESIA', 'Sin Membresía'
    COBRE = 'COBRE', 'Cobre'
    PLATA = 'PLATA', 'Plata'
    PLATINO = 'PLATINO', 'Platino'
    DIAMANTE = 'DIAMANTE', 'Diamante'
    OBSIDIANA = 'OBSIDIANA', 'Obsidiana'


class EstadoTenant(models.TextChoices):
    ACTIVO = 'ACTIVO', 'Activo'
    GRACIA = 'GRACIA', 'En Periodo de Gracia'
    SUSPENDIDO = 'SUSPENDIDO', 'Suspendido'


class RolTenant(models.TextChoices):
    TENANT_ADMIN = 'TENANT_ADMIN', 'Administrador de Cuenta'
    OPERADOR = 'OPERADOR', 'Operador'
    AUDITOR = 'AUDITOR', 'Auditor Legal'
    CLIENTE = 'CLIENTE', 'Cliente Externo'


class RolPlataforma(models.TextChoices):
    """Roles de staff de la plataforma (tenant=None) — planos distinto de RolTenant,
    que es interno a cada empresa cliente."""
    SUPERADMIN = 'SUPERADMIN', 'Super Administrador'
    MODERADOR = 'MODERADOR', 'Moderador'
    TRABAJADOR = 'TRABAJADOR', 'Trabajador'


class Tenant(models.Model):
    """Empresa cliente de la plataforma. Clave de aislamiento de todos los datos.

    La categoría de suscripción define el continente (permisos + cuotas, ver
    tenants/permisos.py y tenants/plans.py); los roles de sus usuarios definen
    el contenido.

    `categoria` es la categoría EFECTIVA, denormalizada desde la Membresia
    ACTIVA vigente (o SIN_MEMBRESIA si no hay ninguna). Las capas de permisos
    la leen en cada petición sin joins; se mantiene sincronizada vía
    tenants/membresias.py — nunca escribirla a mano fuera de ese módulo."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    razon_social = models.CharField(max_length=255, unique=True)
    categoria = models.CharField(max_length=20, choices=CategoriaSuscripcion.choices,
                                 default=CategoriaSuscripcion.SIN_MEMBRESIA)
    estado = models.CharField(max_length=20, choices=EstadoTenant.choices,
                              default=EstadoTenant.ACTIVO)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_tenant'
        ordering = ['razon_social']

    def __str__(self):
        return f"{self.razon_social} ({self.get_categoria_display()})"

    @property
    def membresia_activa(self):
        """Membresia ACTIVA y no vencida, o None. Consulta la DB: usarla en
        vistas de gestión, no en el hot-path de permisos (ahí está `categoria`)."""
        from django.utils import timezone
        return (self.membresias
                .filter(estado=EstadoMembresia.ACTIVA)
                .filter(models.Q(fecha_expiracion__isnull=True)
                        | models.Q(fecha_expiracion__gt=timezone.now()))
                .first())


class EstadoMembresia(models.TextChoices):
    ACTIVA = 'ACTIVA', 'Activa'
    EXPIRADA = 'EXPIRADA', 'Expirada'
    CANCELADA = 'CANCELADA', 'Cancelada'


class Membresia(models.Model):
    """Suscripción de un tenant a una categoría de pago, con vigencia.

    Historial completo: asignar una membresía nueva cancela la ACTIVA anterior
    (nunca se edita en el lugar), así queda auditable quién otorgó qué y cuándo.
    A lo sumo una ACTIVA por tenant (constraint). fecha_expiracion=None ⇒
    indefinida. El ciclo de vida vive en tenants/membresias.py."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='membresias')
    categoria = models.CharField(max_length=20, choices=CategoriaSuscripcion.choices)
    estado = models.CharField(max_length=20, choices=EstadoMembresia.choices,
                              default=EstadoMembresia.ACTIVA)
    fecha_inicio = models.DateTimeField()
    fecha_expiracion = models.DateTimeField(null=True, blank=True,
                                            help_text="Vacío = indefinida.")
    otorgada_por = models.ForeignKey('User', on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='+')
    notas = models.TextField(blank=True, default='')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants_membresia'
        ordering = ['-fecha_inicio']
        constraints = [
            models.UniqueConstraint(fields=['tenant'],
                                    condition=models.Q(estado='ACTIVA'),
                                    name='uniq_membresia_activa_por_tenant'),
            # SIN_MEMBRESIA es la ausencia de membresía, no una membresía.
            models.CheckConstraint(check=~models.Q(categoria='SIN_MEMBRESIA'),
                                   name='membresia_categoria_de_pago'),
        ]

    def __str__(self):
        return f"{self.tenant.razon_social}: {self.get_categoria_display()} ({self.get_estado_display()})"

    @property
    def vigente(self):
        from django.utils import timezone
        return (self.estado == EstadoMembresia.ACTIVA
                and (self.fecha_expiracion is None or self.fecha_expiracion > timezone.now()))


class User(AbstractUser):
    """Usuario de la plataforma. tenant=None ⇒ staff global (SuperAdmin/Moderador/
    Trabajador, ver platform_role); con tenant ⇒ empleado de esa empresa, con rol
    interno (RolTenant)."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='usuarios', db_column='tenant_id')
    role = models.CharField(max_length=20, choices=RolTenant.choices,
                            default=RolTenant.OPERADOR)
    platform_role = models.CharField(max_length=20, choices=RolPlataforma.choices,
                                     null=True, blank=True,
                                     help_text="Solo aplica cuando tenant es null (staff de la plataforma).")
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='usuarios_cuenta', db_column='cliente_id',
                                help_text="Solo aplica cuando role=CLIENTE: el Cliente al que pertenece "
                                          "este usuario externo, usado para scoping de incidencias/contratos.")

    class Meta:
        db_table = 'tenants_user'

    @property
    def is_superadmin(self):
        # is_superuser cubre el bootstrap vía `createsuperuser`; platform_role
        # cubre las cuentas de SuperAdmin creadas manualmente. No depende de
        # is_staff: ese flag solo habilita /admin/ y no debe implicar SuperAdmin
        # (p.ej. si algún día un Moderador necesitara acceso al admin).
        return self.tenant_id is None and (self.is_superuser or self.platform_role == RolPlataforma.SUPERADMIN)

    @property
    def is_moderador(self):
        return self.tenant_id is None and self.platform_role == RolPlataforma.MODERADOR

    @property
    def is_trabajador(self):
        return self.tenant_id is None and self.platform_role == RolPlataforma.TRABAJADOR

    @property
    def is_platform_staff(self):
        return self.is_superadmin or self.is_moderador or self.is_trabajador

    @property
    def is_tenant_admin(self):
        return self.tenant_id is not None and self.role == RolTenant.TENANT_ADMIN

    @property
    def is_auditor(self):
        return self.tenant_id is not None and self.role == RolTenant.AUDITOR


class ClienteGrant(models.Model):
    """Acceso de solo lectura de un Trabajador (staff global) a un Cliente puntual
    y sus contratos. Terreno nuevo: no hay scoping por tenant para estos usuarios
    (tenant=None) — el acceso se concede registro por registro, vía Django Admin."""
    id = models.BigAutoField(primary_key=True)
    trabajador = models.ForeignKey(User, on_delete=models.CASCADE, related_name='clientes_concedidos',
                                   limit_choices_to={'platform_role': RolPlataforma.TRABAJADOR})
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.CASCADE, related_name='grants_trabajador')
    otorgado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tenants_clientegrant'
        constraints = [
            models.UniqueConstraint(fields=['trabajador', 'cliente'], name='uniq_trabajador_cliente'),
        ]

    def __str__(self):
        return f"{self.trabajador.username} → {self.cliente_id}"
