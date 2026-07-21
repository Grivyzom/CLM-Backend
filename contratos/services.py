import io
import secrets
import uuid

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from .models import Contrato, EtapaContrato, HistorialEtapaContrato, RegistroPerdonazo, TokenFirmaContrato

def registrar_extension_contrato(contrato_id, dias_a_extender, motivo_texto):
    """
    Registra una extensión de contrato usando bloqueo pesimista
    (SELECT FOR UPDATE) para evitar race conditions.
    """
    with transaction.atomic():
        contrato = Contrato.objects.select_for_update().get(id=contrato_id)
        
        vencimiento_anterior = contrato.fecha_vencimiento
        
        contrato.dias_gracia_autorizados += dias_a_extender
        if contrato.fecha_vencimiento:
            contrato.fecha_vencimiento += timedelta(days=dias_a_extender)
        if contrato.fin_periodo_gracia:
            contrato.fin_periodo_gracia += timedelta(days=dias_a_extender)
            
        contrato.save()
        
        RegistroPerdonazo.objects.create(
            contrato=contrato,
            dias_extendidos=dias_a_extender,
            motivo=motivo_texto,
            fecha_vencimiento_anterior=vencimiento_anterior
        )


# ---------------------------------------------------------------------------
# Firma electrónica OTP real: magic-link por correo + Certificado de Firma
# anexado al PDF del documento generado.
# ---------------------------------------------------------------------------

CERTIFICADO_FIRMA_TEMPLATE = 'Certificado de Firma.dc.html'


def _fecha_hora_larga_es(dt) -> str:
    """'16 de julio de 2026, 09:32 hrs' -- LANGUAGE_CODE del proyecto es en-us,
    por lo que un datetime sin formatear en el template saldría en inglés."""
    if dt is None:
        return ''
    from plantillas.services.html_doc import fecha_larga_es
    return f"{fecha_larga_es(dt.date())}, {timezone.localtime(dt).strftime('%H:%M')} hrs"


class FirmaElectronicaError(Exception):
    """Error de validación de negocio en el flujo de firma electrónica OTP
    (no un bug: se traduce a un 400 con mensaje claro para el usuario)."""


