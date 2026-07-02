from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from clientes.models import Cliente
from catalogo.models import Software

class SLA(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    uptime_garantizado = models.DecimalField(max_digits=5, decimal_places=2)
    tiempo_respuesta_horas = models.PositiveIntegerField()
    detalles = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.nombre

class Contrato(models.Model):
    TIPO_CONTRATO_CHOICES = [('RECURRENTE', 'Recurrente'), ('PERPETUO', 'Perpetuo'), ('PRO_BONO', 'Pro-bono')]
    ESTADO_CHOICES = [('ACTIVO', 'Activo'), ('MORA', 'Mora'), ('GRACIA', 'Gracia'), ('SUSPENDIDO', 'Suspendido'), ('VENCIDO', 'Vencido')]

    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name='contratos')
    software = models.ForeignKey(Software, on_delete=models.PROTECT, related_name='contratos')
    sla = models.ForeignKey(SLA, on_delete=models.SET_NULL, null=True, blank=True)
    tipo_contrato = models.CharField(max_length=20, choices=TIPO_CONTRATO_CHOICES)
    status = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='ACTIVO')
    monto = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    fecha_inicio = models.DateField(default=timezone.now)
    fecha_vencimiento = models.DateField(blank=True, null=True)
    dias_gracia_autorizados = models.PositiveIntegerField(default=0)
    fin_periodo_gracia = models.DateField(blank=True, null=True)

    @property
    def tiempo_restante(self):
        if not self.fecha_vencimiento: return "Indefinido"
        delta = self.fecha_vencimiento - timezone.now().date()
        return delta.days

class RegistroPerdonazo(models.Model):
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='perdonazos')
    fecha_concesion = models.DateTimeField(auto_now_add=True)
    dias_extendidos = models.PositiveIntegerField()
    motivo = models.TextField()
    fecha_vencimiento_anterior = models.DateField()