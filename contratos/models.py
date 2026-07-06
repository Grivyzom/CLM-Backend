from django.db import models
from django.db.models import CheckConstraint, Q, F
from core.middleware import ThreadLocalContext
from django.conf import settings
from django.core.exceptions import ValidationError

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

class FrecuenciaFacturacion(models.TextChoices):
    MENSUAL = 'MENSUAL', 'Mensual'
    ANUAL = 'ANUAL', 'Anual'

class EtapaContrato(models.TextChoices):
    BORRADOR = 'BORRADOR', 'Borrador (Draft)'
    REVISION = 'REVISION', 'En Revisión / Negociación'
    APROBADO = 'APROBADO', 'Aprobado internamente'
    PENDIENTE_FIRMA = 'PENDIENTE_FIRMA', 'Pendiente de Firma'
    ACTIVO = 'ACTIVO', 'Activo / Ejecutado'
    ENMENDADO = 'ENMENDADO', 'Enmendado (Amended)'
    TERMINADO = 'TERMINADO', 'Terminado / Expirado'


class Contrato(models.Model):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, db_column='cliente_id')
    software = models.ForeignKey('catalogo.Software', on_delete=models.PROTECT, db_column='software_id')
    sla = models.ForeignKey(SLA, on_delete=models.PROTECT, db_column='sla_id')
    
    etapa = models.CharField(max_length=30, choices=EtapaContrato.choices, default=EtapaContrato.BORRADOR)
    tipo_contrato = models.CharField(max_length=30, choices=TipoContrato.choices)
    status = models.CharField(max_length=30, choices=EstadoContrato.choices, default=EstadoContrato.ACTIVO)
    monto = models.DecimalField(max_digits=15, decimal_places=4, default=0.0000)
    frecuencia_facturacion = models.CharField(max_length=10, choices=FrecuenciaFacturacion.choices, null=True, blank=True)

    fecha_inicio = models.DateField()
    fecha_vencimiento = models.DateField(null=True, blank=True)
    dias_gracia_autorizados = models.IntegerField(default=0)
    fin_periodo_gracia = models.DateField(null=True, blank=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    version = models.CharField(max_length=10, default="1.0")
    parent_contrato = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='versiones_hijas', db_column='parent_contrato_id')

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
            ),
            CheckConstraint(
                check=Q(frecuencia_facturacion__isnull=True) | Q(tipo_contrato=TipoContrato.RECURRENTE),
                name='chk_frecuencia_solo_recurrente'
            ),
        ]

    def transicionar_etapa(self, nueva_etapa, usuario=None, notas=""):
        if self.etapa != nueva_etapa:
            from django.db import transaction
            with transaction.atomic():
                HistorialEtapaContrato.objects.create(
                    contrato=self,
                    etapa_anterior=self.etapa,
                    etapa_nueva=nueva_etapa,
                    usuario=usuario,
                    notas=notas
                )
                self.etapa = nueva_etapa
                self.save(update_fields=['etapa'])

class HistorialEtapaContrato(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='historial_etapas', db_column='contrato_id')
    etapa_anterior = models.CharField(max_length=30, choices=EtapaContrato.choices, null=True, blank=True)
    etapa_nueva = models.CharField(max_length=30, choices=EtapaContrato.choices)
    fecha_cambio = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notas = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'contratos_historialetapa'


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


class ObligacionSLA(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='obligaciones', db_column='contrato_id')
    tipo_obligacion = models.CharField(max_length=100)
    descripcion = models.TextField()
    penalizacion = models.TextField()
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'contratos_obligacion_sla'

    def save(self, *args, usuario=None, bypass_etapa_check=False, **kwargs):
        if not bypass_etapa_check and self.contrato.etapa != EtapaContrato.BORRADOR:
            raise ValidationError("No se pueden modificar obligaciones de un contrato que no esté en estado Borrador.")
        
        is_create = self.pk is None
        if not is_create:
            old = ObligacionSLA.objects.get(pk=self.pk)
            valor_anterior = f"Tipo: {old.tipo_obligacion} | Métrica: {old.descripcion} | Penalización: {old.penalizacion}"
        else:
            valor_anterior = ""
            
        super().save(*args, **kwargs)
        
        valor_nuevo = f"Tipo: {self.tipo_obligacion} | Métrica: {self.descripcion} | Penalización: {self.penalizacion}"
        
        actor_name = "Sistema"
        if usuario:
            actor_name = usuario.get_full_name() or usuario.username
        
        ObligacionSLAAuditLog.objects.create(
            contrato=self.contrato,
            obligacion_id=self.id,
            tipo_obligacion=self.tipo_obligacion,
            usuario=usuario,
            actor_nombre=actor_name,
            accion='CREAR' if is_create else 'EDITAR',
            valor_anterior=valor_anterior,
            valor_nuevo=valor_nuevo
        )

    def delete(self, *args, usuario=None, bypass_etapa_check=False, **kwargs):
        if not bypass_etapa_check and self.contrato.etapa != EtapaContrato.BORRADOR:
            raise ValidationError("No se pueden eliminar obligaciones de un contrato que no esté en estado Borrador.")
        
        valor_anterior = f"Tipo: {self.tipo_obligacion} | Métrica: {self.descripcion} | Penalización: {self.penalizacion}"
        contrato = self.contrato
        tipo_ob = self.tipo_obligacion
        ob_id = self.id
        
        actor_name = "Sistema"
        if usuario:
            actor_name = usuario.get_full_name() or usuario.username
            
        super().delete(*args, **kwargs)
        
        ObligacionSLAAuditLog.objects.create(
            contrato=contrato,
            obligacion_id=ob_id,
            tipo_obligacion=tipo_ob,
            usuario=usuario,
            actor_nombre=actor_name,
            accion='ELIMINAR',
            valor_anterior=valor_anterior,
            valor_nuevo=""
        )


class ObligacionSLAAuditLog(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='obligaciones_audit_logs', db_column='contrato_id')
    obligacion_id = models.IntegerField(null=True, blank=True)
    tipo_obligacion = models.CharField(max_length=100)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, db_column='usuario_id')
    actor_nombre = models.CharField(max_length=150, default="Sistema")
    fecha_cambio = models.DateTimeField(auto_now_add=True)
    accion = models.CharField(max_length=10)
    valor_anterior = models.TextField(blank=True, null=True)
    valor_nuevo = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'contratos_obligacionsla_auditlog'