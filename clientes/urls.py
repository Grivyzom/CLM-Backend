from django.urls import path
from . import views

urlpatterns = [
    path('', views.ClienteListView.as_view(), name='cliente-list'),
    path('stats/', views.ClienteStatsView.as_view(), name='cliente-stats'),
    path('<int:pk>/', views.ClienteDetailView.as_view(), name='cliente-detail'),
]
