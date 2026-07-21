import os
import logging
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.conf import settings

logger = logging.getLogger(__name__)

def enviar_correo_bienvenida(cliente, portal_user):
    """
    Envía un correo de bienvenida al cliente recién creado, con el enlace de
    activación de cuenta (uid+token de un solo uso, mismo default_token_generator
    que password-reset). Ese único enlace prueba posesión del correo y permite
    fijar la contraseña y activar la cuenta en un solo paso (ver
    core.views.api_register_cliente) — no hay un segundo correo de confirmación.
    """
    try:
        subject = "¡Bienvenido a Enfoque Platform!"
        from_email = settings.DEFAULT_FROM_EMAIL
        to_email = cliente.email_principal

        # Obtener el nombre según el tipo de cliente
        nombre = getattr(cliente, 'nombre_completo', getattr(cliente, 'razon_social', 'Cliente'))

        uid = urlsafe_base64_encode(force_bytes(portal_user.pk))
        token = default_token_generator.make_token(portal_user)
        activacion_url = f"{settings.FRONTEND_BASE_URL}/registro/{uid}/{token}"

        # Renderizar la plantilla HTML
        context = {
            'nombre_cliente': nombre,
            'cliente_id': str(cliente.id).zfill(4),
            'fecha_registro': cliente.fecha_registro.strftime("%d/%m/%Y"),
            'email_principal': cliente.email_principal,
            'activacion_url': activacion_url,
        }

        html_content = render_to_string('emails/welcome_email.html', context)
        text_content = f"Hola {nombre}, bienvenido a Enfoque Platform. Activa tu cuenta: {activacion_url}"

        # Enviar correo con la versión texto y la versión HTML alternativa
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)

    except Exception as e:
        logger.error(f"Error al enviar el correo de bienvenida al cliente {cliente.email_principal}: {str(e)}")
        # Dependiendo del caso de uso, se podría relanzar la excepción, pero para que no falle la creación de cliente lo capturamos


def enviar_correo_cliente(cliente, asunto, cuerpo, destinatario=None, adjuntos_paths=None):
    """Envía un correo arbitrario al cliente desde el workspace.

    A diferencia de enviar_correo_bienvenida, propaga la excepción: la vista
    que llama registra el intento en CorreoEnviado (ENVIADO o FALLIDO) y
    decide la respuesta HTTP."""
    from django.utils import timezone

    to_email = destinatario or cliente.email_principal
    nombre = getattr(cliente, 'nombre_completo', getattr(cliente, 'razon_social', 'Cliente'))
    context = {
        'nombre_cliente': nombre,
        'asunto': asunto,
        'cuerpo': cuerpo,
        'cliente_id': str(cliente.id).zfill(4),
        'fecha': timezone.localdate().strftime("%d/%m/%Y"),
    }
    html_content = render_to_string('emails/correo_cliente.html', context)
    msg = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, [to_email])
    msg.attach_alternative(html_content, "text/html")
    
    if adjuntos_paths:
        import mimetypes
        for path in adjuntos_paths:
            if os.path.exists(path):
                filename = os.path.basename(path)
                mimetype, _ = mimetypes.guess_type(path)
                mimetype = mimetype or 'application/octet-stream'
                with open(path, 'rb') as f:
                    msg.attach(filename, f.read(), mimetype)
    
    msg.send(fail_silently=False)
    return to_email
