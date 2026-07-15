from django.urls import path

from . import views

urlpatterns = [
    path('', views.IncidenciaListCreateView.as_view(), name='incidencia-list-create'),
    path('stats/', views.IncidenciaStatsView.as_view(), name='incidencia-stats'),
    path('<int:pk>/', views.IncidenciaDetailView.as_view(), name='incidencia-detail'),
    path('<int:pk>/comentarios/', views.ComentarioListCreateView.as_view(), name='incidencia-comentarios'),
]
