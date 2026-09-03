from django.db import models

# Create your models here.

# routing/models.py
# ============================================================
# No database models needed yet — all pasture/zone/capacity data
# is loaded from files (CSV/GeoTIFF/GeoPackage/pickled models) at
# Django startup and cached in memory, per the project spec.
# This file is a placeholder for now; we may add a RouteRequest
# model later if we want to log/save user queries to a database.
# ============================================================

from django.db import models

# Example for later (not active yet):
# class RouteRequestLog(models.Model):
#     timestamp = models.DateTimeField(auto_now_add=True)
#     lat = models.FloatField()
#     lon = models.FloatField()
#     cattle_size = models.IntegerField()
#     livestock_type = models.CharField(max_length=50)
#     month = models.CharField(max_length=20)
#     zone_id = models.IntegerField(null=True)