import os
import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings

logger = logging.getLogger(__name__)

def enviar_correo_bienvenida(cliente):
    """
    Envía un correo de bienvenida al cliente recién creado.
    """
    try:
        subject = "¡Bienvenido a Enfoque Platform!"
        from_email = settings.DEFAULT_FROM_EMAIL
        to_email = cliente.email_principal
        
        # Obtener el nombre según el tipo de cliente
        nombre = getattr(cliente, 'nombre_completo', getattr(cliente, 'razon_social', 'Cliente'))
        
        # Renderizar la plantilla HTML
        context = {
            'nombre_cliente': nombre,
            'cliente_id': str(cliente.id).zfill(4),
            'fecha_registro': cliente.fecha_registro.strftime("%d/%m/%Y"),
            'email_principal': cliente.email_principal,
        }
        
        html_content = render_to_string('emails/welcome_email.html', context)
        text_content = f"Hola {nombre}, bienvenido a Enfoque Platform. Tu cuenta ha sido registrada con éxito."
        
        # Enviar correo con la versión texto y la versión HTML alternativa
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        
    except Exception as e:
        logger.error(f"Error al enviar el correo de bienvenida al cliente {cliente.email_principal}: {str(e)}")
        # Dependiendo del caso de uso, se podría relanzar la excepción, pero para que no falle la creación de cliente lo capturamos


def enviar_correo_cliente(cliente, asunto, cuerpo, destinatario=None):
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
    msg.send(fail_silently=False)
    return to_email
