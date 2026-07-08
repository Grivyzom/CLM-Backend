from django.db import models
from django.contrib.postgres.indexes import GinIndex

class DocumentoLegal(models.Model):
    id = models.BigAutoField(primary_key=True)
    tipo = models.CharField(max_length=30)
    version_codigo = models.CharField(max_length=32)
    contenido_html = models.TextField()
    fecha_publicacion = models.DateTimeField(auto_now_add=True)
    is_vigente = models.BooleanField(default=True)

    class Meta:
        db_table = 'legal_documentolegal'
        indexes = [
            # Partial index cannot be created just with models.Index using a condition in older django, 
            # but Django 3.2+ supports `condition=Q(is_vigente=True)`
            models.Index(fields=['id'], condition=models.Q(is_vigente=True), name='idx_doc_legal_vigente'),
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