def enviar_firma_electronica(contrato: Contrato, usuario=None) -> TokenFirmaContrato:
    """Envía el magic-link de firma OTP por correo real al cliente del contrato.

    Falla (sin dejar estado a medias, todo dentro de una transacción) si no
    hay un DocumentoGenerado para el contrato, si el email del cliente no es
    válido, o si el envío del correo falla."""
    from django.conf import settings
    from django.core.mail import send_mail
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    documento = contrato.documentos_generados.order_by('-fecha_generacion').first()
    if documento is None:
        raise FirmaElectronicaError('Genera el documento del contrato antes de enviarlo a firmar.')

    email = (contrato.cliente.email_principal or '').strip()
    try:
        validate_email(email)
    except ValidationError:
        raise FirmaElectronicaError('El cliente no tiene un correo electrónico válido registrado.')

    # Cooldown anti-spam: evita que un reenvío manual repetido o la
    # regeneración automática del documento (ver sincronizar_firma_tras_regeneracion)
    # bombardeen el correo del cliente con enlaces de firma.
    if contrato.firma_fecha_envio:
        segundos_desde_envio = (timezone.now() - contrato.firma_fecha_envio).total_seconds()
        restante = settings.FIRMA_REENVIO_COOLDOWN_SEGUNDOS - segundos_desde_envio
        if restante > 0:
            raise FirmaElectronicaError(
                f'Ya se envió un enlace de firma hace poco. Espera {int(restante) + 1} segundos antes de reenviar.'
            )

    with transaction.atomic():
        # Un link viejo reenviado por error no debe seguir siendo válido en paralelo.
        TokenFirmaContrato.objects.filter(
            contrato=contrato, fecha_uso__isnull=True, fecha_expiracion__gt=timezone.now(),
        ).update(fecha_expiracion=timezone.now())

        tok = TokenFirmaContrato.objects.create(
            contrato=contrato,
            token=secrets.token_urlsafe(32),
            fecha_expiracion=timezone.now() + timedelta(days=settings.FIRMA_TOKEN_EXPIRATION_DAYS),
        )

        url = f"{settings.FRONTEND_BASE_URL}/firmar/{tok.token}"
        nombre_contrato = contrato.nombre or f"Contrato #{contrato.id}"
        asunto = f"Firma electrónica pendiente — {nombre_contrato}"
        cuerpo = (
            f"Hola,\n\n"
            f"Se ha solicitado tu firma electrónica para el contrato \"{nombre_contrato}\" con Grivyzom.\n\n"
            f"Para revisar el documento y confirmar tu firma, ingresa al siguiente enlace seguro:\n"
            f"{url}\n\n"
            f"Este enlace es válido por {settings.FIRMA_TOKEN_EXPIRATION_DAYS} días y solo puede usarse una vez.\n\n"
            f"Si no esperabas este correo, puedes ignorarlo.\n"
        )
        try:
            send_mail(asunto, cuerpo, None, [email])
        except Exception as exc:
            raise FirmaElectronicaError(f'No se pudo enviar el correo de firma: {exc}') from exc

        contrato.firma_proveedor = 'OTP'
        contrato.firma_status = 'PENDING'
        contrato.firma_envelope_id = str(uuid.uuid4())
        contrato.firma_fecha_envio = timezone.now()
        # Guardar TODOS los campos antes de transicionar_etapa: transicionar_etapa
        # hace self.save(update_fields=['etapa']), que no persistiría lo de arriba.
        contrato.save()

        if contrato.etapa != EtapaContrato.PENDIENTE_FIRMA:
            contrato.transicionar_etapa(
                EtapaContrato.PENDIENTE_FIRMA, usuario=usuario,
                notas="Enviado para firma electrónica vía OTP (enlace seguro por correo).",
            )
        else:
            HistorialEtapaContrato.objects.create(
                contrato=contrato, etapa_anterior=contrato.etapa, etapa_nueva=contrato.etapa,
                usuario=usuario,
                notas=f"Sobre de firma electrónica OTP reiniciado (Envelope ID: {contrato.firma_envelope_id}).",
            )

    return tok


def sincronizar_firma_tras_regeneracion(contrato: Contrato, usuario=None):
    """Se llama después de generar_documento() para que la regeneración del
    documento nunca quede ciega al estado de firma electrónica en curso.

    - SIGNED: el documento firmado ya no representa el contenido vigente del
      contrato -> se invalida la firma (contrato vuelve a APROBADO, hay que
      reenviarlo a firma de nuevo). El hash del nuevo DocumentoGenerado ya
      salió recalculado solo (generar_documento lo hace siempre).
    - PENDING de OTP (magic-link vivo): el enlace que tiene el cliente apunta
      a contenido desactualizado -> se invalida y se reenvía uno nuevo al
      mismo correo automáticamente (sujeto al cooldown de
      enviar_firma_electronica, así que un loop de regeneraciones no spamea
      el correo del cliente).
    - Cualquier otro estado (NONE/DECLINED, o PENDING de DOCUSIGN/ADOBE, que
      no tienen reenvío automático real): no se toca nada.

    Devuelve un mensaje para mostrarle al staff en el frontend, o None si no
    había ninguna firma en curso que sincronizar."""
    if contrato.firma_status == 'SIGNED':
        contrato.firma_status = 'NONE'
        contrato.firma_proveedor = 'NONE'
        contrato.firma_envelope_id = None
        contrato.firma_fecha_envio = None
        contrato.firma_fecha_firma = None
        contrato.firma_documento_firmado.delete(save=False)
        contrato.save()
        contrato.transicionar_etapa(
            EtapaContrato.APROBADO, usuario=usuario,
            notas="Documento regenerado: el contenido cambió, la firma electrónica anterior quedó invalidada.",
        )
        return 'El contrato estaba firmado: al regenerar el documento la firma quedó invalidada y el contrato volvió a "Aprobado". Debes reenviarlo a firma.'

    if contrato.firma_status == 'PENDING' and contrato.firma_proveedor == 'OTP':
        # Invalidar el enlace viejo YA, sin importar si el reenvío logra salir:
        # ese link apunta al documento anterior, y firmarlo firmaría contenido
        # desactualizado. Peor tener el link roto un rato que firmar lo viejo.
        TokenFirmaContrato.objects.filter(
            contrato=contrato, fecha_uso__isnull=True,
        ).update(fecha_expiracion=timezone.now())
        try:
            enviar_firma_electronica(contrato, usuario=usuario)
        except FirmaElectronicaError as exc:
            return (
                'El enlace de firma anterior quedó invalidado (apuntaba a una versión anterior del '
                f'documento), pero no se pudo reenviar uno nuevo automáticamente: {exc} Reenvíalo manualmente.'
            )
        return 'El enlace de firma anterior quedó invalidado; se envió uno nuevo al correo del cliente con el documento actualizado.'

    return None


