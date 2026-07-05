"""Construye el diccionario de variables Jinja2 disponible para las plantillas
docxtpl a partir de un Contrato. Namespaces expuestos: cliente, software, sla,
contrato, terminos_legales, fecha_generacion.
"""
from datetime import date

from django.utils.html import strip_tags

from legal.models import DocumentoLegal


def _contexto_cliente(cliente):
    """Resuelve la herencia multi-tabla PersonaNatural/PersonaJuridica de forma
    transparente para quien escribe la plantilla en Word."""
    persona_juridica = getattr(cliente, 'personajuridica', None)
    persona_natural = getattr(cliente, 'personanatural', None)

    if persona_juridica:
        nombre = persona_juridica.razon_social
        identificador = persona_juridica.rut
        tipo_persona = 'juridica'
    elif persona_natural:
        nombre = persona_natural.nombre_completo
        identificador = persona_natural.run
        tipo_persona = 'natural'
    else:
        nombre = str(cliente)
        identificador = None
        tipo_persona = None

    return {
        'nombre': nombre,
        'identificador': identificador,
        'tipo_persona': tipo_persona,
        'email': cliente.email_principal,
        'telefono': cliente.telefono_contacto or '',
    }


def _contexto_terminos_legales():
    """Todos los DocumentoLegal vigentes (is_vigente=True), como texto plano
    (contenido_html -> strip_tags) porque el destino es un .docx, no HTML.
    Preservar formato rich-text queda para una fase futura."""
    vigentes = list(
        DocumentoLegal.objects.filter(is_vigente=True).order_by('-fecha_publicacion')
    )
    lista = [
        {
            'tipo': doc.tipo,
            'version_codigo': doc.version_codigo,
            'texto_plano': strip_tags(doc.contenido_html),
            'fecha_publicacion': doc.fecha_publicacion,
        }
        for doc in vigentes
    ]
    return {
        'lista': lista,
        'principal': lista[0] if lista else None,
    }


def construir_contexto(contrato):
    """contrato: instancia de contratos.models.Contrato (con cliente/software/sla
    ya asignados; se recomienda haberlo cargado con select_related)."""
    sla = contrato.sla
    software = contrato.software

    return {
        'cliente': _contexto_cliente(contrato.cliente),
        'software': {
            'nombre': software.nombre,
            'slug': software.slug,
            'descripcion': software.descripcion or '',
        },
        'sla': {
            'nombre': sla.nombre,
            'uptime_garantizado': sla.uptime_garantizado,
            'tiempo_respuesta_horas': sla.tiempo_respuesta_horas,
            'detalles': sla.detalles or '',
        },
        'contrato': {
            'id': contrato.id,
            'tipo_contrato': contrato.tipo_contrato,
            'tipo_contrato_display': contrato.get_tipo_contrato_display(),
            'status': contrato.status,
            'status_display': contrato.get_status_display(),
            'etapa': contrato.etapa,
            'etapa_display': contrato.get_etapa_display(),
            'monto': contrato.monto,
            'monto_formateado': f"${float(contrato.monto):,.4f}",
            'fecha_inicio': contrato.fecha_inicio,
            'fecha_vencimiento': contrato.fecha_vencimiento,
            'dias_gracia_autorizados': contrato.dias_gracia_autorizados,
        },
        'terminos_legales': _contexto_terminos_legales(),
        'fecha_generacion': date.today(),
    }
