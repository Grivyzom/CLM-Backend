"""Cron diario recomendado: marca EXPIRADA toda membresía vencida y degrada
el tenant a SIN_MEMBRESIA. Idempotente.

    python manage.py expirar_membresias
"""

from django.core.management.base import BaseCommand

from tenants.membresias import expirar_vencidas


class Command(BaseCommand):
    help = 'Expira las membresías ACTIVAS vencidas y degrada los tenants a SIN_MEMBRESIA.'

    def handle(self, *args, **options):
        degradados = expirar_vencidas()
        if not degradados:
            self.stdout.write('Sin membresías vencidas.')
            return
        for tenant in degradados:
            self.stdout.write(f'  {tenant.razon_social} → SIN_MEMBRESIA')
        self.stdout.write(self.style.SUCCESS(f'{len(degradados)} tenant(s) degradado(s).'))
