from django.urls import path
from . import views

urlpatterns = [
    path('', views.ClienteListView.as_view(), name='cliente-list'),
    path('stats/', views.ClienteStatsView.as_view(), name='cliente-stats'),
    path('<int:pk>/', views.ClienteDetailView.as_view(), name='cliente-detail'),
    path('<int:pk>/workspace/', views.ClienteWorkspaceView.as_view(), name='cliente-workspace'),
    path('<int:pk>/timeline-pagos/', views.ClienteTimelinePagosView.as_view(), name='cliente-timeline-pagos'),
    path('<int:pk>/correos/', views.ClienteCorreosView.as_view(), name='cliente-correos'),
    path('<int:pk>/enviar-correo/', views.ClienteEnviarCorreoView.as_view(), name='cliente-enviar-correo'),
    path('<int:pk>/archivos-adjuntables/', views.ClienteArchivosAdjuntablesView.as_view(), name='cliente-archivos-adjuntables'),
]
