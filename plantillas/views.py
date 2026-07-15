from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.conf import settings
from pathlib import Path
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from contratos.models import Contrato, EtapaContrato
from catalogo.models import Producto
from tenants.permissions import DeleteRequiresTenantAdmin, IsTenantMember, RequiresFeature
from tenants.scoping import resolve_tenant_for_write, scoped
from .models import PlantillaDocumento, DocumentoGenerado, Clausula, VersionClausula, ModoOrigenPlantilla
from .services.validacion import validar_docx_subido
from .services.renderizado import (
    generar_documento, resolver_plantilla_activa, obtener_preview_pdf,
    PlantillaRenderError, VariablesFaltantesError, SinPlantillaActivaError, ConversionPDFError,
)

# Etapas en las que el contrato ya tiene un documento "vigente" — regenerar acá
# requiere confirmación explícita (forzar=true) para no pisar silenciosamente
# un documento ya emitido/firmado.
ETAPAS_CON_DOCUMENTO_EMITIDO = {
    EtapaContrato.PENDIENTE_FIRMA, EtapaContrato.ACTIVO,
    EtapaContrato.ENMENDADO, EtapaContrato.TERMINADO,
}


def _available_html_templates():
    """Lista de rutas relativas bajo templates/plantillas_html/*.html.

    Única fuente de verdad para el dropdown del frontend (AvailableHtmlTemplatesView)
    y para validar server-side que `ruta_plantilla_html` no apunte a un template
    Django fuera de esa carpeta (render_to_string resuelve contra todos los
    directorios de templates del proyecto, no solo este)."""
    import os

    base_dir = Path(settings.BASE_DIR) / 'templates' / 'plantillas_html'
    templates = []
    if base_dir.exists():
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.endswith('.html'):
                    rel_path = os.path.relpath(os.path.join(root, f), base_dir)
                    rel_path = rel_path.replace('\\', '/')
                    templates.append(f'plantillas_html/{rel_path}')
    return templates


