import json
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from contratos.models import Contrato
from clientes.models import Cliente
from clientes.views import get_filtered_clientes_unified
from .services import exportar, importar


def _clientes_filtrados_ordenados(request):
    """Aplica los mismos filtros/búsqueda de la tabla /clientes/ y devuelve
    lista de instancias Cliente en el mismo orden (fecha_registro desc)."""
    unified = get_filtered_clientes_unified(request)
    ids_ordenados = [u['obj'].id for u in unified]
    clientes_map = {c.id: c for c in Cliente.objects.filter(id__in=ids_ordenados)}
    return [clientes_map[cid] for cid in ids_ordenados if cid in clientes_map]


# ─── EXPORTAR ─────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET"])
def exportar_contratos_excel(request):
    qs = Contrato.objects.all()
    buf = exportar.contratos_a_excel(qs)
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="contratos.xlsx"'
    return resp


@login_required
@require_http_methods(["GET"])
def exportar_clientes_excel(request):
    clientes = _clientes_filtrados_ordenados(request)
    buf = exportar.clientes_a_excel(clientes)
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="clientes.xlsx"'
    return resp


@login_required
@require_http_methods(["GET"])
def exportar_clientes_csv(request):
    clientes = _clientes_filtrados_ordenados(request)
    buf = exportar.clientes_a_csv(clientes)
    resp = HttpResponse(buf.read(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="clientes.csv"'
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
    qs = Contrato.objects.all()
    buf = exportar.reporte_contratos_pdf(qs)
    resp = HttpResponse(buf.read(), content_type="application/pdf")
    resp["Content-Disposition"] = 'attachment; filename="reporte_contratos.pdf"'
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
