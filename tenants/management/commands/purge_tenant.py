"""Baja de servicio: destrucción total y aislada de los datos de un tenant.

Borra en orden explícito de dependencias (los PROTECT/RESTRICT entre modelos
hijos impiden confiar en el cascade de tenant.delete()). Filtra estrictamente
por tenant_id, por lo que no puede tocar datos de otros tenants ni la
configuración global.

Uso:
    python manage.py purge_tenant <uuid> --dry-run   # ver qué se borraría
    python manage.py purge_tenant <uuid> --confirm   # ejecutar el borrado
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from clientes.models import Cliente, ContactoRepresentante
from catalogo.models import Producto, Software, SoftwareVersion
from contratos.models import (
    SLA, ArchivoAdjunto, Contrato, HistorialEtapaContrato,
    ObligacionSLA, ObligacionSLAAuditLog, RegistroPerdonazo,
)
from legal.models import DocumentoLegal, LogAceptacion
from plantillas.models import Clausula, DocumentoGenerado, PlantillaDocumento, VersionClausula
from tenants.models import Tenant, User


class Command(BaseCommand):
    help = 'Elimina de forma permanente todos los datos y usuarios de un tenant.'

    def add_arguments(self, parser):
        parser.add_argument('tenant_id', help='UUID del tenant a purgar')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real')
        parser.add_argument('--dry-run', action='store_true', help='Solo muestra los conteos')

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(pk=options['tenant_id'])
        except (Tenant.DoesNotExist, ValueError):
            raise CommandError('Tenant no encontrado')

        # Orden de borrado: hojas primero, respetando PROTECT/RESTRICT.
        borrables = [
            ('Documentos generados', DocumentoGenerado.objects.filter(contrato__tenant=tenant)),
            ('Logs de aceptación', LogAceptacion.objects.filter(cliente__tenant=tenant)),
            ('Perdonazos', RegistroPerdonazo.objects.filter(contrato__tenant=tenant)),
            ('Archivos adjuntos', ArchivoAdjunto.objects.filter(contrato__tenant=tenant)),
            ('Audit logs SLA', ObligacionSLAAuditLog.objects.filter(contrato__tenant=tenant)),
            ('Obligaciones SLA', ObligacionSLA.objects.filter(contrato__tenant=tenant)),
            ('Historial de etapas', HistorialEtapaContrato.objects.filter(contrato__tenant=tenant)),
            ('Contratos', Contrato.objects.filter(tenant=tenant)),
            ('Plantillas', PlantillaDocumento.objects.filter(tenant=tenant)),
            ('Versiones de cláusula', VersionClausula.objects.filter(clausula__tenant=tenant)),
            ('Cláusulas', Clausula.objects.filter(tenant=tenant)),
            ('Documentos legales', DocumentoLegal.objects.filter(tenant=tenant)),
            ('Contactos representantes', ContactoRepresentante.objects.filter(cliente_juridico__tenant=tenant)),
            ('Clientes', Cliente.objects.filter(tenant=tenant)),
            ('Versiones de software', SoftwareVersion.objects.filter(software__tenant=tenant)),
            ('Productos', Producto.objects.filter(tenant=tenant)),
            ('Software', Software.objects.filter(tenant=tenant)),
            ('SLAs', SLA.objects.filter(tenant=tenant)),
            ('Usuarios', User.objects.filter(tenant=tenant)),
        ]

        self.stdout.write(f"Tenant: {tenant.razon_social} ({tenant.pk})")
        for nombre, qs in borrables:
            self.stdout.write(f"  {nombre}: {qs.count()}")

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                'Dry-run. Nada borrado. Ejecuta con --confirm para el borrado definitivo.'))
            return

        # Archivos físicos (best-effort, antes de perder las referencias).
        archivos = []
        archivos += [d.archivo_docx for d in DocumentoGenerado.objects.filter(contrato__tenant=tenant)]
        archivos += [d.archivo_pdf for d in DocumentoGenerado.objects.filter(contrato__tenant=tenant)]
        archivos += [a.archivo for a in ArchivoAdjunto.objects.filter(contrato__tenant=tenant)]
        archivos += [p.archivo_docx for p in PlantillaDocumento.objects.filter(tenant=tenant)
                     if p.archivo_docx]

        with transaction.atomic():
            for nombre, qs in borrables:
                # .delete() de queryset: no pasa por save()/delete() custom de los modelos.
                count, _ = qs.delete()
                self.stdout.write(f"  Borrados {nombre}: {count}")
            tenant.delete()

        for f in archivos:
            try:
                f.storage.delete(f.name)
            except Exception:  # noqa: BLE001 — el archivo puede no existir; no aborta el purge
                pass

        self.stdout.write(self.style.SUCCESS(
            f"Tenant {options['tenant_id']} y todos sus datos destruidos."))
