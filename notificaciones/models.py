from django.conf import settings
from django.db import models


class TipoNotificacion(models.TextChoices):
    INFO = 'INFO', 'Información'
    AVISO = 'AVISO', 'Aviso'
    URGENTE = 'URGENTE', 'Urgente'


class Notificacion(models.Model):
    """Notificación in-app dirigida a un Cliente. El staff la crea desde el
    workspace; los usuarios-cuenta del cliente (rol CLIENTE) la ven en su
    campana. Estado de lectura a nivel de notificación, no por usuario: si el
    cliente tiene varios usuarios-cuenta, el primero que la lee la marca para
    todos (simplificación deliberada, migrable a tabla de lecturas si hace
    falta)."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='notificaciones')
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.SET_NULL,
                                db_column='cliente_id', related_name='notificaciones',
                                null=True, blank=True)
    usuario_destino = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                        db_column='usuario_destino_id', related_name='notificaciones_recibidas',
                                        null=True, blank=True, help_text="Si está seteado, la notificación es para este usuario específico")
    titulo = models.CharField(max_length=150)
    cuerpo = models.TextField()
    tipo = models.CharField(max_length=10, choices=TipoNotificacion.choices,
                            default=TipoNotificacion.INFO)
    para_staff = models.BooleanField(default=False, help_text="True si es para admins/moderadores")
    enlace = models.CharField(max_length=255, null=True, blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, db_column='creado_por_id',
                                   related_name='notificaciones_creadas')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    leida = models.BooleanField(default=False)
    leida_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'notificaciones_notificacion'
        ordering = ['-fecha_creacion']
        indexes = [
            models.Index(fields=['cliente', 'leida'], name='idx_notif_cliente_leida'),
        ]

    def __str__(self):
        return f"[{self.tipo}] {self.titulo}"
