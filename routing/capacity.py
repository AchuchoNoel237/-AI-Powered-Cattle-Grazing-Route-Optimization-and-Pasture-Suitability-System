# routing/capacity.py
# ============================================================
# Carrying Capacity Engine — wraps Phase 5's query functions
# Loads all zone/capacity data ONCE at Django startup (module-level),
# so every request reuses the same in-memory data instead of
# re-reading files from disk on every API call.
# ============================================================

import os
import json
import numpy as np
import pandas as pd
import rasterio
from django.conf import settings

DATA_DIR = settings.DATA_DIR

month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
FULL_MONTH_NAMES = ["January","February","March","April","May","June",
                     "July","August","September","October","November","December"]

MONTH_LOOKUP = {}
for i, (abbr, full) in enumerate(zip(month_names, FULL_MONTH_NAMES), start=1):
    MONTH_LOOKUP[abbr.lower()] = abbr
    MONTH_LOOKUP[full.lower()] = abbr
    MONTH_LOOKUP[str(i)] = abbr

def normalize_month(month):
    """Accepts int (1-12), abbreviation ('Aug'), or full name ('August')."""
    if isinstance(month, int):
        return month_names[month - 1]
    key = str(month).strip().lower()
    if key not in MONTH_LOOKUP:
        raise ValueError(f"Unrecognized month '{month}'. Use 1-12, 'Aug', or 'August'.")
    return MONTH_LOOKUP[key]

# ============================================================
# LOAD DATA ONCE AT MODULE IMPORT (Django startup)
# ============================================================

print("📦 [capacity.py] Loading zone data into memory...")

zone_df_attrs = pd.read_csv(os.path.join(DATA_DIR, "suitability", "grazing_zones_attributes.csv"))

with rasterio.open(os.path.join(DATA_DIR, "suitability", "grazing_zones_raster.tif")) as src:
    zone_raster = src.read(1)

zone_month_capacity_df = pd.read_csv(os.path.join(DATA_DIR, "capacity", "zone_month_capacity.csv"))

with open(os.path.join(DATA_DIR, "parameters.json")) as f:
    PARAMETERS = json.load(f)

CC_CONSTANTS = PARAMETERS["carrying_capacity_constants"]
TLU_CONVERSION = PARAMETERS["tlu_conversion_factors"]

# Biomass monthly mean — needed for grazing duration estimator
with rasterio.open(os.path.join(DATA_DIR, "biomass", "biomass_monthly_mean_2016-2025.tif")) as src:
    biomass_monthly_mean = np.stack([
        np.where(src.read(m + 1) == src.nodata, np.nan, src.read(m + 1))
        for m in range(12)
    ], axis=0)

print(f"✅ [capacity.py] Loaded {len(zone_df_attrs)} zones, "
      f"{len(zone_month_capacity_df)} zone-month capacity rows")

DAYS_PER_MONTH = 30

# ============================================================
# Helper
# ============================================================

def cattle_to_tlu(n_cattle, livestock_type):
    if livestock_type not in TLU_CONVERSION:
        raise ValueError(f"Unknown livestock_type '{livestock_type}'. Valid: {list(TLU_CONVERSION.keys())}")
    return n_cattle * TLU_CONVERSION[livestock_type]

# ============================================================
# Query Function 1: query_zone_capacity
# ============================================================

def query_zone_capacity(zone_id, month, n_cattle, livestock_type):
    month_name = normalize_month(month)
    row = zone_month_capacity_df[
        (zone_month_capacity_df["zone_id"] == zone_id) &
        (zone_month_capacity_df["month"] == month_name)
    ]
    if len(row) == 0:
        return {"error": f"Zone {zone_id} or month '{month_name}' not found."}

    available_capacity_tlu = float(row["capacity_tlu"].values[0])
    requested_tlu = cattle_to_tlu(n_cattle, livestock_type)
    can_sustain = requested_tlu <= available_capacity_tlu
    surplus_deficit = available_capacity_tlu - requested_tlu
    utilization_pct = (requested_tlu / available_capacity_tlu * 100) if available_capacity_tlu > 0 else None

    return {
        "zone_id": zone_id, "month": month_name, "requested_cattle": n_cattle,
        "livestock_type": livestock_type, "requested_tlu": round(requested_tlu, 2),
        "available_capacity_tlu": round(available_capacity_tlu, 2), "can_sustain": bool(can_sustain),
        "surplus_deficit_tlu": round(surplus_deficit, 2),
        "utilization_pct": round(utilization_pct, 1) if utilization_pct is not None else None,
    }

