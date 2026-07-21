from django.db.models import Count, Q
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

from contratos.models import Contrato, EtapaContrato, TipoContrato
from catalogo.models import Producto
from tenants.permissions import DeleteRequiresTenantAdmin, IsTenantMember, RequiresFeature
from tenants.scoping import resolve_tenant_for_write, scoped
from .models import (
    PlantillaDocumento, DocumentoGenerado, Clausula, VersionClausula,
    ModoOrigenPlantilla, TipoTextoClausula,
)
from .services.validacion import validar_docx_subido
from .services.renderizado import (
    generar_documento, resolver_plantilla_activa, obtener_preview_pdf,
    aplicar_campos_manuales, renderizar_html,
    PlantillaRenderError, VariablesFaltantesError, SinPlantillaActivaError, ConversionPDFError,
)
from .services.contexto import construir_contexto
from .services.html_doc import PlantillaHTMLNoEncontrada

# Etapas en las que el contrato ya tiene un documento "vigente" — regenerar acá
# requiere confirmación explícita (forzar=true) para no pisar silenciosamente
# un documento ya emitido/firmado.
ETAPAS_CON_DOCUMENTO_EMITIDO = {
    EtapaContrato.PENDIENTE_FIRMA, EtapaContrato.ACTIVO,
    EtapaContrato.ENMENDADO, EtapaContrato.TERMINADO,
}


def _available_html_templates_info():
    """Plantillas HTML ofrecidas como template en el CLM, con metadata de
    nomenclatura: [{'ruta', 'nombre', 'tipo'}]. tipo=None => global.

    Única fuente de verdad para el dropdown del frontend (AvailableHtmlTemplatesView)
    y para validar server-side que `ruta_plantilla_html` no apunte a un template
    fuera de las carpetas permitidas. Dos orígenes:
    - DOCS_TEMPLATE_DIR (clm_frontend/public/docs_template): archivos .dc.html
      exportados desde Claude Design (nomenclatura TIPO__Nombre.dc.html); el
      motor los adapta a página imprimible.
    - templates/plantillas_html/: templates Django planos legados (globales)."""
    import os

    from .services.html_doc import listar_plantillas_docs_info

    templates = listar_plantillas_docs_info()

    base_dir = Path(settings.BASE_DIR) / 'templates' / 'plantillas_html'
    if base_dir.exists():
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.endswith('.html'):
                    rel_path = os.path.relpath(os.path.join(root, f), base_dir)
                    rel_path = rel_path.replace('\\', '/')
                    templates.append({
                        'ruta': f'plantillas_html/{rel_path}',
                        'nombre': f,
                        'tipo': None,
                    })
    return templates


def _available_html_templates():
    """Rutas planas de plantillas HTML válidas (para validación server-side)."""
    return [t['ruta'] for t in _available_html_templates_info()]


def _validar_ruta_html_para_tipo(ruta: str, tipo_contrato: str):
    """La ruta debe existir y su nomenclatura (si declara tipo) debe coincidir
    con el tipo de contrato de la plantilla que se está creando/editando."""
    info = next((t for t in _available_html_templates_info() if t['ruta'] == ruta), None)
    if info is None:
        raise DRFValidationError({'ruta_plantilla_html': 'Ruta de plantilla HTML no reconocida.'})
    if info['tipo'] and tipo_contrato and info['tipo'] != tipo_contrato:
        raise DRFValidationError({
            'ruta_plantilla_html': (
                f"La plantilla HTML '{info['nombre']}' es para tipo {info['tipo']} "
                f"según su nomenclatura, pero esta plantilla es de tipo {tipo_contrato}."
            )
        })


def _plantilla_a_dict(p: PlantillaDocumento):
    # 'usos' viene anotado (Count) cuando el caller lo precalculó en bulk (listado);
    # si no, cae a una query individual (detalle/creación de una sola plantilla).
    usos = getattr(p, 'usos', None)
    if usos is None:
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
        'confirmada': p.confirmada,
        'fecha_creacion': p.fecha_creacion,
        'usos': usos,
        # .all() en vez de .values_list(): cuando el caller usó prefetch_related
        # (listado) esto lee de la caché ya cargada en vez de disparar otra query.
        'clausulas_seleccionadas': [c.id for c in p.clausulas_seleccionadas.all()],
        'ruta_plantilla_html': p.ruta_plantilla_html if p.modo_origen == 'html' else None,
        'codigo_prefijo': p.codigo_prefijo,
        'requiere_sla_facturacion': p.requiere_sla_facturacion,
        'portada': p.portada.url if p.portada else None,
        'tiene_formulario_dinamico': getattr(p, 'tiene_formulario_dinamico', p.preguntas.exists()),
    }


