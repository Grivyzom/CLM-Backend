from django.conf import settings
from django.db import models, transaction

from contratos.models import TipoContrato


class ModoOrigenPlantilla(models.TextChoices):
    ARCHIVO = 'archivo', 'Documento propio (.docx)'
    CLAUSULAS = 'clausulas', 'Generado por cláusulas del sistema'


class PlantillaDocumento(models.Model):
    """Plantilla de contrato vinculada a un Software/Producto del catálogo.
    modo_origen='archivo'  → se usa el .docx subido directamente.
    modo_origen='clausulas' → el documento se genera dinámicamente con el motor de cláusulas."""
    id = models.BigAutoField(primary_key=True)
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
                    tipo_contrato=self.tipo_contrato, software=self.software, activa=True,
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


class Clausula(models.Model):
    id = models.BigAutoField(primary_key=True)
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
