from django.urls import path
from . import views

urlpatterns = [
    path("plantillas/", views.PlantillaListCreateView.as_view(), name="plantillas_list_create"),
    path("plantillas/<int:pk>/", views.PlantillaDetailView.as_view(), name="plantillas_detail"),

    path("documentos/", views.DocumentoGeneradoListView.as_view(), name="documentos_generados_list"),
    path("documentos/generar/", views.GenerarDocumentoView.as_view(), name="documentos_generar"),
    path("documentos/<int:pk>/pdf/", views.DescargarPDFView.as_view(), name="documentos_pdf"),
    path("documentos/<int:pk>/docx/", views.DescargarDocxView.as_view(), name="documentos_docx"),

    path("clausulas/", views.ClausulaListView.as_view(), name="clausulas_list"),
    path("clausulas/<int:pk>/", views.ClausulaDetailView.as_view(), name="clausula_detail"),
]
