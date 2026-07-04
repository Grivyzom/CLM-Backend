import json
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from contratos.models import Contrato
from clientes.models import Cliente
from clientes.views import get_filtered_clientes_unified
from .services import exportar, importar
from .services.auditoria import (
    build_export_filename, build_audit_meta,
    describir_filtros_clientes, describir_filtros_contratos,
)


def _clientes_filtrados_ordenados(request):
    """Si viene 'ids' (csv de IDs, ej. selección manual del usuario), exporta SOLO esos,
    en el orden recibido. Si no, aplica los mismos filtros/búsqueda de la tabla /clientes/
    y devuelve la lista completa filtrada (orden fecha_registro desc)."""
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        ids_ordenados = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
    else:
        unified = get_filtered_clientes_unified(request.GET)
        ids_ordenados = [u['obj'].id for u in unified]

    clientes_map = {c.id: c for c in Cliente.objects.filter(id__in=ids_ordenados)}
    return [clientes_map[cid] for cid in ids_ordenados if cid in clientes_map]


def _contratos_filtrados_queryset(request):
    """Si viene 'ids' (csv de IDs, selección manual), filtra a solo esos contratos.
    Si no, devuelve todos (aún no existe UI de filtros para la tabla de contratos)."""
    qs = Contrato.objects.all()
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        ids = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
        qs = qs.filter(id__in=ids)
    return qs


# ─── EXPORTAR ─────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET"])
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
def exportar_clientes_csv(request):
    clientes = _clientes_filtrados_ordenados(request)
    buf = exportar.clientes_a_csv(clientes)
    filename = build_export_filename("Clientes", request.user, "csv")
    resp = HttpResponse(buf.read(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["GET"])
def exportar_contrato_word(request, contrato_id):
    try:
        contrato = Contrato.objects.select_related('cliente', 'software', 'sla').get(id=contrato_id)
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
def exportar_contrato_pdf(request, contrato_id):
    try:
        contrato = Contrato.objects.select_related('cliente', 'software', 'sla').get(id=contrato_id)
    except Contrato.DoesNotExist:
        return JsonResponse({"error": "Contrato no encontrado"}, status=404)

    buf = exportar.contrato_a_pdf(contrato)
    resp = HttpResponse(buf.read(), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="contrato_{contrato_id}.pdf"'
    return resp


@login_required
@require_http_methods(["GET"])
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
def importar_clientes_excel(request):
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    resultado = importar.excel_a_clientes(archivo)
    return JsonResponse(resultado)


@login_required
@require_http_methods(["POST"])
def importar_contratos_excel(request):
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    resultado = importar.excel_a_contratos(archivo)
    return JsonResponse(resultado)


@login_required
@require_http_methods(["POST"])
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
def extraer_pptx(request):
    """Extrae texto de cada slide de un PPTX. Devuelve JSON."""
    archivo = request.FILES.get("archivo")
    if not archivo:
        return JsonResponse({"error": "Se requiere campo 'archivo'"}, status=400)

    slides = importar.extraer_texto_pptx(archivo)
    return JsonResponse({"slides": slides})
