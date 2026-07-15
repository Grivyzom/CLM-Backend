from django.conf import settings
from django.db import models, transaction


class CategoriaProducto(models.TextChoices):
    """Replica el choice-set de catalogo.Producto.CATEGORIAS (no se importa
    directo porque Producto no lo expone como TextChoices)."""
    BOT = 'Bot', 'Bot'
    AGENTE = 'Agente', 'Agente'
    SCRIPT = 'Script', 'Script'
    SOFTWARE = 'Software', 'Software'
    AUDITORIA = 'Auditoría', 'Auditoría'
    CONSULTORIA = 'Consultoría', 'Consultoría'


class TipoPregunta(models.TextChoices):
    TEXTO = 'texto', 'Texto corto'
    PARRAFO = 'parrafo', 'Párrafo'
    NUMERO = 'numero', 'Número'
    BOOLEANO = 'booleano', 'Sí / No'
    SELECCION = 'seleccion', 'Selección'
    FECHA = 'fecha', 'Fecha'


class EstadoRequerimiento(models.TextChoices):
    BORRADOR = 'BORRADOR', 'Borrador'
    GENERADO = 'GENERADO', 'Generado'


class PlantillaRequerimiento(models.Model):
    """Set de preguntas de Toma de Requerimientos por categoría de Producto.

    tenant=None → plantilla global (fallback cuando el tenant no definió una
    propia para esa categoría), mismo patrón de resolución que
    plantillas.PlantillaDocumento."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='plantillas_requerimiento',
                               null=True, blank=True)
    categoria_producto = models.CharField(max_length=50, choices=CategoriaProducto.choices)
    nombre = models.CharField(max_length=150)
    activa = models.BooleanField(default=True)
    secciones = models.JSONField(
        default=list,
        help_text=(
            "Lista de secciones: "
            '[{"titulo": "...", "preguntas": [{"id": "slug", "texto": "...", '
            '"tipo": "texto|parrafo|numero|booleano|seleccion|fecha", '
            '"opciones": ["..."], "requerida": true}]}]'
        ),
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'requerimientos_plantillarequerimiento'
        indexes = [
            models.Index(fields=['categoria_producto', 'activa'], name='idx_plantreq_categoria_activa'),
        ]
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f"{self.nombre} ({self.categoria_producto})"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.activa:
                # Una sola plantilla activa por (tenant, categoria_producto).
                # Enforcement de aplicación, no constraint de BD: FK nullable
                # hace que Postgres trate cada NULL de tenant como distinto.
                qs = PlantillaRequerimiento.objects.filter(
                    tenant=self.tenant, categoria_producto=self.categoria_producto, activa=True,
                )
                if self.pk:
                    qs = qs.exclude(pk=self.pk)
                qs.update(activa=False)
            super().save(*args, **kwargs)


class Requerimiento(models.Model):
    """Instancia de Toma de Requerimientos: respuestas de un cliente para una
    categoría de Producto, opcionalmente vinculada a un Contrato concreto."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='requerimientos')
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT,
                                 db_column='cliente_id', related_name='requerimientos')
    contrato = models.ForeignKey('contratos.Contrato', on_delete=models.PROTECT,
                                  db_column='contrato_id', related_name='requerimientos',
                                  null=True, blank=True)
    categoria_producto = models.CharField(max_length=50, choices=CategoriaProducto.choices)
    plantilla = models.ForeignKey(PlantillaRequerimiento, on_delete=models.PROTECT,
                                   db_column='plantilla_id')
    respuestas = models.JSONField(default=dict, blank=True, help_text="{pregunta_id: valor}")
    estado = models.CharField(max_length=20, choices=EstadoRequerimiento.choices,
                               default=EstadoRequerimiento.BORRADOR)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, db_column='creado_por_id')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)
    fecha_generacion = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'requerimientos_requerimiento'
        indexes = [
            models.Index(fields=['cliente', 'estado'], name='idx_requerim_cliente_estado'),
            models.Index(fields=['tenant', 'estado'], name='idx_requerim_tenant_estado'),
        ]
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f"Requerimiento #{self.id} — {self.cliente} ({self.categoria_producto})"


class RequerimientoGenerado(models.Model):
    """Documento inmutable generado a partir de un Requerimiento. Solo se crea,
    nunca se actualiza ni se borra (ver requerimientos/admin.py)."""
    id = models.BigAutoField(primary_key=True)
    requerimiento = models.ForeignKey(Requerimiento, on_delete=models.PROTECT,
                                       db_column='requerimiento_id', related_name='documentos_generados')
    archivo_docx = models.FileField(upload_to='requerimientos_generados/docx/%Y/%m/%d/', editable=False)
    archivo_pdf = models.FileField(upload_to='requerimientos_generados/pdf/%Y/%m/%d/', editable=False)
    hash_sha256 = models.CharField(max_length=64, editable=False)
    fecha_generacion = models.DateTimeField(auto_now_add=True)
    generado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, db_column='generado_por_id')

    class Meta:
        db_table = 'requerimientos_requerimientogenerado'
        indexes = [
            models.Index(fields=['requerimiento', 'fecha_generacion'], name='idx_reqgen_requerimiento_fecha'),
        ]
        ordering = ['-fecha_generacion']

    def __str__(self):
        return f"Documento requerimiento #{self.requerimiento_id} — {self.fecha_generacion:%Y-%m-%d %H:%M}"
