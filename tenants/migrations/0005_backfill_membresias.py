"""Backfill: los tenants que ya tenían una categoría de pago (asignada cuando
Tenant.categoria se editaba directo) reciben su Membresia ACTIVA indefinida,
para que el historial y la categoría denormalizada queden consistentes."""

from django.db import migrations
from django.utils import timezone


def crear_membresias(apps, schema_editor):
    Tenant = apps.get_model('tenants', 'Tenant')
    Membresia = apps.get_model('tenants', 'Membresia')
    ahora = timezone.now()
    for tenant in Tenant.objects.exclude(categoria='SIN_MEMBRESIA'):
        if not tenant.membresias.filter(estado='ACTIVA').exists():
            Membresia.objects.create(
                tenant=tenant,
                categoria=tenant.categoria,
                estado='ACTIVA',
                fecha_inicio=ahora,
                notas='Backfill: categoría previa al sistema de membresías.',
            )


def revertir(apps, schema_editor):
    Membresia = apps.get_model('tenants', 'Membresia')
    Membresia.objects.filter(notas__startswith='Backfill:').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tenants', '0004_alter_tenant_categoria_membresia_and_more'),
    ]

    operations = [
        migrations.RunPython(crear_membresias, revertir),
    ]
