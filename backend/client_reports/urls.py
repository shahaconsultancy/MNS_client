from django.urls import path

from .views import ClientListCreateView, ClientPlanView

urlpatterns = [
    path('clients/', ClientListCreateView.as_view(), name='client-list'),
    path('clients/<int:pk>/plan/', ClientPlanView.as_view(), name='client-plan'),
]
