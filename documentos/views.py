import json
import re
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from contratos.models import Contrato, TipoContrato
from clientes.models import Cliente
from clientes.views import get_filtered_clientes_unified
from tenants.decorators import require_feature, require_tenant_write
from tenants.scoping import QuotaExceeded, enforce_quota, scoped
from .services import exportar, importar
from .services.auditoria import (
    build_export_filename, build_audit_meta,
    describir_filtros_clientes, describir_filtros_contratos,
)

_CODIGO_CONTRATO_RE = re.compile(r'^\s*ctr-?0*(\d+)\s*$', re.IGNORECASE)


def _parse_codigo_contrato(texto):
    """Extrae el ID numérico de una nomenclatura estandarizada tipo 'CTR-000041'
    (o variantes 'ctr41', '000041'). Devuelve None si no matchea el patrón."""
    m = _CODIGO_CONTRATO_RE.match(texto or '')
    return int(m.group(1)) if m else None


def _clientes_filtrados_ordenados(request):
    """Si viene 'ids' (csv de IDs, ej. selección manual del usuario), exporta SOLO esos,
    en el orden recibido. Si no, aplica los mismos filtros/búsqueda de la tabla /clientes/
    y devuelve la lista completa filtrada (orden fecha_registro desc)."""
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        ids_ordenados = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
    else:
        unified = get_filtered_clientes_unified(request.GET, request)
        ids_ordenados = [u['obj'].id for u in unified]

    # scoped() también acota la selección manual por ids: no se puede exportar
    # un cliente de otro tenant aunque se conozca su ID.
    clientes_map = {c.id: c for c in scoped(Cliente.objects.all(), request).filter(id__in=ids_ordenados)}
    return [clientes_map[cid] for cid in ids_ordenados if cid in clientes_map]


def _contratos_filtrados_queryset(request):
    """
    Recorte a exportar, en orden de prioridad:
      1. 'ids' (csv de IDs) — selección manual desde la tabla.
      2. 'cliente_id' — TODOS los contratos vinculados a ese cliente (búsqueda
         "por cliente": trae el historial contractual completo, no un match parcial).
      3. 'search' — coincide contra la nomenclatura estandarizada del contrato
         (ej. 'CTR-000041', también acepta '41' o 'ctr41') o su nombre (software
         licenciado / tipo de contrato). No busca por cliente — para eso está
         'cliente_id': mezclar ambos criterios en un solo texto es ambiguo.
      4. Sin filtros: todos los registros (del tenant).
    """
    qs = scoped(Contrato.objects.all(), request)

    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        ids = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
        return qs.filter(id__in=ids)

    cliente_id = request.GET.get('cliente_id', '').strip()
    if cliente_id.isdigit():
        return qs.filter(cliente_id=int(cliente_id))

    search = request.GET.get('search', '').strip()
    if search:
        codigo_id = _parse_codigo_contrato(search)
        if codigo_id is not None:
            return qs.filter(id=codigo_id)

        tipos_matching = [
            value for value, label in TipoContrato.choices
            if search.lower() in label.lower()
        ]
        filtro = Q(software__nombre__icontains=search)
        if search.isdigit():
            filtro |= Q(id=int(search))
        if tipos_matching:
            filtro |= Q(tipo_contrato__in=tipos_matching)
        return qs.filter(filtro)

    return qs


def _tenant_para_importar(request):
    """Tenant destino de una importación. Superadmin debe indicar tenant_id."""
    if request.user.tenant_id is not None:
        return request.user.tenant, None
    tenant_id = request.POST.get('tenant_id') or request.GET.get('tenant_id')
    if not tenant_id:
        return None, JsonResponse({'error': 'Superadmin debe indicar tenant_id.'}, status=400)
    from tenants.models import Tenant
    try:
        return Tenant.objects.get(pk=tenant_id), None
    except (Tenant.DoesNotExist, ValueError):
        return None, JsonResponse({'error': 'Tenant inexistente.'}, status=400)


