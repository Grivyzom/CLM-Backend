from django.conf import settings
from django.db import models, transaction

from contratos.models import TipoContrato


class ModoOrigenPlantilla(models.TextChoices):
    ARCHIVO = 'archivo', 'Documento propio (.docx)'
    CLAUSULAS = 'clausulas', 'Generado por cláusulas del sistema'
    HTML = 'html', 'Código HTML'


class PlantillaDocumento(models.Model):
    """Plantilla de contrato vinculada a un Software/Producto del catálogo.
    modo_origen='archivo'  → se usa el .docx subido directamente.
    modo_origen='clausulas' → el documento se genera dinámicamente con el motor de cláusulas."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='plantillas')
    nombre = models.CharField(max_length=150)
    tipo_contrato = models.CharField(max_length=30, choices=TipoContrato.choices)
    software = models.ForeignKey('catalogo.Producto', on_delete=models.PROTECT, db_column='software_id',
                                 null=True, blank=True)
    modo_origen = models.CharField(
        max_length=20,
        choices=ModoOrigenPlantilla.choices,
        default=ModoOrigenPlantilla.ARCHIVO,
        help_text="Indica si la plantilla usa un archivo .docx propio o el motor de cláusulas."
    )
    archivo_docx = models.FileField(upload_to='plantillas_contrato/%Y/%m/', null=True, blank=True)
    ruta_plantilla_html = models.CharField(max_length=255, blank=True, null=True, help_text="Ruta del archivo HTML en el backend (ej: plantillas_html/mi_plantilla.html)")
    codigo_prefijo = models.CharField(
        max_length=20, blank=True, null=True,
        help_text="Prefijo del correlativo de 'Referencia' para documentos HTML generados con esta "
                   "plantilla (ej: NDA, MSA, TOS, REQ). El sistema arma códigos únicos tipo "
                   "PREFIJO-AÑO-NNN al generar el documento; se comparte entre plantillas con el "
                   "mismo prefijo (misma familia de documento) y se resetea cada año."
    )
    requiere_sla_facturacion = models.BooleanField(
        default=True,
        help_text="Si está desmarcado, el wizard 'Nuevo Contrato' no pide SLA, facturación ni días "
                   "de gracia para esta plantilla (documentos administrativos como NDA, memorándums "
                   "o fichas de requerimientos, que no son un servicio con nivel de servicio ni cobro). "
                   "Se les asigna un SLA técnico 'N/A' automáticamente."
    )
    clausulas_seleccionadas = models.ManyToManyField('Clausula', blank=True, related_name='plantillas')
    version_codigo = models.CharField(max_length=32)
    activa = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    subida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, db_column='subida_por_id')

    class Meta:
        db_table = 'plantillas_plantilladocumento'
        indexes = [
            models.Index(fields=['tipo_contrato', 'software'], name='idx_plantilla_tipo_software'),
        ]
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f"{self.nombre} ({self.version_codigo})"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.activa:
                # Solo puede haber una plantilla activa por (tipo_contrato, software).
                # Enforcement a nivel de aplicación, no constraint de BD: un índice único
                # parcial con FK nullable no serviría porque Postgres trata cada NULL
                # como distinto. Ventana de carrera aceptable dado el volumen de uso
                # (legal sube plantillas esporádicamente, no es endpoint de alto tráfico).
                qs = PlantillaDocumento.objects.filter(
                    tenant=self.tenant, tipo_contrato=self.tipo_contrato,
                    software=self.software, activa=True,
                )
                if self.pk:
                    qs = qs.exclude(pk=self.pk)
                qs.update(activa=False)
            super().save(*args, **kwargs)


class DocumentoGenerado(models.Model):
    """Registro inmutable de un documento generado a partir de un Contrato + PlantillaDocumento.
    Solo se crea, nunca se actualiza ni se borra (ver plantillas/admin.py)."""
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey('contratos.Contrato', on_delete=models.PROTECT, db_column='contrato_id',
                                  related_name='documentos_generados')
    plantilla = models.ForeignKey(PlantillaDocumento, on_delete=models.PROTECT, db_column='plantilla_id')
    archivo_docx = models.FileField(upload_to='contratos_generados/docx/%Y/%m/%d/', editable=False)
    archivo_pdf = models.FileField(upload_to='contratos_generados/pdf/%Y/%m/%d/', editable=False)
    hash_sha256 = models.CharField(max_length=64, editable=False)
    contexto_usado = models.JSONField(default=dict, editable=False)
    fecha_generacion = models.DateTimeField(auto_now_add=True)
    generado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, db_column='generado_por_id')

    class Meta:
        db_table = 'plantillas_documentogenerado'
        indexes = [
            models.Index(fields=['contrato', 'fecha_generacion'], name='idx_docgen_contrato_fecha'),
        ]
        ordering = ['-fecha_generacion']

    def __str__(self):
        return f"Documento contrato #{self.contrato_id} — {self.fecha_generacion:%Y-%m-%d %H:%M}"


class SecuenciaReferencia(models.Model):
    """Correlativo del código 'Referencia' de documentos HTML, por tenant + prefijo + año.
    Un solo contador compartido entre todas las plantillas con el mismo codigo_prefijo
    (misma familia de documento, ej. todos los NDA), reseteado cada año calendario."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='secuencias_referencia')
    prefijo = models.CharField(max_length=20)
    anio = models.IntegerField()
    ultimo_numero = models.IntegerField(default=0)

    class Meta:
        db_table = 'plantillas_secuenciareferencia'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'prefijo', 'anio'], name='uniq_secuencia_tenant_prefijo_anio'),
        ]

    def __str__(self):
        return f"{self.prefijo}-{self.anio} (último: {self.ultimo_numero})"


class Clausula(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='clausulas')
    categoria = models.CharField(max_length=100)
    nombre = models.CharField(max_length=200)
    RIESGO_CHOICES = [
        ('Alto', 'Alto'),
        ('Medio', 'Medio'),
        ('Bajo', 'Bajo'),
    ]
    riesgo = models.CharField(max_length=20, choices=RIESGO_CHOICES, default='Medio')
    activa = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'plantillas_clausula'
        ordering = ['categoria', 'nombre']
        indexes = [
            models.Index(fields=['tenant', 'activa'], name='idx_clausula_tenant_activa'),
        ]

    def __str__(self):
        return f"{self.nombre} ({self.categoria})"


class VersionClausula(models.Model):
    id = models.BigAutoField(primary_key=True)
    clausula = models.ForeignKey(Clausula, related_name='versiones', on_delete=models.CASCADE)
    etiqueta = models.CharField(max_length=100)
    
    TIPO_CHOICES = [
        ('Estándar', 'Estándar'),
        ('Alternativa', 'Alternativa'),
    ]
    tipo = models.CharField(max_length=50, choices=TIPO_CHOICES, default='Estándar')
    texto = models.TextField()
    activa = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'plantillas_versionclausula'
        ordering = ['id']

    def __str__(self):
        return f"{self.clausula.nombre} - {self.etiqueta}"
