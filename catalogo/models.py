import uuid
from django.db import models
from django.utils import timezone

class Software(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    descripcion = models.TextField(blank=True, null=True)
    api_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nombre

class SoftwareVersion(models.Model):
    software = models.ForeignKey(Software, on_delete=models.CASCADE, related_name='versiones')
    version_semver = models.CharField(max_length=30)
    changelog = models.TextField(blank=True, null=True)
    fecha_liberacion = models.DateField(default=timezone.now)

    class Meta:
        unique_together = ('software', 'version_semver')
        ordering = ['-fecha_liberacion']