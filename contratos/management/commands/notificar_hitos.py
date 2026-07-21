import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from contratos.models import HitoContrato, EstadoHito
from notificaciones.models import Notificacion, TipoNotificacion

class Command(BaseCommand):
    help = 'Revisa los hitos de contratos y genera notificaciones para aquellos que estén próximos a vencer.'

    def handle(self, *args, **options):
        hoy = timezone.now().date()
        hitos_pendientes = HitoContrato.objects.filter(estado=EstadoHito.PENDIENTE, alerta_enviada=False)

        count = 0
        for hito in hitos_pendientes:
            fecha_aviso = hito.fecha_esperada - datetime.timedelta(days=hito.dias_aviso_previo)
            if hoy >= fecha_aviso:
                # Crear notificación
                Notificacion.objects.create(
                    tenant=hito.contrato.tenant,
                    cliente=hito.contrato.cliente,
                    usuario_destino=hito.responsable,
                    titulo=f"Hito próximo: {hito.descripcion}",
                    cuerpo=f"El hito '{hito.descripcion}' del contrato '{hito.contrato.nombre or 'S/N'}' está programado para el {hito.fecha_esperada}.",
                    tipo=TipoNotificacion.AVISO,
                    para_staff=True,
                    enlace=f"/admin/contratos/contrato/{hito.contrato.id}/change/" # Ejemplo de enlace al admin o frontend
                )
                
                # Marcar como notificado
                hito.alerta_enviada = True
                hito.save(update_fields=['alerta_enviada'])
                count += 1

        self.stdout.write(self.style.SUCCESS(f"Proceso completado. Se enviaron {count} notificaciones de hitos."))
