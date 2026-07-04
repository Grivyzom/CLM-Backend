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