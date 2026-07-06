from django.urls import path
from . import views

urlpatterns = [
    path('software/', views.SoftwareListView.as_view(), name='software-list'),
    path('productos/', views.ProductoListCreateView.as_view(), name='producto-list-create'),
    path('productos/<int:pk>/', views.ProductoDetailView.as_view(), name='producto-detail'),
]
