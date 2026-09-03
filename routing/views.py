from django.shortcuts import render

# Create your views here.
# routing/views.py
# ============================================================
# API Views — thin HTTP wrappers around capacity.py, router.py,
# ml_predictor.py. All input validation happens here; the engine
# modules assume clean inputs.
# ============================================================

import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from routing import capacity, router, ml_predictor

VALID_LIVESTOCK_TYPES = list(capacity.TLU_CONVERSION.keys())

def map_view(request):
    """Serves the main Leaflet map page."""
    return render(request, "../templates/map.html")

def _validate_common_inputs(data):
    """Shared validation for cattle_size, livestock_type, month. Returns (cleaned_dict, error_str_or_None)."""
    try:
        n_cattle = float(data.get("cattle_size"))
        if n_cattle <= 0:
            return None, "cattle_size must be greater than 0."
    except (TypeError, ValueError):
        return None, "cattle_size is required and must be a number."

    livestock_type = data.get("livestock_type", "cattle_adult_zebu")
    if livestock_type not in VALID_LIVESTOCK_TYPES:
        return None, f"livestock_type must be one of: {VALID_LIVESTOCK_TYPES}"

    month = data.get("month")
    if month is None:
        return None, "month is required (e.g. 'August', 'Aug', or 8)."
    try:
        month_clean = capacity.normalize_month(month)
    except ValueError as e:
        return None, str(e)

    return {"n_cattle": n_cattle, "livestock_type": livestock_type, "month": month_clean}, None


# @csrf_exempt
# @require_http_methods(["POST"])
# def find_route(request):
#     """POST /api/find-route/  {lat, lon, cattle_size, livestock_type, month}"""
#     try:
#         data = json.loads(request.body)
#     except json.JSONDecodeError:
#         return JsonResponse({"error": "Invalid JSON body."}, status=400)

#     cleaned, err = _validate_common_inputs(data)
#     if err:
#         return JsonResponse({"error": err}, status=400)

#     try:
#         lat = float(data.get("lat"))
#         lon = float(data.get("lon"))
#     except (TypeError, ValueError):
#         return JsonResponse({"error": "lat and lon are required and must be numbers."}, status=400)

#     if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
#         return JsonResponse({"error": "lat/lon out of valid range."}, status=400)

#     result = router.find_optimal_route(
#         lat, lon, cleaned["n_cattle"], cleaned["month"], cleaned["livestock_type"]
#     )
#     status_code = 400 if "error" in result else 200
#     return JsonResponse(result, status=status_code)

# @csrf_exempt
# @require_http_methods(["POST"])
# def find_route(request):
#     """POST /api/find-route/  {lat, lon, cattle_size, livestock_type, month}"""
#     try:
#         data = json.loads(request.body)
#     except json.JSONDecodeError:
#         return JsonResponse({"error": "Invalid JSON body."}, status=400)

#     cleaned, err = _validate_common_inputs(data)
#     if err:
#         return JsonResponse({"error": err}, status=400)

#     try:
#         lat = float(data.get("lat"))
#         lon = float(data.get("lon"))
#     except (TypeError, ValueError):
#         return JsonResponse({"error": "lat and lon are required and must be numbers."}, status=400)

#     if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
#         return JsonResponse({"error": "lat/lon out of valid range."}, status=400)

#     result = router.find_top_routes(
#         lat, lon, cleaned["n_cattle"], cleaned["month"], cleaned["livestock_type"], top_n=6
#     )
#     status_code = 400 if "error" in result else 200
#     return JsonResponse(result, status=status_code)


@csrf_exempt
@require_http_methods(["POST"])
def find_route(request):
    """POST /api/find-route/  {lat, lon, cattle_size, livestock_type, month}"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    cleaned, err = _validate_common_inputs(data)
    if err:
        return JsonResponse({"error": err}, status=400)

    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "lat and lon are required and must be numbers."}, status=400)

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return JsonResponse({"error": "lat/lon out of valid range."}, status=400)

    result = router.find_best_zone_with_route_alternatives(
        lat, lon, cleaned["n_cattle"], cleaned["month"], cleaned["livestock_type"], n_alternatives=3
    )
    status_code = 400 if "error" in result else 200
    return JsonResponse(result, status=status_code)

@require_http_methods(["GET"])
def zone_detail(request, zone_id):
    """GET /api/zone/<zone_id>/  -> full zone details + monthly calendar"""
    zone_row = capacity.zone_df_attrs[capacity.zone_df_attrs["zone_id"] == zone_id]
    if len(zone_row) == 0:
        return JsonResponse({"error": f"Zone {zone_id} not found."}, status=404)

    zone_info = zone_row.iloc[0].to_dict()
    calendar = capacity.get_seasonal_calendar(zone_id)
    monthly_capacity = capacity.zone_month_capacity_df[
        capacity.zone_month_capacity_df["zone_id"] == zone_id
    ][["month", "capacity_tlu"]].to_dict(orient="records")

    return JsonResponse({
        "zone_id": zone_id,
        "zone_info": zone_info,
        "seasonal_calendar": calendar,
        "monthly_capacity": monthly_capacity,
    })


@csrf_exempt
@require_http_methods(["POST"])
def predict_forecast(request):
    """POST /api/predict/  {zone_id, cattle_size?, livestock_type?}"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    zone_id = data.get("zone_id")
    if zone_id is None:
        return JsonResponse({"error": "zone_id is required."}, status=400)
    try:
        zone_id = int(zone_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "zone_id must be an integer."}, status=400)

    if "cattle_size" in data and data.get("cattle_size") not in (None, ""):
        cleaned, err = _validate_common_inputs({**data, "month": "Jan"})  # month unused here, dummy value
        if err and "month" not in err:
            return JsonResponse({"error": err}, status=400)
        result = ml_predictor.predict_zone_forecast_for_herd(
            zone_id, cleaned["n_cattle"], cleaned["livestock_type"]
        )
    else:
        result = ml_predictor.predict_zone_capacity_forecast(zone_id)

    status_code = 400 if "error" in result else 200
    return JsonResponse(result, status=status_code)