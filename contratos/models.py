from django.db import models
from django.db.models import CheckConstraint, Q, F
from core.middleware import ThreadLocalContext

class SoftwareScopedManager(models.Manager):
    def get_queryset(self):
        current_software_id = ThreadLocalContext.get_current_software_id()
        if current_software_id:
            return super().get_queryset().filter(software_id=current_software_id)
        return super().get_queryset()


class SLA(models.Model):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=100, unique=True)
    uptime_garantizado = models.DecimalField(max_digits=5, decimal_places=2)
    tiempo_respuesta_horas = models.IntegerField()
    detalles = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'contratos_sla'
        constraints = [
            CheckConstraint(
                check=Q(uptime_garantizado__lte=100.00),
                name='chk_uptime_maximo'
            )
        ]

    def __str__(self):
        return self.nombre

class TipoContrato(models.TextChoices):
    RECURRENTE = 'RECURRENTE', 'Recurrente'
    PERPETUO = 'PERPETUO', 'Perpetuo'
    PRO_BONO = 'PRO_BONO', 'Pro Bono'
    INTERNO = 'INTERNO', 'Interno / Propio'

class EstadoContrato(models.TextChoices):
    ACTIVO = 'ACTIVO', 'Activo'
    MORA = 'MORA', 'Mora'
    GRACIA = 'GRACIA', 'Gracia'
    SUSPENDIDO = 'SUSPENDIDO', 'Suspendido'
    VENCIDO = 'VENCIDO', 'Vencido'

class Contrato(models.Model):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, db_column='cliente_id')
    software = models.ForeignKey('catalogo.Software', on_delete=models.PROTECT, db_column='software_id')
    sla = models.ForeignKey(SLA, on_delete=models.PROTECT, db_column='sla_id')
    
    tipo_contrato = models.CharField(max_length=30, choices=TipoContrato.choices)
    status = models.CharField(max_length=30, choices=EstadoContrato.choices, default=EstadoContrato.ACTIVO)
    monto = models.DecimalField(max_digits=15, decimal_places=4, default=0.0000)
    
    fecha_inicio = models.DateField()
    fecha_vencimiento = models.DateField(null=True, blank=True)
    dias_gracia_autorizados = models.IntegerField(default=0)
    fin_periodo_gracia = models.DateField(null=True, blank=True)

    objects = models.Manager()
    scoped_objects = SoftwareScopedManager()

    class Meta:
        db_table = 'contratos_contrato'
        indexes = [
            models.Index(fields=['cliente', 'status'], name='idx_contrato_cliente_status'),
            models.Index(fields=['software', 'status'], name='idx_contrato_soft_status'),
        ]
        constraints = [
            CheckConstraint(
                check=Q(fecha_vencimiento__gte=F('fecha_inicio')) | Q(fecha_vencimiento__isnull=True),
                name='chk_fecha_vencimiento_coherente'
            ),
            CheckConstraint(
                check=Q(fin_periodo_gracia__gte=F('fecha_vencimiento')) | Q(fin_periodo_gracia__isnull=True),
                name='chk_periodo_gracia_coherente'
            ),
            CheckConstraint(
                check=Q(dias_gracia_autorizados__gte=0),
                name='chk_dias_gracia_positivos'
            )
        ]

class RegistroPerdonazo(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(Contrato, on_delete=models.RESTRICT, db_column='contrato_id')
    fecha_concesion = models.DateTimeField(auto_now_add=True)
    dias_extendidos = models.IntegerField()
    motivo = models.TextField()
    fecha_vencimiento_anterior = models.DateField()

    class Meta:
        db_table = 'contratos_registroperdonazo'
        constraints = [
            CheckConstraint(
                check=Q(dias_extendidos__gt=0),
                name='chk_dias_extendidos_positivos'
            )
        ]

class ArchivoAdjunto(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='archivos', db_column='contrato_id')
    archivo = models.FileField(upload_to='archivos_proyectos/%Y/%m/%d/')
    nombre = models.CharField(max_length=255)
    descripcion = models.TextField(blank=True, null=True)
    fecha_subida = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'contratos_archivoadjunto'

    def __str__(self):
        return self.nombre