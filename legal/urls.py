from django.urls import path
from .views import AuditoriaView

urlpatterns = [
    path('auditoria/', AuditoriaView.as_view(), name='legal_auditoria'),
]
