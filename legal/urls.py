from django.urls import path
from .views import AuditoriaView, AnalisisIAView, AnalizarIAView

urlpatterns = [
    path('auditoria/', AuditoriaView.as_view(), name='legal_auditoria'),
    path('contratos/<int:contrato_id>/analisis-ia/', AnalisisIAView.as_view(), name='legal_analisis_ia'),
    path('contratos/<int:contrato_id>/analisis-ia/analizar/', AnalizarIAView.as_view(), name='legal_analizar_ia'),
]

