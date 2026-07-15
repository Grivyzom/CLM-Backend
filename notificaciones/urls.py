from django.urls import path
from . import views

urlpatterns = [
    path('', views.NotificacionListCreateView.as_view(), name='notificacion-list'),
    path('unread-count/', views.NotificacionUnreadCountView.as_view(), name='notificacion-unread-count'),
    path('leer-todas/', views.NotificacionLeerTodasView.as_view(), name='notificacion-leer-todas'),
    path('<int:pk>/leer/', views.NotificacionMarcarLeidaView.as_view(), name='notificacion-leer'),
]
