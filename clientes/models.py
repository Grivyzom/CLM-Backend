from django.db import models

class Cliente(models.Model):
    """
    Modelo Base (Multi-table inheritance). 
    PostgreSQL creará una tabla base y Django manejará los punteros implícitos 
    hacia las tablas hijas de forma automática.
    """
    id = models.AutoField(primary_key=True)
    email_principal = models.EmailField(unique=True, max_length=255)
    telefono_contacto = models.CharField(max_length=20, blank=True, null=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        # Intenta retornar el nombre específico según el tipo de persona de manera dinámica
        if hasattr(self, 'personanatural'):
            return self.personanatural.nombre_completo
        if hasattr(self, 'personajuridica'):
            return self.personajuridica.razon_social
        return f"Cliente ID: {self.id}"

class PersonaNatural(Cliente):
    """Entidad Independiente o Pro-bono."""
    run = models.CharField(max_length=12, unique=True) # Formato: 12.345.678-K
    nombre_completo = models.CharField(max_length=255)

    class Meta:
        verbose_name = "Persona Natural"
        verbose_name_plural = "Personas Naturales"

class PersonaJuridica(Cliente):
    """Entidad Corporativa / Empresa."""
    rut = models.CharField(max_length=12, unique=True) # Formato: 76.123.456-7
    razon_social = models.CharField(max_length=255)
    giro = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        verbose_name = "Persona Jurídica"
        verbose_name_plural = "Personas Jurídicas"

class ContactoRepresentante(models.Model):
    """
    Modelo separado para los representantes legales o contactos técnicos 
    asociados exclusivamente a Personas Jurídicas.
    """
    cliente_juridico = models.ForeignKey(
        PersonaJuridica, 
        on_delete=models.CASCADE, 
        related_name='contactos'
    )
    nombre = models.CharField(max_length=255)
    cargo = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(max_length=255)
    telefono = models.CharField(max_length=20, blank=True, null=True)

    def __str__(self):
        return f"{self.nombre} ({self.cargo}) - {self.cliente_juridico.razon_social}"