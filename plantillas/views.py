from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.core.exceptions import ValidationError as DjangoValidationError

from contratos.models import Contrato, EtapaContrato
from .models import PlantillaDocumento, DocumentoGenerado
from .services.validacion import validar_docx_subido
from .services.renderizado import (
    generar_documento, resolver_plantilla_activa,
    PlantillaRenderError, VariablesFaltantesError, SinPlantillaActivaError, ConversionPDFError,
)

# Etapas en las que el contrato ya tiene un documento "vigente" — regenerar acá
# requiere confirmación explícita (forzar=true) para no pisar silenciosamente
# un documento ya emitido/firmado.
ETAPAS_CON_DOCUMENTO_EMITIDO = {
    EtapaContrato.PENDIENTE_FIRMA, EtapaContrato.ACTIVO,
    EtapaContrato.ENMENDADO, EtapaContrato.TERMINADO,
}


def _plantilla_a_dict(p: PlantillaDocumento):
    return {
        'id': p.id,
        'nombre': p.nombre,
        'tipo_contrato': p.tipo_contrato,
        'software_id': p.software_id,
        'version_codigo': p.version_codigo,
        'activa': p.activa,
        'fecha_creacion': p.fecha_creacion,
    }


def _documento_a_dict(d: DocumentoGenerado):
    return {
        'id': d.id,
        'contrato_id': d.contrato_id,
        'plantilla_id': d.plantilla_id,
        'plantilla_version': d.plantilla.version_codigo,
        'hash_sha256': d.hash_sha256,
        'fecha_generacion': d.fecha_generacion,
        'generado_por': d.generado_por_id,
    }


class PlantillaListCreateView(APIView):
    """
    GET  /api/plantillas/plantillas/?tipo_contrato=&software=&activa=
    POST /api/plantillas/plantillas/  (multipart: nombre, tipo_contrato, version_codigo,
                                        archivo_docx, software opcional, activa opcional)
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = PlantillaDocumento.objects.all()
        tipo_contrato = request.GET.get('tipo_contrato')
        software_id = request.GET.get('software')
        activa = request.GET.get('activa')
        if tipo_contrato:
            qs = qs.filter(tipo_contrato=tipo_contrato)
        if software_id:
            qs = qs.filter(software_id=software_id)
        if activa is not None:
            qs = qs.filter(activa=activa.lower() in ('1', 'true', 'si'))
        return Response([_plantilla_a_dict(p) for p in qs])

    def post(self, request):
        archivo = request.FILES.get('archivo_docx')
        if not archivo:
            raise DRFValidationError({'archivo_docx': 'Este campo es requerido.'})
        try:
            validar_docx_subido(archivo)
        except DjangoValidationError as exc:
            raise DRFValidationError({'archivo_docx': exc.messages})

        nombre = request.data.get('nombre')
        tipo_contrato = request.data.get('tipo_contrato')
        version_codigo = request.data.get('version_codigo')
        if not (nombre and tipo_contrato and version_codigo):
            raise DRFValidationError('nombre, tipo_contrato y version_codigo son requeridos.')

        software_id = request.data.get('software') or None
        activa = str(request.data.get('activa', 'true')).lower() in ('1', 'true', 'si')

        plantilla = PlantillaDocumento.objects.create(
            nombre=nombre,
            tipo_contrato=tipo_contrato,
            software_id=software_id,
            archivo_docx=archivo,
            version_codigo=version_codigo,
            activa=activa,
            subida_por=request.user,
        )
        return Response(_plantilla_a_dict(plantilla), status=status.HTTP_201_CREATED)


class PlantillaDetailView(APIView):
    """
    GET   /api/plantillas/plantillas/<id>/
    PATCH /api/plantillas/plantillas/<id>/   (solo permite alternar {"activa": bool};
                                               para cambiar el .docx se sube una plantilla nueva)
    """
    permission_classes = [IsAdminUser]

    def get(self, request, pk):
        plantilla = get_object_or_404(PlantillaDocumento, pk=pk)
        return Response(_plantilla_a_dict(plantilla))

    def patch(self, request, pk):
        plantilla = get_object_or_404(PlantillaDocumento, pk=pk)
        if 'activa' not in request.data:
            raise DRFValidationError('Solo se admite actualizar el campo "activa".')
        plantilla.activa = str(request.data.get('activa')).lower() in ('1', 'true', 'si')
        plantilla.save()
        return Response(_plantilla_a_dict(plantilla))


class DocumentoGeneradoListView(APIView):
    """GET /api/plantillas/documentos/?contrato_id=<id> — historial de un contrato."""

    def get(self, request):
        contrato_id = request.GET.get('contrato_id')
        if not contrato_id:
            raise DRFValidationError({'contrato_id': 'Este parámetro es requerido.'})
        qs = DocumentoGenerado.objects.filter(contrato_id=contrato_id).select_related('plantilla')
        return Response([_documento_a_dict(d) for d in qs])


class GenerarDocumentoView(APIView):
    """POST /api/plantillas/documentos/generar/  {contrato_id, plantilla_id?, forzar?}"""

    def post(self, request):
        contrato_id = request.data.get('contrato_id')
        if not contrato_id:
            raise DRFValidationError({'contrato_id': 'Este campo es requerido.'})

        contrato = get_object_or_404(
            Contrato.objects.select_related('cliente', 'software', 'sla'), pk=contrato_id,
        )

        forzar = str(request.data.get('forzar', 'false')).lower() in ('1', 'true', 'si')
        if contrato.etapa in ETAPAS_CON_DOCUMENTO_EMITIDO and not forzar:
            return Response(
                {
                    'error': (
                        f"Este contrato ya está en etapa '{contrato.get_etapa_display()}'. "
                        "¿Confirmas generar una nueva versión del documento? "
                        "Esto no elimina la versión anterior."
                    ),
                    'requiere_confirmacion': True,
                },
                status=status.HTTP_409_CONFLICT,
            )

        plantilla = None
        plantilla_id = request.data.get('plantilla_id')
        if plantilla_id:
            plantilla = get_object_or_404(PlantillaDocumento, pk=plantilla_id)

        try:
            documento = generar_documento(contrato, plantilla=plantilla, usuario=request.user)
        except SinPlantillaActivaError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        except VariablesFaltantesError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except ConversionPDFError:
            return Response(
                {'error': 'No se pudo generar el PDF del documento. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except PlantillaRenderError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        return Response(_documento_a_dict(documento), status=status.HTTP_201_CREATED)


class DescargarPDFView(APIView):
    """GET /api/plantillas/documentos/<id>/pdf/ — el único archivo pensado para descarga externa."""

    def get(self, request, pk):
        documento = get_object_or_404(DocumentoGenerado, pk=pk)
        return FileResponse(
            documento.archivo_pdf.open('rb'), as_attachment=True,
            filename=f"contrato_{documento.contrato_id}.pdf", content_type='application/pdf',
        )


class DescargarDocxView(APIView):
    """GET /api/plantillas/documentos/<id>/docx/ — solo staff, para auditoría interna."""
    permission_classes = [IsAdminUser]

    def get(self, request, pk):
        documento = get_object_or_404(DocumentoGenerado, pk=pk)
        return FileResponse(
            documento.archivo_docx.open('rb'), as_attachment=True,
            filename=f"contrato_{documento.contrato_id}_interno.docx",
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
