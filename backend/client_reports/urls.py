from django.urls import path

from .views import ClientDetailView, ClientListCreateView, ClientPlanView

urlpatterns = [
    path('clients/', ClientListCreateView.as_view(), name='client-list'),
    path('clients/<int:pk>/', ClientDetailView.as_view(), name='client-detail'),
    path('clients/<int:pk>/plan/', ClientPlanView.as_view(), name='client-plan'),
]
