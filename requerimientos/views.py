from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError

from clientes.models import Cliente
from contratos.models import Contrato
from tenants.permissions import IsTenantMember, RequiresFeature
from tenants.scoping import resolve_tenant_for_write, scoped

from .models import PlantillaRequerimiento, Requerimiento, RequerimientoGenerado, EstadoRequerimiento
from .services.generador import generar_documento, GeneracionExistenteError

FEATURE = RequiresFeature('requerimientos')


def resolver_plantilla_activa(tenant, categoria):
    """Plantilla activa del tenant para la categoría, con fallback a la
    plantilla global (tenant=None) — mismo patrón de resolución que
    plantillas.PlantillaDocumento (ver plantillas/services/renderizado.py)."""
    plantilla = PlantillaRequerimiento.objects.filter(
        tenant=tenant, categoria_producto=categoria, activa=True,
    ).first()
    if plantilla is None:
        plantilla = PlantillaRequerimiento.objects.filter(
            tenant__isnull=True, categoria_producto=categoria, activa=True,
        ).first()
    return plantilla


def _plantilla_a_dict(p):
    return {
        'id': p.id,
        'nombre': p.nombre,
        'categoria_producto': p.categoria_producto,
        'secciones': p.secciones,
    }


def _requerimiento_a_dict(r):
    ultimo_doc = r.documentos_generados.order_by('-fecha_generacion').first()
    return {
        'id': r.id,
        'cliente_id': r.cliente_id,
        'contrato_id': r.contrato_id,
        'categoria_producto': r.categoria_producto,
        'plantilla_id': r.plantilla_id,
        'plantilla': _plantilla_a_dict(r.plantilla),
        'respuestas': r.respuestas,
        'estado': r.estado,
        'fecha_creacion': r.fecha_creacion,
        'fecha_actualizacion': r.fecha_actualizacion,
        'fecha_generacion': r.fecha_generacion,
        'documento_generado_id': ultimo_doc.id if ultimo_doc else None,
    }