def _plantilla_a_dict(p: PlantillaDocumento):
    from .models import DocumentoGenerado
    usos = DocumentoGenerado.objects.filter(plantilla=p).count()
    return {
        'id': p.id,
        'nombre': p.nombre,
        'tipo_contrato': p.tipo_contrato,
        'tipo_contrato_display': dict(p._meta.get_field('tipo_contrato').choices or {}).get(p.tipo_contrato, p.tipo_contrato),
        'software_id': p.software_id,
        'software_nombre': p.software.nombre if p.software_id else None,
        'modo_origen': p.modo_origen,
        'modo_origen_display': p.get_modo_origen_display(),
        'version_codigo': p.version_codigo,
        'activa': p.activa,
        'fecha_creacion': p.fecha_creacion,
        'usos': usos,
        'clausulas_seleccionadas': list(p.clausulas_seleccionadas.values_list('id', flat=True)),
        'ruta_plantilla_html': p.ruta_plantilla_html if p.modo_origen == 'html' else None,
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

    Listar es para cualquier miembro del tenant (el modal de Nuevo Contrato
    necesita las plantillas); registrar/modificar queda reservado al
    Administrador de Cuenta del tenant (o superadmin).
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        qs = scoped(PlantillaDocumento.objects.all(), request).select_related('software')
        tipo_contrato = request.GET.get('tipo_contrato')
        software_id = request.GET.get('software')
        activa = request.GET.get('activa')
        modo_origen = request.GET.get('modo_origen')
        incluir_globales = request.GET.get('incluir_globales', '').lower() in ('1', 'true', 'si')
        if tipo_contrato:
            qs = qs.filter(tipo_contrato=tipo_contrato)
        if software_id:
            # `incluir_globales` suma las plantillas sin software asignado, que el
            # motor de renderizado usa como fallback (resolver_plantilla_activa).
            if incluir_globales:
                qs = qs.filter(Q(software_id=software_id) | Q(software__isnull=True))
            else:
                qs = qs.filter(software_id=software_id)
        if activa is not None:
            qs = qs.filter(activa=activa.lower() in ('1', 'true', 'si'))
        if modo_origen:
            qs = qs.filter(modo_origen=modo_origen)
        return Response([_plantilla_a_dict(p) for p in qs])

    def post(self, request):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            return Response(
                {'error': 'Solo el Administrador de Cuenta puede registrar plantillas.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        nombre = request.data.get('nombre')
        tipo_contrato = request.data.get('tipo_contrato')
        version_codigo = request.data.get('version_codigo')
        software_id = request.data.get('software') or None
        modo_origen = request.data.get('modo_origen', ModoOrigenPlantilla.ARCHIVO)

        errors = {}
        if not nombre:
            errors['nombre'] = 'Este campo es requerido.'
        if not tipo_contrato:
            errors['tipo_contrato'] = 'Este campo es requerido.'
        if not version_codigo:
            errors['version_codigo'] = 'Este campo es requerido.'
        if not software_id:
            errors['software'] = 'Debe especificar a qué software/producto pertenece esta plantilla.'
        if modo_origen not in ModoOrigenPlantilla.values:
            errors['modo_origen'] = f'Modo inválido. Opciones: {list(ModoOrigenPlantilla.values)}'
        if errors:
            raise DRFValidationError(errors)

        archivo = request.FILES.get('archivo_docx')
        ruta_plantilla_html = request.data.get('ruta_plantilla_html')
        
        if modo_origen == ModoOrigenPlantilla.ARCHIVO:
            if not archivo:
                raise DRFValidationError({'archivo_docx': 'Se requiere un archivo .docx para el modo "archivo".'})
            try:
                validar_docx_subido(archivo)
            except DjangoValidationError as exc:
                raise DRFValidationError({'archivo_docx': exc.messages})
        elif modo_origen == ModoOrigenPlantilla.HTML:
            if not ruta_plantilla_html or not str(ruta_plantilla_html).strip():
                raise DRFValidationError({'ruta_plantilla_html': 'Se requiere seleccionar una ruta de plantilla HTML.'})
            if ruta_plantilla_html not in _available_html_templates():
                raise DRFValidationError({'ruta_plantilla_html': 'Ruta de plantilla HTML no reconocida.'})

        tenant = resolve_tenant_for_write(request, request.data)
        if software_id and not Producto.objects.filter(pk=software_id, tenant=tenant).exists():
            raise DRFValidationError({'software': 'Producto no encontrado.'})

        activa = str(request.data.get('activa', 'true')).lower() in ('1', 'true', 'si')

        plantilla = PlantillaDocumento.objects.create(
            tenant=tenant,
            nombre=nombre,
            tipo_contrato=tipo_contrato,
            software_id=software_id,
            modo_origen=modo_origen,
            archivo_docx=archivo if modo_origen == ModoOrigenPlantilla.ARCHIVO else None,
            ruta_plantilla_html=ruta_plantilla_html if modo_origen == ModoOrigenPlantilla.HTML else None,
            version_codigo=version_codigo,
            activa=activa,
            subida_por=request.user,
        )

        clausulas_str = request.data.get('clausulas_seleccionadas')
        if clausulas_str:
            import json
            try:
                clausulas_ids = json.loads(clausulas_str)
                plantilla.clausulas_seleccionadas.set(clausulas_ids)
            except ValueError:
                pass

        return Response(_plantilla_a_dict(plantilla), status=status.HTTP_201_CREATED)


class PlantillaDetailView(APIView):
    """
    GET   /api/plantillas/plantillas/<id>/
    PATCH /api/plantillas/plantillas/<id>/   (solo permite alternar {"activa": bool};
                                               para cambiar el .docx se sube una plantilla nueva)
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        return Response(_plantilla_a_dict(plantilla))

    def patch(self, request, pk):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            return Response(
                {'error': 'Solo el Administrador de Cuenta puede modificar plantillas.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)

        if 'activa' in request.data:
            plantilla.activa = str(request.data.get('activa')).lower() in ('1', 'true', 'si')
            
        if 'nombre' in request.data:
            plantilla.nombre = request.data.get('nombre')
        if 'tipo_contrato' in request.data:
            plantilla.tipo_contrato = request.data.get('tipo_contrato')
        if 'version_codigo' in request.data:
            plantilla.version_codigo = request.data.get('version_codigo')
        if 'software' in request.data:
            software_id = request.data.get('software')
            plantilla.software_id = software_id if software_id else None
        if 'modo_origen' in request.data:
            plantilla.modo_origen = request.data.get('modo_origen')
            
        archivo = request.FILES.get('archivo_docx')
        if archivo:
            if plantilla.modo_origen == ModoOrigenPlantilla.ARCHIVO:
                try:
                    validar_docx_subido(archivo)
                except DjangoValidationError as exc:
                    raise DRFValidationError({'archivo_docx': exc.messages})
                plantilla.archivo_docx = archivo

        if 'ruta_plantilla_html' in request.data:
            nueva_ruta = request.data.get('ruta_plantilla_html')
            if plantilla.modo_origen == ModoOrigenPlantilla.HTML:
                if not str(nueva_ruta).strip():
                    raise DRFValidationError({'ruta_plantilla_html': 'Se requiere seleccionar una ruta de plantilla HTML.'})
                if nueva_ruta not in _available_html_templates():
                    raise DRFValidationError({'ruta_plantilla_html': 'Ruta de plantilla HTML no reconocida.'})
            plantilla.ruta_plantilla_html = nueva_ruta

        clausulas_str = request.data.get('clausulas_seleccionadas')
        if clausulas_str is not None:
            import json
            try:
                clausulas_ids = json.loads(clausulas_str)
                plantilla.clausulas_seleccionadas.set(clausulas_ids)
            except ValueError:
                pass

        plantilla.save()
        return Response(_plantilla_a_dict(plantilla))


class PlantillaPreviewPDFView(APIView):
    """GET /api/plantillas/plantillas/<id>/preview-pdf/

    Sirve un PDF de la plantilla original (con sus variables sin resolver)
    para previsualizarla embebida en el catálogo. Solo aplica a plantillas
    modo 'archivo'; las de cláusulas no tienen documento base.
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        if plantilla.modo_origen == ModoOrigenPlantilla.ARCHIVO and not plantilla.archivo_docx:
            return Response(
                {'error': 'Esta plantilla en modo archivo no tiene un documento base (.docx) para previsualizar.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            pdf_path = obtener_preview_pdf(plantilla)
        except ConversionPDFError:
            return Response(
                {'error': 'No se pudo generar la previsualización PDF de la plantilla. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        response = FileResponse(
            open(pdf_path, 'rb'), as_attachment=False,
            filename=f"plantilla_{plantilla.id}_preview.pdf", content_type='application/pdf',
        )
        # Igual que en DescargarPDFView inline: el middleware pondría DENY.
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response


class PlantillaRegenerarPreviewView(APIView):
    """POST /api/plantillas/plantillas/<id>/regenerar-preview/"""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def post(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        
        cache_dir = Path(settings.MEDIA_ROOT) / 'plantillas_previews'
        if cache_dir.exists():
            for f in cache_dir.glob(f"plantilla_{plantilla.id}_*.pdf"):
                try:
                    f.unlink()
                except Exception:
                    pass
                    
        return Response({'status': 'ok'})


class AvailableHtmlTemplatesView(APIView):
    """GET /api/plantillas/html-templates/"""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        return Response(_available_html_templates())


class DocumentoGeneradoListView(APIView):
    """GET /api/plantillas/documentos/?contrato_id=<id> — historial de un contrato."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        contrato_id = request.GET.get('contrato_id')
        if not contrato_id:
            raise DRFValidationError({'contrato_id': 'Este parámetro es requerido.'})
        qs = scoped(DocumentoGenerado.objects.all(), request, 'contrato__tenant') \
            .filter(contrato_id=contrato_id).select_related('plantilla')
        return Response([_documento_a_dict(d) for d in qs])


class GenerarDocumentoView(APIView):
    """POST /api/plantillas/documentos/generar/  {contrato_id, plantilla_id?, forzar?}"""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def post(self, request):
        contrato_id = request.data.get('contrato_id')
        if not contrato_id:
            raise DRFValidationError({'contrato_id': 'Este campo es requerido.'})

        contrato = get_object_or_404(
            scoped(Contrato.objects.all(), request).select_related('cliente', 'software', 'sla'),
            pk=contrato_id,
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
            # La plantilla debe pertenecer al mismo tenant que el contrato.
            plantilla = get_object_or_404(
                PlantillaDocumento.objects.filter(tenant=contrato.tenant), pk=plantilla_id,
            )

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
    """GET /api/plantillas/documentos/<id>/pdf/ — el único archivo pensado para descarga externa.

    Con `?inline=1` se sirve sin Content-Disposition: attachment, para
    previsualizarlo embebido (iframe) en el workspace del contrato.
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        documento = get_object_or_404(
            scoped(DocumentoGenerado.objects.all(), request, 'contrato__tenant'), pk=pk,
        )
        inline = request.GET.get('inline', '').lower() in ('1', 'true', 'si')
        response = FileResponse(
            documento.archivo_pdf.open('rb'), as_attachment=not inline,
            filename=f"contrato_{documento.contrato_id}.pdf", content_type='application/pdf',
        )
        if inline:
            # XFrameOptionsMiddleware pone DENY por defecto y rompería el iframe;
            # SAMEORIGIN limita el embebido al propio CLM.
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response


class DescargarDocxView(APIView):
    """GET /api/plantillas/documentos/<id>/docx/ — auditoría interna: superadmin,
    Administrador de Cuenta o Auditor Legal del tenant."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        user = request.user
        if user.tenant_id is not None and not (user.is_tenant_admin or user.is_auditor):
            return Response(
                {'error': 'Requiere rol Administrador de Cuenta o Auditor Legal.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        documento = get_object_or_404(
            scoped(DocumentoGenerado.objects.all(), request, 'contrato__tenant'), pk=pk,
        )
        return FileResponse(
            documento.archivo_docx.open('rb'), as_attachment=True,
            filename=f"contrato_{documento.contrato_id}_interno.docx",
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )

class ClausulaListView(APIView):
    """
    GET /api/plantillas/clausulas/
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        qs = scoped(Clausula.objects.all(), request).prefetch_related('versiones').filter(activa=True)
        data = []
        for c in qs:
            versions = [
                {
                    'id': v.id,
                    'etiqueta': v.etiqueta,
                    'tipo': v.tipo,
                    'texto': v.texto,
                } for v in c.versiones.all() if v.activa
            ]
            data.append({
                'id': c.id,
                'cat': c.categoria,
                'name': c.nombre,
                'risk': c.riesgo,
                'versions': versions
            })
        return Response(data)

    def post(self, request):
        data = request.data
        tenant = resolve_tenant_for_write(request, data)
        try:
            with transaction.atomic():
                clausula = Clausula.objects.create(
                    tenant=tenant,
                    categoria=data.get('cat', 'General'),
                    nombre=data.get('name', 'Nueva Cláusula'),
                    riesgo=data.get('risk', 'Medio'),
                    activa=True
                )
                
                versions_data = data.get('versions', [])
                for v_data in versions_data:
                    VersionClausula.objects.create(
                        clausula=clausula,
                        etiqueta=v_data.get('label', 'Estándar'),
                        tipo=v_data.get('tag', 'Estándar'),
                        texto=v_data.get('text', '')
                    )
            
            return Response({'status': 'ok', 'id': clausula.id}, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ClausulaDetailView(APIView):
    """
    PUT /api/plantillas/clausulas/<pk>/
    DELETE /api/plantillas/clausulas/<pk>/
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas'), DeleteRequiresTenantAdmin]

    def put(self, request, pk):
        clausula = get_object_or_404(scoped(Clausula.objects.all(), request), pk=pk)
        data = request.data
        try:
            with transaction.atomic():
                clausula.categoria = data.get('cat', clausula.categoria)
                clausula.nombre = data.get('name', clausula.nombre)
                clausula.riesgo = data.get('risk', clausula.riesgo)
                clausula.save()

                active_versions = {v.id: v for v in clausula.versiones.filter(activa=True)}
                processed_ids = set()
                
                versions_data = data.get('versions', [])
                for v_data in versions_data:
                    v_id = v_data.get('id')
                    label = v_data.get('label', 'Estándar')
                    tag = v_data.get('tag', 'Estándar')
                    text = v_data.get('text', '')
                    
                    if v_id and v_id in active_versions:
                        existing = active_versions[v_id]
                        processed_ids.add(v_id)
                        
                        if existing.etiqueta != label or existing.tipo != tag or existing.texto != text:
                            existing.activa = False
                            existing.save()
                            VersionClausula.objects.create(
                                clausula=clausula,
                                etiqueta=label,
                                tipo=tag,
                                texto=text
                            )
                    else:
                        VersionClausula.objects.create(
                            clausula=clausula,
                            etiqueta=label,
                            tipo=tag,
                            texto=text
                        )
                
                for v_id, v in active_versions.items():
                    if v_id not in processed_ids:
                        v.activa = False
                        v.save()

            return Response({'status': 'ok'})
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            
    def delete(self, request, pk):
        clausula = get_object_or_404(scoped(Clausula.objects.all(), request), pk=pk)
        # Soft delete
        clausula.activa = False
        clausula.save()
        return Response({'status': 'deleted'})


class EmitidosListView(APIView):
    """
    GET /api/plantillas/emitidos/
    Lista paginada de documentos generados (registros inmutables).

    Query params:
        - software_id: filtra por software del contrato
        - cliente_id: filtra por cliente del contrato
        - contrato_id: filtra por contrato específico
        - fecha_desde / fecha_hasta: rango de fecha_generacion (YYYY-MM-DD)
        - page / page_size: paginación (default page_size=20, max=100)
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
        except (ValueError, TypeError):
            page, page_size = 1, 20

        qs = scoped(DocumentoGenerado.objects.all(), request, 'contrato__tenant').select_related(
            'plantilla', 'contrato', 'contrato__cliente',
            'contrato__software', 'generado_por',
        ).order_by('-fecha_generacion')

        software_id = request.query_params.get('software_id')
        if software_id:
            qs = qs.filter(contrato__software_id=software_id)

        cliente_id = request.query_params.get('cliente_id')
        if cliente_id:
            qs = qs.filter(contrato__cliente_id=cliente_id)

        contrato_id = request.query_params.get('contrato_id')
        if contrato_id:
            qs = qs.filter(contrato_id=contrato_id)

        fecha_desde = request.query_params.get('fecha_desde')
        if fecha_desde:
            qs = qs.filter(fecha_generacion__date__gte=fecha_desde)

        fecha_hasta = request.query_params.get('fecha_hasta')
        if fecha_hasta:
            qs = qs.filter(fecha_generacion__date__lte=fecha_hasta)

        total = qs.count()
        offset = (page - 1) * page_size
        items = list(qs[offset: offset + page_size])

        def _to_dict(d):
            cliente = d.contrato.cliente
            cliente_nombre = (
                getattr(getattr(cliente, 'personajuridica', None), 'razon_social', None)
                or getattr(getattr(cliente, 'personanatural', None), 'nombre_completo', None)
                or str(cliente)
            )
            return {
                'id': d.id,
                'contrato_id': d.contrato_id,
                'contrato_display': f'CTR-{str(d.contrato_id).zfill(6)}',
                'cliente_id': d.contrato.cliente_id,
                'cliente_nombre': cliente_nombre,
                'software_id': d.contrato.software_id,
                'software_nombre': d.contrato.software.nombre if d.contrato.software_id else '',
                'plantilla_id': d.plantilla_id,
                'plantilla_nombre': d.plantilla.nombre,
                'plantilla_version': d.plantilla.version_codigo,
                'hash_sha256': d.hash_sha256,
                'fecha_generacion': d.fecha_generacion,
                'generado_por': (
                    d.generado_por.get_full_name() or d.generado_por.username
                ) if d.generado_por_id else 'Sistema',
                'tiene_pdf': bool(d.archivo_pdf),
                'pdf_url': request.build_absolute_uri(d.archivo_pdf.url) if d.archivo_pdf else None,
            }

        return Response({
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, -(-total // page_size)),
            'results': [_to_dict(d) for d in items],
        })

