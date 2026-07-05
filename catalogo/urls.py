from django.urls import path
from . import views

urlpatterns = [
    path('productos/', views.ProductoListCreateView.as_view(), name='producto-list-create'),
    path('productos/<int:pk>/', views.ProductoDetailView.as_view(), name='producto-detail'),
]
