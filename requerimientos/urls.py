from django.urls import path
from . import views

urlpatterns = [
    path("plantillas/", views.PlantillaActivaView.as_view(), name="requerimientos_plantilla_activa"),

    path("", views.RequerimientoListCreateView.as_view(), name="requerimientos_list_create"),
    path("<int:pk>/", views.RequerimientoDetailView.as_view(), name="requerimientos_detail"),
    path("<int:pk>/generar/", views.GenerarDocumentoView.as_view(), name="requerimientos_generar"),

    path("documentos/<int:pk>/docx/", views.DescargarDocxView.as_view(), name="requerimientos_docx"),
    path("documentos/<int:pk>/pdf/", views.DescargarPDFView.as_view(), name="requerimientos_pdf"),
]