# ============================================================
# Query Function 2: query_best_zones
# ============================================================

def query_best_zones(month, n_cattle, livestock_type="cattle_adult_zebu", top_n=10):
    month_name = normalize_month(month)
    requested_tlu = cattle_to_tlu(n_cattle, livestock_type)

    month_data = zone_month_capacity_df[zone_month_capacity_df["month"] == month_name].copy()
    month_data["requested_tlu"] = requested_tlu
    month_data["can_sustain"] = month_data["capacity_tlu"] >= requested_tlu
    month_data["surplus_tlu"] = month_data["capacity_tlu"] - requested_tlu

    qualifying = month_data[month_data["can_sustain"]].sort_values("surplus_tlu", ascending=False)
    result = qualifying.merge(
        zone_df_attrs[["zone_id", "quality", "centroid_lat", "centroid_lon", "area_km2"]],
        on="zone_id", how="left"
    )
    return result.head(top_n).reset_index(drop=True)

# ============================================================
# Query Function 3: get_seasonal_calendar
# ============================================================

def get_seasonal_calendar(zone_id=None):
    if zone_id is not None:
        subset = zone_month_capacity_df[zone_month_capacity_df["zone_id"] == zone_id]
        if len(subset) == 0:
            return {"error": f"Zone {zone_id} not found."}
        best_row = subset.loc[subset["capacity_tlu"].idxmax()]
        worst_row = subset.loc[subset["capacity_tlu"].idxmin()]
        return {
            "zone_id": zone_id, "best_month": best_row["month"],
            "best_month_capacity_tlu": float(best_row["capacity_tlu"]),
            "worst_month": worst_row["month"], "worst_month_capacity_tlu": float(worst_row["capacity_tlu"]),
        }
    else:
        return pd.DataFrame([get_seasonal_calendar(zid) for zid in zone_df_attrs["zone_id"].unique()])

# ============================================================
# Query Function 4: query_grazing_duration (capped at monthly horizon)
# ============================================================

def query_grazing_duration(zone_id, month, n_cattle, livestock_type="cattle_adult_zebu"):
    month_name = normalize_month(month)
    month_idx = month_names.index(month_name)

    zone_row = zone_df_attrs[zone_df_attrs["zone_id"] == zone_id]
    if len(zone_row) == 0:
        return {"error": f"Zone {zone_id} not found."}
    area_km2 = float(zone_row.iloc[0]["area_km2"])

    zone_mask = (zone_raster == zone_id)
    mean_biomass_kg_dm_ha = float(np.nanmean(biomass_monthly_mean[month_idx][zone_mask]))

    if np.isnan(mean_biomass_kg_dm_ha) or mean_biomass_kg_dm_ha <= 0:
        return {"error": f"No valid biomass data for zone {zone_id} in {month_name}."}

    area_ha = area_km2 * CC_CONSTANTS["PIXEL_AREA_HA"]
    total_biomass_kg = mean_biomass_kg_dm_ha * area_ha
    sustainable_biomass_kg = total_biomass_kg * CC_CONSTANTS["UTILIZATION_RATE"]

    requested_tlu = cattle_to_tlu(n_cattle, livestock_type)
    daily_consumption_kg = requested_tlu * CC_CONSTANTS["DAILY_INTAKE_PER_TLU_KG"]

    if daily_consumption_kg <= 0:
        return {"error": "n_cattle must be greater than 0."}

    raw_days_to_ceiling = sustainable_biomass_kg / daily_consumption_kg
    monthly_utilization_pct = (daily_consumption_kg * DAYS_PER_MONTH / sustainable_biomass_kg) * 100

    if raw_days_to_ceiling >= DAYS_PER_MONTH:
        status = "within_monthly_capacity"
        practical_days = DAYS_PER_MONTH
        message = (f"This herd uses only {monthly_utilization_pct:.1f}% of the zone's sustainable "
                   f"monthly forage — can graze the full month without hitting the ceiling.")
    else:
        status = "depletes_within_month"
        practical_days = raw_days_to_ceiling
        message = (f"This herd will reach the sustainable utilization ceiling in "
                   f"{practical_days:.1f} days — should move to a new zone before then.")

    return {
        "zone_id": zone_id, "month": month_name, "n_cattle": n_cattle,
        "livestock_type": livestock_type, "requested_tlu": round(requested_tlu, 2),
        "mean_biomass_kg_dm_ha": round(mean_biomass_kg_dm_ha, 1),
        "monthly_utilization_pct": round(monthly_utilization_pct, 2),
        "status": status, "practical_grazing_days": round(practical_days, 1),
        "message": message,
    }