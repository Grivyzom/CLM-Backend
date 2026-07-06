from django.urls import path
from . import views

urlpatterns = [
    # ── Exportar ──────────────────────────────────────────────────────────────
    path("exportar/contratos/excel/", views.exportar_contratos_excel, name="exportar_contratos_excel"),
    path("exportar/contratos/csv/", views.exportar_contratos_csv, name="exportar_contratos_csv"),
    path("exportar/clientes/excel/", views.exportar_clientes_excel, name="exportar_clientes_excel"),
    path("exportar/clientes/csv/", views.exportar_clientes_csv, name="exportar_clientes_csv"),
    path("exportar/contratos/pdf/", views.exportar_reporte_contratos_pdf, name="exportar_reporte_contratos_pdf"),
    path("exportar/contrato/<int:contrato_id>/word/", views.exportar_contrato_word, name="exportar_contrato_word"),
    path("exportar/contrato/<int:contrato_id>/pdf/", views.exportar_contrato_pdf, name="exportar_contrato_pdf"),

    # ── Importar ──────────────────────────────────────────────────────────────
    path("importar/clientes/excel/", views.importar_clientes_excel, name="importar_clientes_excel"),
    path("importar/contratos/excel/", views.importar_contratos_excel, name="importar_contratos_excel"),

    # ── Extraer datos (sin persistir) ─────────────────────────────────────────
    path("extraer/pdf/", views.extraer_pdf, name="extraer_pdf"),
    path("extraer/word/", views.extraer_word, name="extraer_word"),
    path("extraer/pptx/", views.extraer_pptx, name="extraer_pptx"),
]