# ─── EXPORTAR ─────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET"])
@require_feature('descarga_masiva')
def exportar_contratos_excel(request):
    qs = _contratos_filtrados_queryset(request)
    meta = build_audit_meta(
        request,
        titulo="Exportación de Registros - Módulo Contratos",
        filtros_desc=describir_filtros_contratos(request),
    )
    buf = exportar.contratos_a_excel(qs, meta=meta)
    filename = build_export_filename("Contratos", request.user, "xlsx")
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('descarga_masiva')
def exportar_contratos_csv(request):
    qs = _contratos_filtrados_queryset(request)
    buf = exportar.contratos_a_csv(qs)
    filename = build_export_filename("Contratos", request.user, "csv")
    resp = HttpResponse(buf.read(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('descarga_masiva')
def exportar_clientes_excel(request):
    clientes = _clientes_filtrados_ordenados(request)
    meta = build_audit_meta(
        request,
        titulo="Exportación de Registros - Módulo Clientes",
        filtros_desc=describir_filtros_clientes(request),
    )
    buf = exportar.clientes_a_excel(clientes, meta=meta)
    filename = build_export_filename("Clientes", request.user, "xlsx")
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('descarga_masiva')
def exportar_clientes_csv(request):
    clientes = _clientes_filtrados_ordenados(request)
    buf = exportar.clientes_a_csv(clientes)
    filename = build_export_filename("Clientes", request.user, "csv")
    resp = HttpResponse(buf.read(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('documentos')
def exportar_contrato_word(request, contrato_id):
    try:
        contrato = scoped(Contrato.objects.all(), request) \
            .select_related('cliente', 'software', 'sla').get(id=contrato_id)
    except Contrato.DoesNotExist:
        return JsonResponse({"error": "Contrato no encontrado"}, status=404)

    buf = exportar.contrato_a_word(contrato)
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    resp["Content-Disposition"] = f'attachment; filename="contrato_{contrato_id}.docx"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('documentos')
def exportar_contrato_pdf(request, contrato_id):
    try:
        contrato = scoped(Contrato.objects.all(), request) \
            .select_related('cliente', 'software', 'sla').get(id=contrato_id)
    except Contrato.DoesNotExist:
        return JsonResponse({"error": "Contrato no encontrado"}, status=404)

    buf = exportar.contrato_a_pdf(contrato)
    resp = HttpResponse(buf.read(), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="contrato_{contrato_id}.pdf"'
    return resp


@login_required
@require_http_methods(["GET"])
@require_feature('descarga_masiva')
def exportar_reporte_contratos_pdf(request):
    """
    PDF paginado (nunca carga toda la tabla en un solo render: con 50k
    contratos eso hacía el reporte casi inutilizable). Por defecto entrega la
    página 1 con 500 filas; ?page= y ?page_size= (máx 1000) para navegar.
    """
    try:
        page = max(1, int(request.GET.get('page', 1)))
        page_size = min(1000, max(1, int(request.GET.get('page_size', 500))))
    except (ValueError, TypeError):
        page, page_size = 1, 500

    qs = _contratos_filtrados_queryset(request).order_by('-fecha_inicio', '-id')
    total = qs.count()
    offset = (page - 1) * page_size
    pagina_qs = qs[offset:offset + page_size]

    pagina_info = {
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': max(1, -(-total // page_size)),  # ceil division
    }

    meta = build_audit_meta(
        request,
        titulo="Reporte de Contratos",
        filtros_desc=describir_filtros_contratos(request),
    )
    buf = exportar.reporte_contratos_pdf(pagina_qs, meta=meta, pagina_info=pagina_info)
    filename = build_export_filename(f"ReporteContratos-P{page}", request.user, "pdf")
    resp = HttpResponse(buf.read(), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp["X-Total-Pages"] = str(pagina_info['total_pages'])
    resp["X-Total-Count"] = str(total)
    return resp


# ─── IMPORTAR ─────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
@require_feature('documentos')
@require_tenant_write
def importar_clientes_excel(request):
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    tenant, error = _tenant_para_importar(request)
    if error:
        return error

    # Bloquea el import si el tenant ya está en su límite; las filas creadas
    # dentro del import pueden igual toparlo — el conteo fino es post-import.
    try:
        enforce_quota(tenant, 'clientes')
    except QuotaExceeded as exc:
        return JsonResponse(exc.detail, status=403)

    resultado = importar.excel_a_clientes(archivo, tenant)
    return JsonResponse(resultado)


@login_required
@require_http_methods(["POST"])
@require_feature('documentos')
@require_tenant_write
def importar_contratos_excel(request):
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    tenant, error = _tenant_para_importar(request)
    if error:
        return error

    try:
        enforce_quota(tenant, 'contratos')
    except QuotaExceeded as exc:
        return JsonResponse(exc.detail, status=403)

    resultado = importar.excel_a_contratos(archivo, tenant)
    return JsonResponse(resultado)


@login_required
@require_http_methods(["POST"])
@require_feature('documentos')
def extraer_pdf(request):
    """Extrae texto y tablas de un PDF. Devuelve JSON."""
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    texto = importar.extraer_texto_pdf(archivo)
    archivo.seek(0)
    tablas = importar.extraer_tablas_pdf(archivo)

    return JsonResponse({
        "texto": texto,
        "tablas": [t.to_dict(orient="records") for t in tablas],
    })


@login_required
@require_http_methods(["POST"])
@require_feature('documentos')
def extraer_word(request):
    """Extrae texto y tablas de un Word. Devuelve JSON."""
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    texto = importar.extraer_texto_word(archivo)
    archivo.seek(0)
    tablas = importar.extraer_tablas_word(archivo)

    return JsonResponse({
        "texto": texto,
        "tablas": [t.to_dict(orient="records") for t in tablas],
    })


@login_required
@require_http_methods(["POST"])
@require_feature('documentos')
def extraer_pptx(request):
    """Extrae texto de cada slide de un PPTX. Devuelve JSON."""
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    slides = importar.extraer_texto_pptx(archivo)
    return JsonResponse({"slides": slides})
