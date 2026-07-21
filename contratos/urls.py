from django.urls import path
from . import views
from . import guest_views
from .resumen_inicio import ResumenInicioView

urlpatterns = [
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('dashboard/resumen-inicio/', ResumenInicioView.as_view(), name='dashboard-resumen-inicio'),
    path('slas/', views.SLAListView.as_view(), name='sla-list'),
    path('slas/na/', views.SLANAView.as_view(), name='sla-na'),
    path('contratos/', views.ContratoListCreateView.as_view(), name='contrato-list-create'),
    path('contratos/stats/', views.ContratoStatsView.as_view(), name='contrato-stats'),
    path('contratos/<int:pk>/', views.ContratoDetailView.as_view(), name='contrato-detail'),
    path('contratos/<int:contrato_id>/obligaciones/', views.ObligacionListCreateView.as_view(), name='contrato-obligaciones-list-create'),
    path('obligaciones/<int:pk>/', views.ObligacionDetailView.as_view(), name='obligacion-detail'),
    path('obligaciones/<int:pk>/historial/', views.ObligacionHistorialView.as_view(), name='obligacion-historial'),
    path('contratos/<int:pk>/enmendar/', views.ContratoEnmendarView.as_view(), name='contrato-enmendar'),
    path('contratos/<int:pk>/external-sync/', views.ContratoExternalSyncView.as_view(), name='contrato-external-sync'),
    path('contratos/<int:pk>/firma/', views.ContratoFirmaElectronicaView.as_view(), name='contrato-firma-electronica'),
    path('contratos/firmar/<str:token>/', views.api_firma_token_info, name='firma-token-info'),
    path('contratos/firmar/<str:token>/confirmar/', views.api_firma_token_confirmar, name='firma-token-confirmar'),
    path('contratos/<int:contrato_id>/comentarios/', views.ComentarioListCreateView.as_view(), name='contrato-comentarios-list-create'),
    path('comentarios/<int:pk>/', views.ComentarioDetailView.as_view(), name='comentario-detail'),
    
    # Guest Portal Endpoints
    path('contratos/<int:pk>/guest-link/', guest_views.GenerateGuestLinkView.as_view(), name='generate-guest-link'),
    path('guest/contracts/<str:token>/', guest_views.GuestContractView.as_view(), name='guest-contract-view'),
    path('guest/contracts/<str:token>/comments/', guest_views.GuestCommentView.as_view(), name='guest-contract-comments'),
    path('guest/contracts/<str:token>/sign/', guest_views.GuestSignView.as_view(), name='guest-contract-sign'),
]
