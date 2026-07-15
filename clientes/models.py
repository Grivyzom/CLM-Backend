from django.conf import settings
from django.db import models

class Cliente(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='clientes')
    email_principal = models.CharField(max_length=255)
    telefono_contacto = models.CharField(max_length=32, blank=True, null=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'clientes_cliente'
        constraints = [
            # Unicidad por tenant: dos empresas distintas pueden registrar el mismo email.
            models.UniqueConstraint(fields=['tenant', 'email_principal'],
                                    name='uniq_cliente_email_por_tenant'),
        ]

    def __str__(self):
        if hasattr(self, 'personanatural'):
            return self.personanatural.nombre_completo
        if hasattr(self, 'personajuridica'):
            return self.personajuridica.razon_social
        return f"Cliente {self.id}"

class PersonaNatural(Cliente):
    # Hereda de Cliente, Django creará automáticamente cliente_ptr_id como PK y FK.
    # run sin unique de DB: la unicidad es por tenant y tenant vive en la tabla
    # padre (multi-table inheritance no permite constraint cruzando tablas);
    # se valida a nivel de aplicación en clientes/views.py.
    run = models.CharField(max_length=12)
    nombre_completo = models.CharField(max_length=255)

    class Meta:
        db_table = 'clientes_personanatural'

class PersonaJuridica(Cliente):
    rut = models.CharField(max_length=12)
    razon_social = models.CharField(max_length=255)
    giro = models.CharField(max_length=255)

    class Meta:
        db_table = 'clientes_personajuridica'

class EstadoCorreo(models.TextChoices):
    ENVIADO = 'ENVIADO', 'Enviado'
    FALLIDO = 'FALLIDO', 'Fallido'


class CorreoEnviado(models.Model):
    """Historial de correos enviados a un cliente desde el workspace.
    Se registra también el intento fallido (estado FALLIDO + error) para que
    el historial refleje la realidad y no solo los éxitos."""
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                               db_column='tenant_id', related_name='correos_enviados')
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE,
                                db_column='cliente_id', related_name='correos')
    destinatario = models.EmailField(max_length=255)
    asunto = models.CharField(max_length=200)
    cuerpo = models.TextField()
    enviado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, db_column='enviado_por_id',
                                    related_name='correos_cliente_enviados')
    estado = models.CharField(max_length=10, choices=EstadoCorreo.choices,
                              default=EstadoCorreo.ENVIADO)
    error = models.TextField(blank=True, default='')
    fecha_envio = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'clientes_correoenviado'
        ordering = ['-fecha_envio']
        indexes = [
            models.Index(fields=['cliente', 'fecha_envio'], name='idx_correo_cliente_fecha'),
        ]

    def __str__(self):
        return f"{self.destinatario} — {self.asunto}"


class ContactoRepresentante(models.Model):
    id = models.BigAutoField(primary_key=True)
    cliente_juridico = models.ForeignKey(PersonaJuridica, on_delete=models.CASCADE, db_column='cliente_juridico_id')
    nombre = models.CharField(max_length=255)
    cargo = models.CharField(max_length=100)
    email = models.CharField(max_length=255)
    telefono = models.CharField(max_length=32, blank=True, null=True)

    class Meta:
        db_table = 'clientes_contactorepresentante'