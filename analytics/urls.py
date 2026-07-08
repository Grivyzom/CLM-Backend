from django.urls import path
from . import views

urlpatterns = [
    path('', views.AnalyticsView.as_view(), name='analytics'),
]
