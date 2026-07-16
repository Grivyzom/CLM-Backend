from django.urls import path
from . import views

urlpatterns = [
    path("plantillas/", views.PlantillaListCreateView.as_view(), name="plantillas_list_create"),
    path("plantillas/<int:pk>/", views.PlantillaDetailView.as_view(), name="plantillas_detail"),
    path("plantillas/<int:pk>/preview-pdf/", views.PlantillaPreviewPDFView.as_view(), name="plantillas_preview_pdf"),
    path("plantillas/<int:pk>/regenerar-preview/", views.PlantillaRegenerarPreviewView.as_view(), name="plantillas_regenerar_preview"),
    path("plantillas/html-templates/", views.AvailableHtmlTemplatesView.as_view(), name="plantillas_html_templates"),

    path("documentos/", views.DocumentoGeneradoListView.as_view(), name="documentos_generados_list"),
    path("documentos/campos/", views.CamposPlantillaView.as_view(), name="documentos_campos"),
    path("documentos/generar/", views.GenerarDocumentoView.as_view(), name="documentos_generar"),
    path("documentos/<int:pk>/pdf/", views.DescargarPDFView.as_view(), name="documentos_pdf"),
    path("documentos/<int:pk>/docx/", views.DescargarDocxView.as_view(), name="documentos_docx"),

    path("emitidos/", views.EmitidosListView.as_view(), name="emitidos_list"),

    path("clausulas/", views.ClausulaListView.as_view(), name="clausulas_list"),
    path("clausulas/<int:pk>/", views.ClausulaDetailView.as_view(), name="clausula_detail"),
]

