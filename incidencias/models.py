from django.conf import settings
from django.db import models, transaction


class SeveridadIncidencia(models.TextChoices):
    BAJA = 'BAJA', 'Baja'
    MEDIA = 'MEDIA', 'Media'
    ALTA = 'ALTA', 'Alta'
    CRITICA = 'CRITICA', 'Crítica'


class EstadoIncidencia(models.TextChoices):
    ABIERTO = 'ABIERTO', 'Abierto'
    EN_PROGRESO = 'EN_PROGRESO', 'En progreso'
    RESUELTO = 'RESUELTO', 'Resuelto'
    CERRADO = 'CERRADO', 'Cerrado'


class Incidencia(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='incidencias')
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.CASCADE,
                                db_column='cliente_id', related_name='incidencias')
    # Nulos: el cliente puede reportar sin identificar contrato/software exacto.
    contrato = models.ForeignKey('contratos.Contrato', on_delete=models.SET_NULL, null=True, blank=True,
                                 db_column='contrato_id', related_name='incidencias')
    software = models.ForeignKey('catalogo.Producto', on_delete=models.SET_NULL, null=True, blank=True,
                                 db_column='software_id', related_name='incidencias')

    titulo = models.CharField(max_length=200)
    descripcion = models.TextField()
    severidad = models.CharField(max_length=10, choices=SeveridadIncidencia.choices,
                                 default=SeveridadIncidencia.MEDIA)
    estado = models.CharField(max_length=15, choices=EstadoIncidencia.choices,
                              default=EstadoIncidencia.ABIERTO)

    reportado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='incidencias_reportadas', db_column='reportado_por_id')
    asignado_a = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='incidencias_asignadas', db_column='asignado_a_id')

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    fecha_resolucion = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'incidencias_incidencia'
        ordering = ['-fecha_creacion']
        indexes = [
            models.Index(fields=['tenant', 'estado'], name='idx_incidencia_tenant_estado'),
            models.Index(fields=['cliente', 'estado'], name='idx_incidencia_cliente_estado'),
        ]

    def __str__(self):
        return f"#{self.id} {self.titulo}"

    def transicionar_estado(self, nuevo_estado, usuario=None):
        if self.estado == nuevo_estado:
            return
        with transaction.atomic():
            HistorialEstadoIncidencia.objects.create(
                incidencia=self,
                estado_anterior=self.estado,
                estado_nuevo=nuevo_estado,
                usuario=usuario,
            )
            self.estado = nuevo_estado
            update_fields = ['estado', 'fecha_actualizacion']
            if nuevo_estado == EstadoIncidencia.RESUELTO and not self.fecha_resolucion:
                from django.utils import timezone
                self.fecha_resolucion = timezone.now()
                update_fields.append('fecha_resolucion')
            self.save(update_fields=update_fields)


class ComentarioIncidencia(models.Model):
    id = models.BigAutoField(primary_key=True)
    incidencia = models.ForeignKey(Incidencia, on_delete=models.CASCADE,
                                   related_name='comentarios', db_column='incidencia_id')
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              db_column='autor_id')
    mensaje = models.TextField()
    es_interno = models.BooleanField(
        default=False,
        help_text='Nota interna de staff, nunca visible para el cliente externo.',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'incidencias_comentario'
        ordering = ['fecha_creacion']

    def __str__(self):
        return f"Comentario #{self.id} en Incidencia #{self.incidencia_id}"


class HistorialEstadoIncidencia(models.Model):
    """Auditoría de cambios de estado — mismo patrón que HistorialEtapaContrato."""
    id = models.BigAutoField(primary_key=True)
    incidencia = models.ForeignKey(Incidencia, on_delete=models.CASCADE,
                                   related_name='historial_estados', db_column='incidencia_id')
    estado_anterior = models.CharField(max_length=15, choices=EstadoIncidencia.choices, null=True, blank=True)
    estado_nuevo = models.CharField(max_length=15, choices=EstadoIncidencia.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                db_column='usuario_id')
    fecha_cambio = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'incidencias_historialestado'
        ordering = ['fecha_cambio']


class AdjuntoIncidencia(models.Model):
    id = models.BigAutoField(primary_key=True)
    incidencia = models.ForeignKey(Incidencia, on_delete=models.CASCADE,
                                   related_name='adjuntos', db_column='incidencia_id')
    # Nulo si el adjunto va con el reporte inicial; poblado si se sube junto a un comentario.
    comentario = models.ForeignKey(ComentarioIncidencia, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='adjuntos', db_column='comentario_id')
    archivo = models.FileField(upload_to='incidencias/%Y/%m/%d/')
    nombre = models.CharField(max_length=255)
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   db_column='subido_por_id')
    fecha_subida = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'incidencias_adjunto'

    def __str__(self):
        return self.nombre
