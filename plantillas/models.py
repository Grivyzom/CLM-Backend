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
        max_length=20,
        help_text="Identificador de familia de documento (ej: NDA, MSA, TOS, REQ), compartido por "
                   "todas las versiones del mismo documento sin importar el modo de generación. "
                   "Agrupa las versiones en el catálogo y, para plantillas HTML, arma el correlativo "
                   "de 'Referencia' como PREFIJO-AÑO-NNN al generar el documento."
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
    portada = models.ImageField(upload_to='plantillas_portadas/%Y/%m/', null=True, blank=True, help_text="Imagen de la primera página (portada) de la plantilla")
    activa = models.BooleanField(default=True)
    confirmada = models.BooleanField(
        default=False,
        help_text="False = borrador: la plantilla aún no se publicó, puede editarse y eliminarse "
                   "libremente y no aparece al crear contratos. Activarla la confirma (transición "
                   "de una sola vía); una plantilla confirmada ya no se elimina, solo se archiva."
    )
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
                # Activar es la transición borrador → confirmada (una sola vía).
                self.confirmada = True
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
    plantilla = models.ForeignKey(PlantillaDocumento, on_delete=models.SET_NULL, null=True, db_column='plantilla_id')
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


class TipoTextoClausula(models.TextChoices):
    """Clasifica cada texto de la biblioteca según su función dentro del documento.
    CLAUSULA es el cuerpo legal tradicional; el resto son textos de apoyo
    (saludos, introducciones, despedidas, cierres, bloques de firma) que permiten
    armar un documento completo sin redactar desde cero."""
    CLAUSULA = 'CLAUSULA', 'Cláusula'
    SALUDO = 'SALUDO', 'Saludo / Apertura'
    INTRODUCCION = 'INTRODUCCION', 'Introducción / Preámbulo'
    DESPEDIDA = 'DESPEDIDA', 'Despedida'
    CIERRE = 'CIERRE', 'Cierre legal'
    FIRMA = 'FIRMA', 'Bloque de firmas'
    OTRO = 'OTRO', 'Otro texto útil'


class Clausula(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='clausulas')
    categoria = models.CharField(max_length=100)
    nombre = models.CharField(max_length=200)
    tipo_texto = models.CharField(
        max_length=20,
        choices=TipoTextoClausula.choices,
        default=TipoTextoClausula.CLAUSULA,
        help_text="Función del texto dentro del documento: cláusula legal, saludo, "
                   "introducción, despedida, cierre o bloque de firmas."
    )
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
            # Índice del "recopilador por tipo": permite resolver el índice de
            # textos (agrupado por tipo_texto) en una sola consulta eficiente.
            models.Index(fields=['tenant', 'tipo_texto', 'activa'],
                         name='idx_clausula_tenant_tipo'),
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


class PreguntaFormulario(models.Model):
    id = models.BigAutoField(primary_key=True)
    plantilla = models.ForeignKey(PlantillaDocumento, on_delete=models.CASCADE, related_name='preguntas')
    texto = models.CharField(max_length=255)
    TIPO_OPCIONES = [
        ('booleano', 'Sí / No'),
        ('opcion_multiple', 'Opción Múltiple'),
    ]
    tipo = models.CharField(max_length=20, choices=TIPO_OPCIONES, default='booleano')
    orden = models.IntegerField(default=0)

    class Meta:
        db_table = 'plantillas_preguntaformulario'
        ordering = ['orden']

    def __str__(self):
        return self.texto


class OpcionRespuesta(models.Model):
    id = models.BigAutoField(primary_key=True)
    pregunta = models.ForeignKey(PreguntaFormulario, on_delete=models.CASCADE, related_name='opciones')
    texto = models.CharField(max_length=150)
    
    class Meta:
        db_table = 'plantillas_opcionrespuesta'

    def __str__(self):
        return self.texto


class ReglaInclusionClausula(models.Model):
    id = models.BigAutoField(primary_key=True)
    plantilla = models.ForeignKey(PlantillaDocumento, on_delete=models.CASCADE, related_name='reglas_inclusion')
    pregunta = models.ForeignKey(PreguntaFormulario, on_delete=models.CASCADE)
    opcion_respuesta = models.ForeignKey(OpcionRespuesta, on_delete=models.CASCADE, null=True, blank=True)
    respuesta_booleana = models.BooleanField(null=True, blank=True)
    clausula_version = models.ForeignKey(VersionClausula, on_delete=models.CASCADE)
    
    class Meta:
        db_table = 'plantillas_reglainclusion'