def merge_pdfs(pdf_contenido: bytes, pdf_certificado: bytes) -> bytes:
    """Concatena el PDF del documento con la página de certificado al final."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for datos in (pdf_contenido, pdf_certificado):
        reader = PdfReader(io.BytesIO(datos))
        for page in reader.pages:
            writer.add_page(page)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def confirmar_firma_electronica(token: str, request) -> bytes:
    """Ejecuta la confirmación del magic-link: valida el token, genera el
    Certificado de Firma con los datos reales de esta confirmación, lo anexa
    al PDF del documento generado del contrato, marca el contrato como
    firmado/ACTIVO y devuelve los bytes del PDF final (para que el firmante
    externo lo descargue en la misma respuesta)."""
    from documentos.services.auditoria import get_client_ip
    from django.template import engines
    from plantillas.services.html_doc import cargar_plantilla_html, html_a_pdf, siguiente_referencia

    with transaction.atomic():
        tok = TokenFirmaContrato.objects.select_for_update().select_related(
            'contrato', 'contrato__cliente', 'contrato__tenant',
        ).filter(token=token).first()
        if tok is None or not tok.vigente():
            raise FirmaElectronicaError('El enlace de firma es inválido o ya expiró.')

        contrato = tok.contrato
        if contrato.firma_status != 'PENDING':
            raise FirmaElectronicaError('Este contrato ya no está pendiente de firma.')

        documento = contrato.documentos_generados.order_by('-fecha_generacion').first()
        if documento is None:
            raise FirmaElectronicaError('No hay documento generado para este contrato.')

        ip = get_client_ip(request)
        folio = siguiente_referencia(contrato.tenant, 'FIRMA')
        fecha_confirmacion = timezone.now()
        firmante = str(contrato.cliente)

        contexto = {
            'folio': folio,
            'document_id': documento.id,
            'firmante': firmante,
            'email': contrato.cliente.email_principal,
            'fecha_envio': _fecha_hora_larga_es(contrato.firma_fecha_envio),
            'fecha_confirmacion': _fecha_hora_larga_es(fecha_confirmacion),
            'ip': ip,
            'hash': documento.hash_sha256,
        }
        html_cert = cargar_plantilla_html(CERTIFICADO_FIRMA_TEMPLATE)
        html_cert_render = engines['django'].from_string(html_cert).render(contexto)
        pdf_cert = html_a_pdf(html_cert_render)

        with documento.archivo_pdf.open('rb') as f:
            pdf_contenido = f.read()
        pdf_final = merge_pdfs(pdf_contenido, pdf_cert)

        contrato.firma_status = 'SIGNED'
        contrato.firma_fecha_firma = fecha_confirmacion
        contrato.firma_documento_firmado.save(
            f"contrato_{contrato.id}_firmado.pdf", ContentFile(pdf_final), save=False,
        )
        # Igual que en enviar_firma_electronica: guardar todos los campos ANTES
        # de transicionar_etapa (que solo persiste la columna 'etapa').
        contrato.save()
        contrato.transicionar_etapa(
            EtapaContrato.ACTIVO, usuario=None,
            notas=f"Firmado electrónicamente por {firmante} vía enlace seguro (folio {folio}), IP {ip}.",
        )

        tok.fecha_uso = fecha_confirmacion
        tok.ip_confirmacion = ip
        tok.save(update_fields=['fecha_uso', 'ip_confirmacion'])

    return pdf_final
