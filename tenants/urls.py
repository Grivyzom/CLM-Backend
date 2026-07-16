from django.urls import path

from . import views

urlpatterns = [
    path('', views.TenantListCreateView.as_view(), name='tenant_list_create'),
    path('usuarios/todos/', views.PlatformUserListView.as_view(), name='platform_user_list'),
    path('usuarios/todos/<int:pk>/', views.PlatformUserDetailView.as_view(), name='platform_user_detail'),
    path('usuarios/todos/<int:pk>/reset-password/', views.PlatformUserResetPasswordView.as_view(), name='platform_user_reset_password'),
    path('usuarios/', views.TenantUserListCreateView.as_view(), name='tenant_user_list_create'),
    path('usuarios/<int:pk>/', views.TenantUserDetailView.as_view(), name='tenant_user_detail'),
    path('<uuid:pk>/', views.TenantDetailView.as_view(), name='tenant_detail'),
    path('<uuid:pk>/membresias/', views.TenantMembresiaView.as_view(), name='tenant_membresias'),
]
