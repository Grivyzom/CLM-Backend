from django.db import models
from tenants.models import Tenant

class TipoDocumento(models.Model):
    """
    Catálogo central de documentos que la plataforma maneja.
    Ej: "Cédula de Identidad", "RUT de la Empresa", "Escritura de Constitución", "Orden de Compra".
    """
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='tipos_documento')
    nombre = models.CharField(max_length=255)
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'documentos_tipodocumento'
        unique_together = ('tenant', 'nombre')

    def __str__(self):
        return self.nombre


class RequisitoDocumental(models.Model):
    """
    Reglas que definen CUÁNDO se exige un TipoDocumento.
    """
    TIPO_CLIENTE_CHOICES = [
        ('TODOS', 'Aplica a todos'),
        ('NATURAL', 'Solo Persona Natural'),
        ('JURIDICA', 'Solo Persona Jurídica'),
    ]

    # Importamos CATEGORIAS de Producto dinámicamente si es necesario, 
    # pero es mejor declararlo igual o cargarlo (como es una tupla estática en catalogo).
    # En Producto las CATEGORIAS son:
    # ('Bot', 'Bot'), ('Agente', 'Agente'), ('Script', 'Script'), 
    # ('Software', 'Software'), ('Auditoría', 'Auditoría'), ('Consultoría', 'Consultoría')
    CATEGORIAS = [
        ('Bot', 'Bot'),
        ('Agente', 'Agente'),
        ('Script', 'Script'),
        ('Software', 'Software'),
        ('Auditoría', 'Auditoría'),
        ('Consultoría', 'Consultoría'),
    ]

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='requisitos_documentales')
    tipo_documento = models.ForeignKey(TipoDocumento, on_delete=models.CASCADE, related_name='requisitos')
    
    # Filtro por tipo de cliente
    tipo_cliente = models.CharField(max_length=20, choices=TIPO_CLIENTE_CHOICES, default='TODOS')
    
    # Filtro por Software / Producto
    categoria_producto = models.CharField(
        max_length=50,
        choices=CATEGORIAS,
        blank=True,
        null=True,
        help_text="Ej: 'Software', 'Bot'. Si está vacío, no filtra por categoría."
    )
    
    producto_especifico = models.ForeignKey(
        'catalogo.Producto',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        help_text="Aplica solo a este producto específico."
    )
    
    es_obligatorio = models.BooleanField(default=True)

    class Meta:
        db_table = 'documentos_requisitodocumental'

    def __str__(self):
        return f"Requisito: {self.tipo_documento.nombre} para {self.tipo_cliente}"
