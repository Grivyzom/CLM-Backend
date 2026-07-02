from django.db import models
from django.core.exceptions import ValidationError
from clientes.models import Cliente
from catalogo.models import Software

class DocumentoLegal(models.Model):
    TIPO_DOC_CHOICES = [('TYC', 'Términos y Condiciones'), ('PRIVACIDAD', 'Privacidad')]
    tipo = models.CharField(max_length=20, choices=TIPO_DOC_CHOICES)
    version_codigo = models.CharField(max_length=20)
    contenido_html = models.TextField()
    fecha_publicacion = models.DateTimeField(auto_now_add=True)
    is_vigente = models.BooleanField(default=True)

    class Meta:
        unique_together = ('tipo', 'version_codigo')

class LogAceptacion(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name='logs_legales')
    software = models.ForeignKey(Software, on_delete=models.PROTECT)
    documento_legal = models.ForeignKey(DocumentoLegal, on_delete=models.PROTECT)
    ip_direccion = models.GenericIPAddressField()
    user_agent = models.TextField()
    fecha_hora_registro = models.DateTimeField(auto_now_add=True)
    metadata_auditoria = models.JSONField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.pk: raise ValidationError("Inmutable")
        super().save(*args, **kwargs)