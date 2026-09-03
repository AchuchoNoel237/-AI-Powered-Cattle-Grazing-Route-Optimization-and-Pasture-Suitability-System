# routing/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.map_view, name='map_view'),
    path('find-route/', views.find_route, name='find_route'),
    path('zone/<int:zone_id>/', views.zone_detail, name='zone_detail'),
    path('predict/', views.predict_forecast, name='predict_forecast'),
]