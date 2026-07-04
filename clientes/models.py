from django.db import models

class Cliente(models.Model):
    id = models.BigAutoField(primary_key=True)
    email_principal = models.CharField(max_length=255, unique=True)
    telefono_contacto = models.CharField(max_length=32, blank=True, null=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'clientes_cliente'

    def __str__(self):
        if hasattr(self, 'personanatural'):
            return self.personanatural.nombre_completo
        if hasattr(self, 'personajuridica'):
            return self.personajuridica.razon_social
        return f"Cliente {self.id}"

class PersonaNatural(Cliente):
    # Hereda de Cliente, Django creará automáticamente cliente_ptr_id como PK y FK
    run = models.CharField(max_length=12, unique=True)
    nombre_completo = models.CharField(max_length=255)

    class Meta:
        db_table = 'clientes_personanatural'

class PersonaJuridica(Cliente):
    rut = models.CharField(max_length=12, unique=True)
    razon_social = models.CharField(max_length=255)
    giro = models.CharField(max_length=255)

    class Meta:
        db_table = 'clientes_personajuridica'

class ContactoRepresentante(models.Model):
    id = models.BigAutoField(primary_key=True)
    cliente_juridico = models.ForeignKey(PersonaJuridica, on_delete=models.CASCADE, db_column='cliente_juridico_id')
    nombre = models.CharField(max_length=255)
    cargo = models.CharField(max_length=100)
    email = models.CharField(max_length=255)
    telefono = models.CharField(max_length=32, blank=True, null=True)

    class Meta:
        db_table = 'clientes_contactorepresentante'