def _colision_activa(tenant, tipo_contrato, software_id, exclude_pk=None):
    """Devuelve la plantilla actualmente activa para (tenant, tipo_contrato, software),
    si existe y es distinta de exclude_pk. PlantillaDocumento.save() desactiva esa fila
    en silencio en cuanto se guarda una nueva activa para la misma combinación — este
    helper existe para poder avisarle al usuario ANTES de que eso ocurra."""
    qs = PlantillaDocumento.objects.filter(
        tenant=tenant, tipo_contrato=tipo_contrato, software_id=software_id, activa=True,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.first()


def _documento_a_dict(d: DocumentoGenerado):
    return {
        'id': d.id,
        'contrato_id': d.contrato_id,
        'plantilla_id': d.plantilla_id,
        'plantilla_version': d.plantilla.version_codigo if d.plantilla_id else 'N/A (Eliminada)',
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
        # select_related('software') evita 1 query por fila para software_nombre;
        # annotate(usos=...) + prefetch_related evita el N+1 que había en
        # _plantilla_a_dict (antes: 2 queries extra por plantilla, sin paginar).
        qs = (
            scoped(PlantillaDocumento.objects.all(), request)
            .select_related('software')
            .annotate(usos=Count('documentogenerado', distinct=True))
            .prefetch_related('clausulas_seleccionadas')
        )
        tipo_contrato = request.GET.get('tipo_contrato')
        software_id = request.GET.get('software')
        activa = request.GET.get('activa')
        modo_origen = request.GET.get('modo_origen')
        codigo_prefijo = request.GET.get('codigo_prefijo')
        incluir_globales = request.GET.get('incluir_globales', '').lower() in ('1', 'true', 'si')
        if tipo_contrato:
            qs = qs.filter(tipo_contrato=tipo_contrato)
        if codigo_prefijo:
            # Todas las versiones de una misma familia de documento (ej. todas las NDA),
            # para el catálogo agrupado.
            qs = qs.filter(codigo_prefijo__iexact=codigo_prefijo)
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
        codigo_prefijo = (request.data.get('codigo_prefijo') or '').strip().upper()[:20]

        errors = {}
        if not nombre:
            errors['nombre'] = 'Este campo es requerido.'
        if not tipo_contrato:
            errors['tipo_contrato'] = 'Este campo es requerido.'
        elif tipo_contrato not in TipoContrato.values:
            errors['tipo_contrato'] = f'Tipo inválido. Opciones: {list(TipoContrato.values)}'
        if not version_codigo:
            errors['version_codigo'] = 'Este campo es requerido.'
        if not software_id:
            errors['software'] = 'Debe especificar a qué software/producto pertenece esta plantilla.'
        if modo_origen not in ModoOrigenPlantilla.values:
            errors['modo_origen'] = f'Modo inválido. Opciones: {list(ModoOrigenPlantilla.values)}'
        if not codigo_prefijo:
            errors['codigo_prefijo'] = (
                'Debe indicar la familia de documento (ej: NDA, MSA, TOS) — agrupa las distintas '
                'versiones de un mismo documento en el catálogo.'
            )
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
            _validar_ruta_html_para_tipo(ruta_plantilla_html, tipo_contrato)

        tenant = resolve_tenant_for_write(request, request.data)
        if software_id and not Producto.objects.filter(pk=software_id, tenant=tenant).exists():
            raise DRFValidationError({'software': 'Producto no encontrado.'})

        activa = str(request.data.get('activa', 'true')).lower() in ('1', 'true', 'si')
        confirmar = str(request.data.get('confirmar', 'false')).lower() in ('1', 'true', 'si')

        if activa:
            conflicto = _colision_activa(tenant, tipo_contrato, software_id)
            if conflicto and not confirmar:
                return Response(
                    {
                        'error': (
                            f"Ya existe una plantilla activa para este tipo/software: "
                            f"'{conflicto.nombre}' ({conflicto.codigo_prefijo} · {conflicto.version_codigo}). "
                            f"¿Confirmas crear esta nueva versión? La anterior quedará archivada (inactiva)."
                        ),
                        'requiere_confirmacion': True,
                        'plantilla_conflicto': _plantilla_a_dict(conflicto),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        plantilla = PlantillaDocumento.objects.create(
            tenant=tenant,
            nombre=nombre,
            tipo_contrato=tipo_contrato,
            software_id=software_id,
            modo_origen=modo_origen,
            archivo_docx=archivo if modo_origen == ModoOrigenPlantilla.ARCHIVO else None,
            ruta_plantilla_html=ruta_plantilla_html if modo_origen == ModoOrigenPlantilla.HTML else None,
            codigo_prefijo=codigo_prefijo,
            requiere_sla_facturacion=str(request.data.get('requiere_sla_facturacion', 'true')).lower() in ('1', 'true', 'si'),
            version_codigo=version_codigo,
            activa=activa,
            portada=request.FILES.get('portada'),
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
    GET    /api/plantillas/plantillas/<id>/
    PATCH  /api/plantillas/plantillas/<id>/   (solo permite alternar {"activa": bool};
                                                para cambiar el .docx se sube una plantilla nueva)
    DELETE /api/plantillas/plantillas/<id>/   (solo borradores — plantillas nunca confirmadas;
                                                una confirmada se archiva, no se elimina)
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
        estaba_activa = plantilla.activa

        if 'activa' in request.data:
            plantilla.activa = str(request.data.get('activa')).lower() in ('1', 'true', 'si')

        if 'nombre' in request.data:
            plantilla.nombre = request.data.get('nombre')
        if 'tipo_contrato' in request.data:
            nuevo_tipo = request.data.get('tipo_contrato')
            if nuevo_tipo not in TipoContrato.values:
                raise DRFValidationError({'tipo_contrato': f'Tipo inválido. Opciones: {list(TipoContrato.values)}'})
            plantilla.tipo_contrato = nuevo_tipo
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
                _validar_ruta_html_para_tipo(nueva_ruta, plantilla.tipo_contrato)
            plantilla.ruta_plantilla_html = nueva_ruta

        if 'portada' in request.FILES:
            plantilla.portada = request.FILES.get('portada')
        elif 'portada' in request.data and request.data.get('portada') in [None, '', 'null']:
            plantilla.portada = None

        if 'codigo_prefijo' in request.data:
            nuevo_prefijo = (request.data.get('codigo_prefijo') or '').strip().upper()[:20]
            if not nuevo_prefijo:
                raise DRFValidationError({'codigo_prefijo': 'La familia de documento no puede quedar vacía.'})
            plantilla.codigo_prefijo = nuevo_prefijo

        if 'requiere_sla_facturacion' in request.data:
            plantilla.requiere_sla_facturacion = str(request.data.get('requiere_sla_facturacion')).lower() in ('1', 'true', 'si')

        clausulas_str = request.data.get('clausulas_seleccionadas')
        if clausulas_str is not None:
            import json
            try:
                clausulas_ids = json.loads(clausulas_str)
                plantilla.clausulas_seleccionadas.set(clausulas_ids)
            except ValueError:
                pass

        confirmar = str(request.data.get('confirmar', 'false')).lower() in ('1', 'true', 'si')
        if plantilla.activa and not estaba_activa and not confirmar:
            conflicto = _colision_activa(plantilla.tenant_id, plantilla.tipo_contrato, plantilla.software_id, exclude_pk=plantilla.pk)
            if conflicto:
                return Response(
                    {
                        'error': (
                            f"Ya existe una plantilla activa para este tipo/software: "
                            f"'{conflicto.nombre}' ({conflicto.codigo_prefijo} · {conflicto.version_codigo}). "
                            f"¿Confirmas activar esta versión? La anterior quedará archivada (inactiva)."
                        ),
                        'requiere_confirmacion': True,
                        'plantilla_conflicto': _plantilla_a_dict(conflicto),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        plantilla.save()
        return Response(_plantilla_a_dict(plantilla))

    def delete(self, request, pk):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            return Response(
                {'error': 'Solo el Administrador de Cuenta puede eliminar plantillas.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)

        if plantilla.activa:
            return Response(
                {'error': 'Esta plantilla está activa. Debes archivarla (desactivarla) antes de poder eliminarla.'},
                status=status.HTTP_409_CONFLICT,
            )

        docs = DocumentoGenerado.objects.filter(plantilla=plantilla).select_related('contrato')
        docs_pendientes = docs.filter(contrato__etapa__in=['BORRADOR', 'REVISION'])
        total_docs_pendientes = len(docs_pendientes)

        if total_docs_pendientes:
            docs_info = []
            for doc in docs_pendientes[:3]:
                contrato_nombre = doc.contrato.nombre or f"Contrato #{doc.contrato_id}"
                docs_info.append({'id': doc.contrato_id, 'nombre': contrato_nombre})

            return Response(
                {
                    'error': (
                        f"Esta versión está siendo utilizada por {total_docs_pendientes} contrato(s) "
                        f"en etapa de Borrador o Revisión, por lo que no puede eliminarse."
                    ),
                    'documentos_afectados': docs_info,
                    'total_docs': total_docs_pendientes
                },
                status=status.HTTP_409_CONFLICT,
            )

        plantilla.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FormularioDinamicoView(APIView):
    """GET /api/plantillas/plantillas/<id>/formulario/"""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        preguntas = plantilla.preguntas.all().prefetch_related('opciones')
        data = []
        for p in preguntas:
            data.append({
                'id': p.id,
                'texto': p.texto,
                'tipo': p.tipo,
                'orden': p.orden,
                'opciones': [{'id': o.id, 'texto': o.texto} for o in p.opciones.all()]
            })
        return Response({'plantilla_id': plantilla.id, 'preguntas': data})

class EvaluarFormularioView(APIView):
    """POST /api/plantillas/plantillas/<id>/evaluar-formulario/"""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def post(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        respuestas = request.data.get('respuestas', {})
        
        reglas = ReglaInclusionClausula.objects.filter(plantilla=plantilla).select_related('clausula_version', 'clausula_version__clausula', 'pregunta')
        
        versiones_incluidas = []
        for regla in reglas:
            pregunta_id = str(regla.pregunta_id)
            if pregunta_id in respuestas:
                respuesta = respuestas[pregunta_id]
                if regla.pregunta.tipo == 'booleano':
                    if str(respuesta).lower() in ('true', '1', 'si', 'True') and regla.respuesta_booleana == True:
                        versiones_incluidas.append(regla.clausula_version)
                    elif str(respuesta).lower() in ('false', '0', 'no', 'False') and regla.respuesta_booleana == False:
                        versiones_incluidas.append(regla.clausula_version)
                elif regla.pregunta.tipo == 'opcion_multiple':
                    try:
                        if int(respuesta) == regla.opcion_respuesta_id:
                            versiones_incluidas.append(regla.clausula_version)
                    except (ValueError, TypeError):
                        pass
                        
        data = []
        for v in versiones_incluidas:
            data.append({
                'clausula_id': v.clausula_id,
                'version_id': v.id,
                'titulo': v.clausula.nombre,
                'texto': v.texto,
                'tipo_texto': v.clausula.tipo_texto
            })
            
        return Response({'clausulas': data})


class FormularioBuilderView(APIView):
    """GET y PUT para construir/editar todo el formulario de una plantilla de una vez."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        
        preguntas = plantilla.preguntas.all().prefetch_related('opciones')
        reglas = ReglaInclusionClausula.objects.filter(plantilla=plantilla).select_related('clausula_version', 'clausula_version__clausula')
        
        data = []
        for p in preguntas:
            pregunta_dict = {
                'id': p.id,
                'texto': p.texto,
                'tipo': p.tipo,
                'orden': p.orden,
                'opciones': [{'id': o.id, 'texto': o.texto} for o in p.opciones.all()],
                'reglas': []
            }
            for r in reglas:
                if r.pregunta_id == p.id:
                    pregunta_dict['reglas'].append({
                        'id': r.id,
                        'opcion_respuesta_id': r.opcion_respuesta_id,
                        'respuesta_booleana': r.respuesta_booleana,
                        'clausula_version_id': r.clausula_version_id,
                        'clausula_nombre': r.clausula_version.clausula.nombre,
                        'clausula_etiqueta': r.clausula_version.etiqueta,
                    })
            data.append(pregunta_dict)
            
        return Response({'plantilla_id': plantilla.id, 'preguntas': data})

    def put(self, request, pk):
        if request.user.tenant_id is not None and not request.user.is_tenant_admin:
            if not getattr(request.user, 'is_moderador', False) and not request.user.is_superadmin:
                return Response({'error': 'No tienes permisos para editar este formulario.'}, status=status.HTTP_403_FORBIDDEN)
                
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        preguntas_data = request.data.get('preguntas', [])
        
        with transaction.atomic():
            plantilla.preguntas.all().delete()
            
            for p_idx, p_data in enumerate(preguntas_data):
                pregunta = PreguntaFormulario.objects.create(
                    plantilla=plantilla,
                    texto=p_data.get('texto', ''),
                    tipo=p_data.get('tipo', 'booleano'),
                    orden=p_idx
                )
                
                opciones_map = {}
                for o_data in p_data.get('opciones', []):
                    opcion = OpcionRespuesta.objects.create(
                        pregunta=pregunta,
                        texto=o_data.get('texto', '')
                    )
                    if 'id' in o_data:
                        opciones_map[str(o_data['id'])] = opcion
                        
                for r_data in p_data.get('reglas', []):
                    clausula_version_id = r_data.get('clausula_version_id')
                    if not clausula_version_id:
                        continue
                        
                    opcion_id_str = str(r_data.get('opcion_respuesta_id'))
                    db_opcion = opciones_map.get(opcion_id_str)
                    
                    ReglaInclusionClausula.objects.create(
                        plantilla=plantilla,
                        pregunta=pregunta,
                        opcion_respuesta=db_opcion,
                        respuesta_booleana=r_data.get('respuesta_booleana'),
                        clausula_version_id=clausula_version_id
                    )
                    
        return Response({'status': 'ok'})

class PlantillaContratosView(APIView):
    """GET /api/plantillas/plantillas/<id>/contratos/

    Contratos que usan esta versión específica de plantilla (uno por contrato,
    aunque se haya regenerado el documento varias veces con ella)."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)

        docs = list(
            DocumentoGenerado.objects
            .filter(plantilla=plantilla)
            .select_related('contrato', 'contrato__cliente', 'contrato__software')
            .order_by('contrato_id', '-fecha_generacion')
        )

        conteo_por_contrato = {}
        mas_reciente_por_contrato = {}
        for d in docs:
            conteo_por_contrato[d.contrato_id] = conteo_por_contrato.get(d.contrato_id, 0) + 1
            # order_by ya deja la más reciente primero dentro de cada contrato_id.
            mas_reciente_por_contrato.setdefault(d.contrato_id, d)

        def _cliente_nombre(cliente):
            return (
                getattr(getattr(cliente, 'personajuridica', None), 'razon_social', None)
                or getattr(getattr(cliente, 'personanatural', None), 'nombre_completo', None)
                or str(cliente)
            )

        resultado = []
        for contrato_id, d in mas_reciente_por_contrato.items():
            c = d.contrato
            resultado.append({
                'contrato_id': c.id,
                'contrato_display': f'CTR-{str(c.id).zfill(6)}',
                'nombre': c.nombre,
                'cliente_id': c.cliente_id,
                'cliente_nombre': _cliente_nombre(c.cliente),
                'software_id': c.software_id,
                'software_nombre': c.software.nombre if c.software_id else '',
                'etapa': c.etapa,
                'etapa_display': c.get_etapa_display(),
                'status': c.status,
                'monto': str(c.monto),
                'fecha_ultima_generacion': d.fecha_generacion,
                'total_generaciones': conteo_por_contrato[contrato_id],
            })
        resultado.sort(key=lambda r: r['fecha_ultima_generacion'], reverse=True)

        return Response({
            'plantilla_id': plantilla.id,
            'plantilla_nombre': plantilla.nombre,
            'plantilla_version': plantilla.version_codigo,
            'total_contratos': len(resultado),
            'results': resultado,
        })


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


class PlantillaPreviewImageView(APIView):
    """GET /api/plantillas/plantillas/<id>/preview-img/

    Sirve una imagen JPG de la primera página de la plantilla para usar como portada/miniatura.
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        from .services.renderizado import obtener_preview_imagen
        plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=pk)
        if plantilla.modo_origen == ModoOrigenPlantilla.ARCHIVO and not plantilla.archivo_docx:
            return Response(
                {'error': 'Esta plantilla en modo archivo no tiene un documento base (.docx) para previsualizar.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            img_path = obtener_preview_imagen(plantilla)
        except Exception:
            return Response(
                {'error': 'No se pudo generar la portada de la plantilla.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return FileResponse(
            open(img_path, 'rb'), as_attachment=False,
            filename=f"plantilla_{plantilla.id}_portada.jpg", content_type='image/jpeg',
        )


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
    """GET /api/plantillas/html-templates/?tipo_contrato=<TIPO>

    Devuelve [{'ruta', 'nombre', 'tipo'}]. Con ?tipo_contrato= solo entrega las
    plantillas de ese tipo (según nomenclatura TIPO__Nombre.dc.html) más las
    globales (sin prefijo, tipo=null)."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        tipo = request.GET.get('tipo_contrato')
        items = _available_html_templates_info()
        if tipo:
            items = [t for t in items if t['tipo'] in (None, tipo)]
        return Response(items)


class CamposPlantillaView(APIView):
    """GET /api/plantillas/documentos/campos/?plantilla_id=<id>
       GET /api/plantillas/documentos/campos/?contrato_id=<id>[&plantilla_id=<id>]

    Campos manuales que la plantilla HTML espera del usuario (para renderizar
    el formulario previo a "Generar documento"). Si la plantilla resuelta no
    es de modo HTML, devuelve lista vacía (docx/cláusulas no tienen este paso).

    `plantilla_id` solo (sin contrato_id): consulta directa a una plantilla ya
    elegida, útil en wizards donde el contrato aún no existe (UseTemplateModal,
    NewContractModal). `contrato_id`: resuelve la plantilla activa de ese
    contrato si no se especifica `plantilla_id`."""
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        contrato_id = request.GET.get('contrato_id')
        plantilla_id = request.GET.get('plantilla_id')
        if not contrato_id and not plantilla_id:
            raise DRFValidationError({'contrato_id': 'Se requiere contrato_id o plantilla_id.'})

        if not contrato_id:
            plantilla = get_object_or_404(scoped(PlantillaDocumento.objects.all(), request), pk=plantilla_id)
        else:
            contrato = get_object_or_404(scoped(Contrato.objects.all(), request), pk=contrato_id)
            if plantilla_id:
                plantilla = get_object_or_404(
                    PlantillaDocumento.objects.filter(tenant=contrato.tenant), pk=plantilla_id,
                )
            else:
                try:
                    plantilla = resolver_plantilla_activa(contrato.tipo_contrato, contrato.software_id, contrato.tenant)
                except SinPlantillaActivaError as exc:
                    return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)

        if plantilla.modo_origen != ModoOrigenPlantilla.HTML:
            return Response({'plantilla_id': plantilla.id, 'campos': []})

        from .services.html_doc import extraer_campos_manuales
        try:
            campos = extraer_campos_manuales(plantilla.ruta_plantilla_html)
        except PlantillaHTMLNoEncontrada as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({'plantilla_id': plantilla.id, 'campos': campos})


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


class PreviewBorradorPDFView(APIView):
    """POST /api/plantillas/documentos/preview-borrador/  {contrato_id, campos?, clausulas?}

    PDF efímero del documento tal como quedaría con el estado actual del
    usuario: no crea DocumentoGenerado, no consume correlativo de Referencia y
    no persiste nada en el contrato.

    - `campos`: valores de los campos manuales de plantillas HTML.
    - `clausulas`: bloques del editor de cláusulas SIN guardar (mismo shape que
      el PATCH de clausulas_estructuradas) — se aplican al contrato solo en
      memoria, para ver cómo queda el documento mientras se edita.
    Soporta los tres modos de plantilla: html vía WeasyPrint (rápido) y
    archivo/cláusulas vía docxtpl + LibreOffice (más lento; el frontend usa un
    debounce mayor).
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def post(self, request):
        contrato_id = request.data.get('contrato_id')
        if not contrato_id:
            raise DRFValidationError({'contrato_id': 'Este campo es requerido.'})
        contrato = get_object_or_404(
            scoped(Contrato.objects.all(), request).select_related('cliente', 'software', 'sla'),
            pk=contrato_id,
        )
        try:
            plantilla = resolver_plantilla_activa(
                contrato.tipo_contrato, contrato.software_id, contrato.tenant)
        except SinPlantillaActivaError:
            return Response(
                {'error': 'No hay una plantilla activa para este contrato.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Bloques en vivo del editor: mismo saneo que el PATCH real, pero el
        # contrato NO se guarda — el override vive solo en esta instancia.
        if 'clausulas' in request.data:
            from contratos.views import _validar_clausulas_estructuradas
            contrato.clausulas_estructuradas = _validar_clausulas_estructuradas(
                request.data.get('clausulas'))

        contexto = construir_contexto(contrato)
        campos = request.data.get('campos')
        aplicar_campos_manuales(contexto, campos if isinstance(campos, dict) else None)
        # Correlativo ficticio: el real se consume solo al generar de verdad.
        contexto['referencia'] = 'VISTA PREVIA'
        try:
            if plantilla.modo_origen == ModoOrigenPlantilla.HTML:
                from .services.html_doc import html_a_pdf
                html_bytes = renderizar_html(plantilla, contexto)
                pdf_bytes = html_a_pdf(html_bytes.decode('utf-8'))
            else:
                from .services.renderizado import convertir_a_pdf, renderizar_docx
                docx_bytes = renderizar_docx(plantilla, contexto, contrato)
                pdf_bytes = convertir_a_pdf(docx_bytes)
        except VariablesFaltantesError as exc:
            return Response(
                {'error': f'Faltan variables para la vista previa: {exc}'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except (PlantillaRenderError, PlantillaHTMLNoEncontrada, ConversionPDFError) as exc:
            return Response(
                {'error': f'No se pudo generar la vista previa: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        from django.http import HttpResponse
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        # Se sirve embebido en un iframe del propio frontend.
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response


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
            if contrato.firma_status == 'SIGNED':
                aviso_firma_confirm = " El contrato está firmado electrónicamente: la firma quedará invalidada y deberá reenviarse a firma."
            elif contrato.firma_status == 'PENDING' and contrato.firma_proveedor == 'OTP':
                aviso_firma_confirm = " Hay un enlace de firma pendiente de confirmación: quedará invalidado y se reenviará uno nuevo al correo del cliente."
            else:
                aviso_firma_confirm = ""
            return Response(
                {
                    'error': (
                        f"Este contrato ya está en etapa '{contrato.get_etapa_display()}'. "
                        "¿Confirmas generar una nueva versión del documento? "
                        "Esto no elimina la versión anterior." + aviso_firma_confirm
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

        campos = request.data.get('campos') or {}
        if not isinstance(campos, dict):
            raise DRFValidationError({'campos': 'Debe ser un objeto {variable: valor}.'})
        if len(campos) > 50:
            raise DRFValidationError({'campos': 'Máximo 50 campos.'})
        for clave, valor in campos.items():
            if not isinstance(valor, str) or len(str(clave)) > 64 or len(valor) > 10000:
                raise DRFValidationError({'campos': f"Valor inválido para '{clave}': solo texto (máx. 10.000 caracteres)."})

        try:
            documento = generar_documento(contrato, plantilla=plantilla, usuario=request.user, campos=campos)
            from contratos.models import HistorialEtapaContrato
            from contratos.services import sincronizar_firma_tras_regeneracion
            HistorialEtapaContrato.objects.create(
                contrato=contrato,
                etapa_anterior=contrato.etapa,
                etapa_nueva=contrato.etapa,
                usuario=request.user,
                notas="Actualización/Regeneración de documento PDF desde plantilla."
            )
            # El contenido recién generado puede no coincidir más con una firma
            # ya en curso (SIGNED) o con el enlace que tiene el cliente
            # (PENDING) -- ver contratos.services.sincronizar_firma_tras_regeneracion.
            aviso_firma = sincronizar_firma_tras_regeneracion(contrato, usuario=request.user)
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

        payload = _documento_a_dict(documento)
        if aviso_firma:
            payload['aviso_firma'] = aviso_firma
        return Response(payload, status=status.HTTP_201_CREATED)


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


class DocumentoPreviewImageView(APIView):
    """GET /api/plantillas/documentos/<id>/preview-img/

    Primera página del PDF del documento como JPG (miniatura para las vistas de
    contratos). Los DocumentoGenerado son write-once, así que la imagen se
    cachea por id para siempre — regenerar crea otro documento (id nuevo).
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request, pk):
        documento = get_object_or_404(
            scoped(DocumentoGenerado.objects.all(), request, 'contrato__tenant'), pk=pk,
        )
        if not documento.archivo_pdf:
            return Response(
                {'error': 'Este documento no tiene PDF para previsualizar.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        cache_dir = Path(settings.MEDIA_ROOT) / 'documentos_previews'
        cache_dir.mkdir(parents=True, exist_ok=True)
        img_path = cache_dir / f"documento_{documento.id}.jpg"
        if not img_path.exists():
            try:
                import pdfplumber
                with pdfplumber.open(documento.archivo_pdf.path) as pdf:
                    if not pdf.pages:
                        raise ValueError('PDF sin páginas')
                    # 150 dpi: suficiente para miniatura, a diferencia de la
                    # portada de plantillas (300) que se muestra a mayor tamaño.
                    im = pdf.pages[0].to_image(resolution=150)
                    pil_img = getattr(im, 'original', im)
                    if hasattr(pil_img, 'convert'):
                        pil_img = pil_img.convert('RGB')
                    pil_img.save(str(img_path), format='JPEG')
            except Exception:
                return Response(
                    {'error': 'No se pudo generar la miniatura del documento.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return FileResponse(
            open(img_path, 'rb'), as_attachment=False,
            filename=f"documento_{documento.id}_miniatura.jpg", content_type='image/jpeg',
        )


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

def _clausula_a_dict(c: Clausula):
    # Mismo shape que un item del listado GET: el frontend lo usa para hacer
    # upsert local de la cláusula creada/editada sin refetchear la biblioteca.
    # Filtrado de activas en Python: con prefetch_related lee de la caché.
    return {
        'id': c.id,
        'cat': c.categoria,
        'name': c.nombre,
        'risk': c.riesgo,
        'tipo_texto': c.tipo_texto,
        'versions': [
            {
                'id': v.id,
                'etiqueta': v.etiqueta,
                'tipo': v.tipo,
                'texto': v.texto,
            } for v in c.versiones.all() if v.activa
        ],
    }


def _tipo_texto_valido(valor, default=None):
    """Normaliza el tipo de texto recibido del cliente; valor desconocido → default."""
    if not valor:
        return default
    valor = str(valor).upper()
    return valor if valor in TipoTextoClausula.values else default


class ClausulaListView(APIView):
    """
    GET /api/plantillas/clausulas/?tipo=SALUDO  (tipo opcional)
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        qs = scoped(Clausula.objects.all(), request).prefetch_related('versiones').filter(activa=True)
        tipo = _tipo_texto_valido(request.query_params.get('tipo'))
        if tipo:
            qs = qs.filter(tipo_texto=tipo)
        return Response([_clausula_a_dict(c) for c in qs])

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
                    tipo_texto=_tipo_texto_valido(
                        data.get('tipo_texto'), TipoTextoClausula.CLAUSULA),
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
            
            return Response({'status': 'ok', **_clausula_a_dict(clausula)}, status=status.HTTP_201_CREATED)
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
                clausula.tipo_texto = _tipo_texto_valido(
                    data.get('tipo_texto'), clausula.tipo_texto)
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

            # Se relee para reflejar las versiones recién creadas/desactivadas
            # (el reemplazo de versiones cambia sus IDs) — el frontend hace
            # upsert local con este cuerpo en vez de refetchear todo.
            return Response({'status': 'ok', **_clausula_a_dict(clausula)})
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        clausula = get_object_or_404(scoped(Clausula.objects.all(), request), pk=pk)
        # Soft delete
        clausula.activa = False
        clausula.save()
        return Response({'status': 'deleted'})


class ClausulaIndiceView(APIView):
    """
    GET /api/plantillas/clausulas/indice/?tipo=SALUDO  (tipo opcional)

    Índice compacto de la biblioteca agrupado por tipo de texto, resuelto en una
    sola consulta (apoyada en idx_clausula_tenant_tipo). Pensado para recopilar
    de un vistazo qué textos existen (saludos, despedidas, cierres, cláusulas)
    sin traer los cuerpos completos.
    """
    permission_classes = [IsTenantMember, RequiresFeature('plantillas')]

    def get(self, request):
        qs = scoped(Clausula.objects.all(), request).filter(activa=True)
        tipo = _tipo_texto_valido(request.query_params.get('tipo'))
        if tipo:
            qs = qs.filter(tipo_texto=tipo)
        filas = (
            qs.values('id', 'nombre', 'categoria', 'riesgo', 'tipo_texto')
              .annotate(versiones=Count('versiones', filter=Q(versiones__activa=True)))
              .order_by('tipo_texto', 'categoria', 'nombre')
        )
        labels = dict(TipoTextoClausula.choices)
        grupos = {}
        for fila in filas:
            grupo = grupos.setdefault(fila['tipo_texto'], {
                'tipo': fila['tipo_texto'],
                'label': labels.get(fila['tipo_texto'], fila['tipo_texto']),
                'items': [],
            })
            grupo['items'].append({
                'id': fila['id'],
                'nombre': fila['nombre'],
                'categoria': fila['categoria'],
                'riesgo': fila['riesgo'],
                'versiones': fila['versiones'],
            })
        # Orden estable según la declaración de choices (cláusulas primero).
        orden = list(TipoTextoClausula.values)
        tipos = sorted(grupos.values(), key=lambda g: orden.index(g['tipo']))
        return Response({
            'total': sum(len(g['items']) for g in tipos),
            'tipos': tipos,
        })


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
                'plantilla_nombre': d.plantilla.nombre if d.plantilla_id else 'Eliminada',
                'plantilla_version': d.plantilla.version_codigo if d.plantilla_id else 'N/A',
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