class PlantillaActivaView(APIView):
    """GET /api/requerimientos/plantillas/?categoria=<Bot|Agente|Script|Software|Auditoría|Consultoría>"""
    permission_classes = [IsTenantMember, FEATURE]

    def get(self, request):
        categoria = request.GET.get('categoria')
        if not categoria:
            raise DRFValidationError({'categoria': 'Este parámetro es requerido.'})
        tenant = request.user.tenant if request.user.tenant_id else None
        plantilla = resolver_plantilla_activa(tenant, categoria)
        if plantilla is None:
            return Response(
                {'error': f"No hay una plantilla de requerimientos activa para la categoría '{categoria}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_plantilla_a_dict(plantilla))


class RequerimientoListCreateView(APIView):
    """
    GET  /api/requerimientos/?cliente=<id>&contrato=<id>
    POST /api/requerimientos/  {cliente_id, contrato_id?, categoria_producto?}

    Si se indica contrato_id, la categoría se deriva de contrato.software.categoria
    (ignora categoria_producto del payload).
    """
    permission_classes = [IsTenantMember, FEATURE]

    def get(self, request):
        qs = scoped(Requerimiento.objects.all(), request, cliente_field='cliente_id') \
            .select_related('plantilla', 'contrato')
        cliente_id = request.GET.get('cliente')
        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)
        contrato_id = request.GET.get('contrato')
        if contrato_id:
            qs = qs.filter(contrato_id=contrato_id)
        return Response([_requerimiento_a_dict(r) for r in qs])

    def post(self, request):
        cliente_id = request.data.get('cliente_id')
        if not cliente_id:
            raise DRFValidationError({'cliente_id': 'Este campo es requerido.'})

        cliente = get_object_or_404(
            scoped(Cliente.objects.all(), request, cliente_field='pk'), pk=cliente_id,
        )

        contrato = None
        contrato_id = request.data.get('contrato_id')
        categoria_producto = request.data.get('categoria_producto')
        if contrato_id:
            contrato = get_object_or_404(
                scoped(Contrato.objects.all(), request, cliente_field='cliente_id').select_related('software'),
                pk=contrato_id,
            )
            if contrato.cliente_id != cliente.pk:
                raise DRFValidationError({'contrato_id': 'El contrato no pertenece a este cliente.'})
            categoria_producto = contrato.software.categoria
        elif not categoria_producto:
            raise DRFValidationError({'categoria_producto': 'Requerido cuando no se indica un contrato.'})

        tenant = resolve_tenant_for_write(request, request.data)
        plantilla = resolver_plantilla_activa(tenant, categoria_producto)
        if plantilla is None:
            return Response(
                {'error': f"No hay una plantilla de requerimientos activa para la categoría '{categoria_producto}'."},
                status=status.HTTP_409_CONFLICT,
            )

        respuestas = request.data.get('respuestas') or {}
        if not isinstance(respuestas, dict):
            raise DRFValidationError({'respuestas': 'Debe ser un objeto {pregunta_id: valor}.'})

        requerimiento = Requerimiento.objects.create(
            tenant=tenant,
            cliente=cliente,
            contrato=contrato,
            categoria_producto=categoria_producto,
            plantilla=plantilla,
            respuestas=respuestas,
            creado_por=request.user,
        )
        return Response(_requerimiento_a_dict(requerimiento), status=status.HTTP_201_CREATED)


class RequerimientoDetailView(APIView):
    """
    GET   /api/requerimientos/<id>/
    PATCH /api/requerimientos/<id>/  {respuestas}  — solo mientras está en BORRADOR
    """
    permission_classes = [IsTenantMember, FEATURE]

    def get(self, request, pk):
        requerimiento = get_object_or_404(
            scoped(Requerimiento.objects.all(), request, cliente_field='cliente_id').select_related('plantilla'),
            pk=pk,
        )
        return Response(_requerimiento_a_dict(requerimiento))

    def patch(self, request, pk):
        requerimiento = get_object_or_404(
            scoped(Requerimiento.objects.all(), request, cliente_field='cliente_id'), pk=pk,
        )
        if requerimiento.estado != EstadoRequerimiento.BORRADOR:
            return Response(
                {'error': 'Este requerimiento ya fue generado y no admite más ediciones.'},
                status=status.HTTP_409_CONFLICT,
            )
        respuestas = request.data.get('respuestas')
        if respuestas is not None:
            if not isinstance(respuestas, dict):
                raise DRFValidationError({'respuestas': 'Debe ser un objeto {pregunta_id: valor}.'})
            requerimiento.respuestas = {**requerimiento.respuestas, **respuestas}
            requerimiento.save(update_fields=['respuestas', 'fecha_actualizacion'])
        return Response(_requerimiento_a_dict(requerimiento))


class GenerarDocumentoView(APIView):
    """POST /api/requerimientos/<id>/generar/  {forzar?}"""
    permission_classes = [IsTenantMember, FEATURE]

    def post(self, request, pk):
        requerimiento = get_object_or_404(
            scoped(Requerimiento.objects.all(), request, cliente_field='cliente_id').select_related('plantilla', 'cliente'),
            pk=pk,
        )
        forzar = str(request.data.get('forzar', 'false')).lower() in ('1', 'true', 'si')
        try:
            documento = generar_documento(requerimiento, usuario=request.user, forzar=forzar)
        except GeneracionExistenteError as exc:
            return Response(
                {'error': str(exc), 'requiere_confirmacion': True},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            'id': documento.id,
            'requerimiento_id': documento.requerimiento_id,
            'hash_sha256': documento.hash_sha256,
            'fecha_generacion': documento.fecha_generacion,
        }, status=status.HTTP_201_CREATED)


class DescargarDocxView(APIView):
    """GET /api/requerimientos/documentos/<id>/docx/"""
    permission_classes = [IsTenantMember, FEATURE]

    def get(self, request, pk):
        documento = get_object_or_404(
            scoped(RequerimientoGenerado.objects.all(), request, 'requerimiento__tenant'), pk=pk,
        )
        return FileResponse(
            documento.archivo_docx.open('rb'), as_attachment=True,
            filename=f"requerimiento_{documento.requerimiento_id}.docx",
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )


class DescargarPDFView(APIView):
    """GET /api/requerimientos/documentos/<id>/pdf/"""
    permission_classes = [IsTenantMember, FEATURE]

    def get(self, request, pk):
        documento = get_object_or_404(
            scoped(RequerimientoGenerado.objects.all(), request, 'requerimiento__tenant'), pk=pk,
        )
        return FileResponse(
            documento.archivo_pdf.open('rb'), as_attachment=True,
            filename=f"requerimiento_{documento.requerimiento_id}.pdf",
            content_type='application/pdf',
        )
