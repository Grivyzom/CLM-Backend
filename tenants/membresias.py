"""Ciclo de vida de membresías — único módulo que escribe Tenant.categoria.

Reglas:
- Asignar nueva membresía cancela la ACTIVA anterior (historial inmutable).
- Tenant.categoria = categoría de la ACTIVA vigente, o SIN_MEMBRESIA.
- La expiración la aplica `expirar_vencidas()` (comando expirar_membresias,
  pensado para cron diario); mientras no corra, la categoría denormalizada
  sigue vigente — mismo trade-off que el resto del sistema (DB como verdad,
  sincronización explícita y auditable, sin señales mágicas).
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import CategoriaSuscripcion, EstadoMembresia, Membresia


def asignar_membresia(tenant, categoria, otorgada_por=None, fecha_expiracion=None, notas=''):
    """Activa una membresía de pago para el tenant (upgrade, downgrade o
    renovación: siempre un registro nuevo). Devuelve la Membresia creada."""
    if categoria == CategoriaSuscripcion.SIN_MEMBRESIA:
        raise ValidationError({'categoria': 'SIN_MEMBRESIA no se asigna: usa cancelar_membresia.'})
    if categoria not in CategoriaSuscripcion.values:
        raise ValidationError({'categoria': f'Categoría inválida. Opciones: {CategoriaSuscripcion.values}'})
    if fecha_expiracion is not None and fecha_expiracion <= timezone.now():
        raise ValidationError({'fecha_expiracion': 'La fecha de expiración debe ser futura.'})

    with transaction.atomic():
        tenant.membresias.filter(estado=EstadoMembresia.ACTIVA).update(
            estado=EstadoMembresia.CANCELADA, fecha_modificacion=timezone.now())
        membresia = Membresia.objects.create(
            tenant=tenant,
            categoria=categoria,
            fecha_inicio=timezone.now(),
            fecha_expiracion=fecha_expiracion,
            otorgada_por=otorgada_por,
            notas=notas,
        )
        tenant.categoria = categoria
        tenant.save(update_fields=['categoria', 'fecha_modificacion'])
    return membresia


def cancelar_membresia(tenant):
    """Cancela la membresía ACTIVA (si existe) y degrada a SIN_MEMBRESIA.
    Devuelve cuántas membresías se cancelaron (0 o 1)."""
    with transaction.atomic():
        canceladas = tenant.membresias.filter(estado=EstadoMembresia.ACTIVA).update(
            estado=EstadoMembresia.CANCELADA, fecha_modificacion=timezone.now())
        tenant.categoria = CategoriaSuscripcion.SIN_MEMBRESIA
        tenant.save(update_fields=['categoria', 'fecha_modificacion'])
    return canceladas


def sincronizar_categoria(tenant):
    """Recalcula Tenant.categoria desde la membresía ACTIVA vigente. Red de
    seguridad para inconsistencias (ediciones manuales en el admin)."""
    activa = tenant.membresia_activa
    categoria = activa.categoria if activa else CategoriaSuscripcion.SIN_MEMBRESIA
    if tenant.categoria != categoria:
        tenant.categoria = categoria
        tenant.save(update_fields=['categoria', 'fecha_modificacion'])
    return tenant.categoria


def expirar_vencidas():
    """Marca EXPIRADA toda membresía ACTIVA cuya fecha_expiracion ya pasó y
    degrada los tenants afectados a SIN_MEMBRESIA. Devuelve los tenants
    degradados. Idempotente: pensada para cron."""
    ahora = timezone.now()
    degradados = []
    vencidas = (Membresia.objects
                .filter(estado=EstadoMembresia.ACTIVA, fecha_expiracion__lte=ahora)
                .select_related('tenant'))
    for membresia in vencidas:
        with transaction.atomic():
            membresia.estado = EstadoMembresia.EXPIRADA
            membresia.save(update_fields=['estado', 'fecha_modificacion'])
            sincronizar_categoria(membresia.tenant)
        degradados.append(membresia.tenant)
    return degradados
