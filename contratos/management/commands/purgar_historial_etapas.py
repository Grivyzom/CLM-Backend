"""Purga de HistorialEtapaContrato: la tabla de log más ruidosa del sistema
(se inserta una fila por cada transición de etapa de cualquier contrato, de
cualquier tenant, sin límite de retención). LogAceptacion NO se toca acá
-- es evidencia legal de aceptación de términos y debe conservarse íntegra.

Exporta a CSV antes de borrar (respaldo fuera de la DB) y solo borra
registros más viejos que --dias. Requiere --confirm; sin eso es dry-run.

Uso:
    python manage.py purgar_historial_etapas --dias 365 --output historial.csv --dry-run
    python manage.py purgar_historial_etapas --dias 365 --output historial.csv --confirm
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from contratos.models import HistorialEtapaContrato


class Command(BaseCommand):
    help = 'Exporta a CSV y purga HistorialEtapaContrato más viejo que --dias días.'

    def add_arguments(self, parser):
        parser.add_argument('--dias', type=int, default=365,
                             help='Antigüedad mínima en días para purgar (default: 365)')
        parser.add_argument('--output', required=True,
                             help='Ruta del archivo CSV donde se exportan los registros antes de borrarlos')
        parser.add_argument('--confirm', action='store_true', help='Ejecuta el borrado real')
        parser.add_argument('--dry-run', action='store_true', help='Solo muestra el conteo, no exporta ni borra')

    def handle(self, *args, **options):
        if options['dias'] < 30:
            raise CommandError('--dias debe ser al menos 30 (margen de seguridad contra purgas accidentales)')

        corte = timezone.now() - timezone.timedelta(days=options['dias'])
        qs = HistorialEtapaContrato.objects.filter(fecha_cambio__lt=corte).select_related('usuario').order_by('fecha_cambio')
        total = qs.count()

        self.stdout.write(f"Registros con fecha_cambio anterior a {corte.date()}: {total}")

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                'Dry-run. Nada exportado ni borrado. Ejecuta con --confirm para purgar.'))
            return

        if total == 0:
            self.stdout.write(self.style.SUCCESS('Nada que purgar.'))
            return

        with open(options['output'], 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'contrato_id', 'etapa_anterior', 'etapa_nueva',
                              'fecha_cambio', 'usuario', 'notas'])
            ids_a_borrar = []
            for h in qs.iterator():
                writer.writerow([
                    h.id, h.contrato_id, h.etapa_anterior or '', h.etapa_nueva,
                    h.fecha_cambio.isoformat(),
                    h.usuario.username if h.usuario else '',
                    (h.notas or '').replace('\n', ' '),
                ])
                ids_a_borrar.append(h.id)

        self.stdout.write(f"Exportados {len(ids_a_borrar)} registros a {options['output']}")

        with transaction.atomic():
            count, _ = HistorialEtapaContrato.objects.filter(id__in=ids_a_borrar).delete()

        self.stdout.write(self.style.SUCCESS(f"Purgados {count} registros de HistorialEtapaContrato."))
