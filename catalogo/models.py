from django.db import models
import uuid

class Software(models.Model):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=150, unique=True)
    descripcion = models.TextField(blank=True, null=True)
    # api_key UUID indexed and unique
    api_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'catalogo_software'

    def __str__(self):
        return self.nombre

class SoftwareVersion(models.Model):
    id = models.BigAutoField(primary_key=True)
    software = models.ForeignKey(Software, on_delete=models.RESTRICT, db_column='software_id')
    version_semver = models.CharField(max_length=32)
    changelog = models.TextField(blank=True, null=True)
    fecha_liberacion = models.DateField()

    class Meta:
        db_table = 'catalogo_softwareversion'

    def __str__(self):
        return f"{self.software.nombre} - {self.version_semver}"

class Producto(models.Model):
    CATEGORIAS = [
        ('Bot', 'Bot'),
        ('Agente', 'Agente'),
        ('Script', 'Script'),
        ('Software', 'Software'),
        ('Auditoría', 'Auditoría'),
        ('Consultoría', 'Consultoría'),
    ]
    ESTADOS = [
        ('Activo', 'Activo'),
        ('Descontinuado', 'Descontinuado'),
    ]

    id = models.BigAutoField(primary_key=True)
    sku = models.CharField(max_length=40, unique=True)
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True, null=True)
    categoria = models.CharField(max_length=20, choices=CATEGORIAS, default='Software')
    tipo_licencia = models.CharField(max_length=30, default='Comercial')
    precio = models.DecimalField(max_digits=12, decimal_places=2)
    moneda = models.CharField(max_length=8, default='USD')
    unidad = models.CharField(max_length=40, blank=True, default='')
    estado = models.CharField(max_length=20, choices=ESTADOS, default='Activo')
    datos_adicionales = models.JSONField(default=dict, blank=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'catalogo_producto'
        ordering = ['nombre']

    def __str__(self):
        return f"{self.sku} - {self.nombre}"