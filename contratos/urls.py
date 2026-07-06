from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('slas/', views.SLAListView.as_view(), name='sla-list'),
    path('contratos/', views.ContratoListCreateView.as_view(), name='contrato-list-create'),
    path('contratos/stats/', views.ContratoStatsView.as_view(), name='contrato-stats'),
    path('contratos/<int:pk>/', views.ContratoDetailView.as_view(), name='contrato-detail'),
    path('contratos/<int:contrato_id>/obligaciones/', views.ObligacionListCreateView.as_view(), name='contrato-obligaciones-list-create'),
    path('obligaciones/<int:pk>/', views.ObligacionDetailView.as_view(), name='obligacion-detail'),
    path('obligaciones/<int:pk>/historial/', views.ObligacionHistorialView.as_view(), name='obligacion-historial'),
    path('contratos/<int:pk>/enmendar/', views.ContratoEnmendarView.as_view(), name='contrato-enmendar'),
]
