from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Client
from .serializers import ClientSerializer, PlanSerializer, plan_data, save_plan


class ClientListCreateView(generics.ListCreateAPIView):
    serializer_class = ClientSerializer

    def get_queryset(self):
        return Client.objects.filter(advisor=self.request.user)

    def perform_create(self, serializer):
        serializer.save(advisor=self.request.user)


class ClientPlanView(APIView):
    """GET loads a client's saved plan; PUT saves all plan tabs into their tables."""

    def get_client(self, request, pk):
        return get_object_or_404(Client, pk=pk, advisor=request.user)

    def get(self, request, pk):
        return Response(plan_data(self.get_client(request, pk)))

    def put(self, request, pk):
        client = self.get_client(request, pk)
        serializer = PlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        save_plan(client, serializer.validated_data)
        return Response(plan_data(client))
