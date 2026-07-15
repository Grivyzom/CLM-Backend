"""Alta de un tenant con su primer Administrador de Cuenta.

Uso:
    python manage.py create_tenant "Acme SpA" --categoria PLATA \
        --admin-username acme_admin --admin-password 'S3guro!' --admin-email admin@acme.cl
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tenants.models import CategoriaSuscripcion, RolTenant, Tenant, User


class Command(BaseCommand):
    help = 'Crea un tenant y su usuario Administrador de Cuenta inicial.'

    def add_arguments(self, parser):
        parser.add_argument('razon_social')
        parser.add_argument('--categoria', default=CategoriaSuscripcion.COBRE,
                            choices=CategoriaSuscripcion.values)
        parser.add_argument('--admin-username', required=True)
        parser.add_argument('--admin-password', required=True)
        parser.add_argument('--admin-email', default='')

    def handle(self, *args, **options):
        if Tenant.objects.filter(razon_social__iexact=options['razon_social']).exists():
            raise CommandError('Ya existe un tenant con esa razón social')
        if User.objects.filter(username=options['admin_username']).exists():
            raise CommandError('El username ya existe')

        with transaction.atomic():
            tenant = Tenant.objects.create(
                razon_social=options['razon_social'],
                categoria=options['categoria'],
            )
            admin = User.objects.create_user(
                username=options['admin_username'],
                password=options['admin_password'],
                email=options['admin_email'],
                tenant=tenant,
                role=RolTenant.TENANT_ADMIN,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Tenant '{tenant.razon_social}' ({tenant.pk}) creado — plan {tenant.categoria}. "
            f"Admin: {admin.username}"))
