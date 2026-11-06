from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
    path('api/', include('client_reports.urls')),
    path('api/', include('calculators.urls')),
    path('api/', include('fund_analyzer.urls')),
]
