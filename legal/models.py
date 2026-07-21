from django.db import models
from django.contrib.postgres.indexes import GinIndex

class DocumentoLegal(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='documentos_legales')
    tipo = models.CharField(max_length=30)
    version_codigo = models.CharField(max_length=32)
    contenido_html = models.TextField()
    fecha_publicacion = models.DateTimeField(auto_now_add=True)
    is_vigente = models.BooleanField(default=True)

    class Meta:
        db_table = 'legal_documentolegal'
        indexes = [
            # Partial index sobre las columnas de búsqueda real (tenant+tipo vigente),
            # no sobre 'id' (que Postgres ya indexa como PK y no acelera este filtro).
            models.Index(fields=['tenant', 'tipo'], condition=models.Q(is_vigente=True), name='idx_doc_legal_vigente'),
        ]

class LogAceptacion(models.Model):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, db_column='cliente_id')
    software = models.ForeignKey('catalogo.Producto', on_delete=models.PROTECT, db_column='software_id')
    documento_legal = models.ForeignKey(DocumentoLegal, on_delete=models.PROTECT, db_column='documento_legal_id')
    
    ip_direccion = models.GenericIPAddressField()
    user_agent = models.TextField()
    fecha_hora_registro = models.DateTimeField(auto_now_add=True)
    metadata_auditoria = models.JSONField(default=dict)

    class Meta:
        db_table = 'legal_logaceptacion'
        indexes = [
            models.Index(fields=['cliente', 'documento_legal'], name='idx_log_cliente_doc'),
            # Índice GIN para buscar rápidamente propiedades dentro del JSONB
            GinIndex(fields=['metadata_auditoria'], name='idx_log_metadata_gin'),
        ]


class AnalisisContratoIA(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey('contratos.Contrato', on_delete=models.CASCADE, related_name='analisis_ia', db_column='contrato_id')
    fecha_analisis = models.DateTimeField(auto_now_add=True)
    checklist_cumplido = models.BooleanField(default=False)
    # { "required": [{"doc": "NDA", "cumple": true, "detalles": "..."}, ...], "missing": [...] }
    resultado_checklist_json = models.JSONField(default=dict)
    # [ { "clausula_nombre": "Confidencialidad", "riesgo": "Alto", "similitud": 85, "explicacion": "...", "diff": "...", "original": "...", "standard": "..." } ]
    riesgos_detectados_json = models.JSONField(default=list)
    texto_analizado = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'legal_analisiscontratoia'
        ordering = ['-fecha_analisis']