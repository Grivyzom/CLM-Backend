from django.db import models

class DimCliente(models.Model):
    """
    Dimensión Cliente para el Data Warehouse.
    Desnormaliza la información de PersonaNatural, PersonaJuridica y Cliente base.
    """
    cliente_id_origen = models.IntegerField(unique=True, help_text="ID original del cliente en la BD transaccional")
    tipo_cliente = models.CharField(max_length=50) # 'NATURAL' o 'JURIDICA'
    nombre_completo = models.CharField(max_length=255)
    rut_identificador = models.CharField(max_length=50, null=True, blank=True)
    pais = models.CharField(max_length=100, null=True, blank=True)
    industria = models.CharField(max_length=100, null=True, blank=True) # Específico para jurídicas
    fecha_registro = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        db_table = 'dwh_dim_cliente'
        verbose_name = 'Dimensión Cliente'
        verbose_name_plural = 'Dimensiones Cliente'

    def __str__(self):
        return f"{self.nombre_completo} ({self.tipo_cliente})"

class DimSoftware(models.Model):
    """
    Dimensión Software para el Data Warehouse.
    """
    software_id_origen = models.IntegerField(unique=True, help_text="ID original del software")
    nombre = models.CharField(max_length=200)
    categoria = models.CharField(max_length=100, null=True, blank=True)
    estado = models.CharField(max_length=50, null=True, blank=True)
    
    class Meta:
        db_table = 'dwh_dim_software'
        verbose_name = 'Dimensión Software'
        verbose_name_plural = 'Dimensiones Software'

class FactContrato(models.Model):
    """
    Tabla de Hechos Contrato. Almacena las métricas principales de los contratos.
    """
    contrato_id_origen = models.IntegerField(unique=True, help_text="ID original del contrato")
    dim_cliente = models.ForeignKey(DimCliente, on_delete=models.CASCADE)
    dim_software = models.ForeignKey(DimSoftware, on_delete=models.CASCADE, null=True, blank=True)
    
    tipo_contrato = models.CharField(max_length=50, null=True, blank=True)
    estado = models.CharField(max_length=50, null=True, blank=True)
    etapa = models.CharField(max_length=50, null=True, blank=True)
    
    # Fechas importantes
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_termino = models.DateField(null=True, blank=True)
    
    # Métricas Monetarias
    monto_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ingreso_mensual_recurrente = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="MRR calculado")
    
    # Métricas de Servicio
    sla_cumplimiento_porcentaje = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    
    class Meta:
        db_table = 'dwh_fact_contrato'
        verbose_name = 'Hecho Contrato'
        verbose_name_plural = 'Hechos Contrato'

    def __str__(self):
        return f"FactContrato {self.contrato_id_origen} - Cliente {self.dim_cliente.nombre_completo}"
