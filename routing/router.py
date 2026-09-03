# routing/router.py

import os
import json
import numpy as np
import pandas as pd
import rasterio
import networkx as nx
from pyproj import Transformer
from django.conf import settings

from routing.capacity import (
    zone_df_attrs, zone_month_capacity_df, normalize_month,
    cattle_to_tlu, month_names,
)

DATA_DIR = settings.DATA_DIR

print("📦 [router.py] Loading cost surface + building routing graph...")

with open(os.path.join(DATA_DIR, "manifest.json")) as f:
    manifest = json.load(f)

GRID_WIDTH = manifest["grid_width"]
GRID_HEIGHT = manifest["grid_height"]
GRID_TRANSFORM = manifest["grid_transform"]
GRID_CRS = manifest["crs"]
transform_obj = rasterio.Affine(*GRID_TRANSFORM[:6])
PIXEL_SIZE_M = abs(transform_obj[0])

with rasterio.open(os.path.join(DATA_DIR, "routing", "cost_surface.tif")) as src:
    cost_surface = src.read(1)
    cost_surface = np.where(cost_surface == src.nodata, np.nan, cost_surface)

with rasterio.open(os.path.join(DATA_DIR, "suitability", "grazing_zones_raster.tif")) as src:
    zone_raster = src.read(1)

with rasterio.open(os.path.join(DATA_DIR, "biomass", "biomass_monthly_mean_2016-2025.tif")) as src:
    biomass_monthly_mean = np.stack([
        np.where(src.read(m + 1) == src.nodata, np.nan, src.read(m + 1))
        for m in range(12)
    ], axis=0)

with rasterio.open(os.path.join(DATA_DIR, "static", "distance_to_water.tif")) as src:
    dist_water_raster = src.read(1)
    dist_water_raster = np.where(dist_water_raster == src.nodata, np.nan, dist_water_raster)

month_names_full = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


transformer_to_wgs84 = Transformer.from_crs(GRID_CRS, "EPSG:4326", always_xy=True)
transformer_to_utm = Transformer.from_crs("EPSG:4326", GRID_CRS, always_xy=True)

def latlon_to_rowcol(lat, lon):
    x, y = transformer_to_utm.transform(lon, lat)
    col, row = ~transform_obj * (x, y)
    return int(round(row)), int(round(col))

def rowcol_to_latlon(row, col):
    x, y = transform_obj * (col, row)
    lon, lat = transformer_to_wgs84.transform(x, y)
    return lat, lon

# ============================================================
# Build routable graph (8-connectivity, vectorized) — ONCE
# ============================================================

valid_mask = ~np.isnan(cost_surface)
node_ids = np.full((GRID_HEIGHT, GRID_WIDTH), -1, dtype="int64")
node_ids[valid_mask] = np.arange(valid_mask.sum())
n_nodes = valid_mask.sum()

directions = [
    (0, 1, PIXEL_SIZE_M),
    (1, 0, PIXEL_SIZE_M),
    (1, 1, PIXEL_SIZE_M * np.sqrt(2)),
    (1, -1, PIXEL_SIZE_M * np.sqrt(2)),
]

edges_u, edges_v, edges_weight, edges_dist = [], [], [], []

for dr, dc, dist_m in directions:
    r0, r1 = max(0, -dr), GRID_HEIGHT - max(0, dr)
    c0, c1 = max(0, -dc), GRID_WIDTH - max(0, dc)

    src_ids = node_ids[r0:r1, c0:c1]
    dst_ids = node_ids[r0+dr:r1+dr, c0+dc:c1+dc]
    src_cost = cost_surface[r0:r1, c0:c1]
    dst_cost = cost_surface[r0+dr:r1+dr, c0+dc:c1+dc]

    both_valid = (src_ids >= 0) & (dst_ids >= 0)
    u = src_ids[both_valid]
    v = dst_ids[both_valid]
    avg_cost = (src_cost[both_valid] + dst_cost[both_valid]) / 2.0
    w = avg_cost * dist_m
    d = np.full(u.shape, dist_m)

    edges_u.append(u); edges_v.append(v)
    edges_weight.append(w); edges_dist.append(d)

edges_u = np.concatenate(edges_u)
edges_v = np.concatenate(edges_v)
edges_weight = np.concatenate(edges_weight)
edges_dist = np.concatenate(edges_dist)

G = nx.Graph()
G.add_nodes_from(range(n_nodes))
G.add_edges_from([
    (int(u), int(v), {"weight": float(w), "dist_m": float(d)})
    for u, v, w, d in zip(edges_u, edges_v, edges_weight, edges_dist)
])

rows_all, cols_all = np.where(valid_mask)
node_to_rc = {int(node_ids[r, c]): (r, c) for r, c in zip(rows_all, cols_all)}

print(f"✅ [router.py] Graph built: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

# --- Zone centroid -> nearest valid node lookup (built once) ---
zone_centroid_nodes = {}
for _, row in zone_df_attrs.iterrows():
    zid = int(row["zone_id"])
    zone_mask = (zone_raster == zid)
    rows_idx, cols_idx = np.where(zone_mask)
    center_row = int(round(rows_idx.mean()))
    center_col = int(round(cols_idx.mean()))
    if node_ids[center_row, center_col] >= 0:
        zone_centroid_nodes[zid] = int(node_ids[center_row, center_col])
    else:
        dists = (rows_idx - center_row)**2 + (cols_idx - center_col)**2
        nearest_idx = np.argmin(dists)
        zone_centroid_nodes[zid] = int(node_ids[rows_idx[nearest_idx], cols_idx[nearest_idx]])

print(f"✅ [router.py] Mapped {len(zone_centroid_nodes)} zone centroids to graph nodes")

TRANSHUMANCE_SPEED_KM_PER_DAY = 15

# ============================================================
# MINOR PASTURE PATCHES + WATER POINTS
# Computed ONCE at startup, using the SAME connected-component
# methodology as the official 104 zones (Phase 4), but with a
# much smaller minimum area, and explicitly EXCLUDING pixels
# already part of the 104 official zones — surfaces smaller
# suitable patches that were too small to qualify as an official
# zone. Water points are individual permanent water bodies
# labeled from the same water mask used throughout the project.
# No capacity/quality data is computed for these — area and
# suitability score only, per your instruction.
# ============================================================

from scipy import ndimage
from scipy.ndimage import uniform_filter
from shapely.geometry import LineString, Point

print("📦 [router.py] Building minor pasture patches + water points...")

MINOR_MIN_AREA_KM2 = 1        # vs 5 km² for the official 104 zones
MINOR_SCORE_THRESHOLD = 35    # same threshold used for the official zones
MINOR_SMOOTHING_SIZE = 5      # same smoothing used for the official zones

with rasterio.open(os.path.join(DATA_DIR, "suitability", "suitability_score_monthly_0-100.tif")) as src:
    suitability_score_monthly = np.stack([
        np.where(src.read(m + 1) == src.nodata, np.nan, src.read(m + 1))
        for m in range(12)
    ], axis=0)

annual_mean_score = np.nanmean(suitability_score_monthly, axis=0)

score_filled = np.nan_to_num(annual_mean_score, nan=0.0)
valid_mask_f = (~np.isnan(annual_mean_score)).astype("float32")
smoothed_sum = uniform_filter(score_filled, size=MINOR_SMOOTHING_SIZE, mode="constant", cval=0.0)
smoothed_count = uniform_filter(valid_mask_f, size=MINOR_SMOOTHING_SIZE, mode="constant", cval=0.0)
with np.errstate(invalid="ignore", divide="ignore"):
    score_smoothed = smoothed_sum / smoothed_count
score_smoothed = np.where(smoothed_count > 0, score_smoothed, np.nan)

# Candidate pixels: pass the score threshold AND not already part of an official zone
candidate_mask = (score_smoothed >= MINOR_SCORE_THRESHOLD) & (~np.isnan(score_smoothed)) & (zone_raster == 0)

structure_4conn = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
labeled_minor, n_raw_minor = ndimage.label(candidate_mask, structure=structure_4conn)

minor_sizes = ndimage.sum(candidate_mask, labeled_minor, range(1, n_raw_minor + 1))
valid_minor_ids = np.where(minor_sizes >= MINOR_MIN_AREA_KM2)[0] + 1  # 1 km² = 1 pixel at 1km res

MINOR_PATCHES = []
for mid in valid_minor_ids:
    mask = (labeled_minor == mid)
    n_pixels = int(mask.sum())
    rows_idx, cols_idx = np.where(mask)
    center_row, center_col = rows_idx.mean(), cols_idx.mean()
    x_map, y_map = transform_obj * (center_col, center_row)
    lon, lat = transformer_to_wgs84.transform(x_map, y_map)
    mean_score = float(np.nanmean(score_smoothed[mask]))
    MINOR_PATCHES.append({
        "area_km2": round(n_pixels * 1.0, 1),
        "mean_score": round(mean_score, 1),
        "lat": round(lat, 6), "lon": round(lon, 6),
        "utm_x": x_map, "utm_y": y_map,
    })

print(f"✅ [router.py] Found {len(MINOR_PATCHES)} minor pasture patches "
      f"(< 5 km², not part of the 104 official zones)")

# --- Water points: label individual permanent water bodies ---
with rasterio.open(os.path.join(DATA_DIR, "static", "water_mask.tif")) as src:
    water_mask_raw = src.read(1)
    water_binary = (water_mask_raw == 1)

labeled_water, n_water_bodies = ndimage.label(water_binary, structure=structure_4conn)

WATER_POINTS = []
for wid in range(1, n_water_bodies + 1):
    mask = (labeled_water == wid)
    n_pixels = int(mask.sum())
    rows_idx, cols_idx = np.where(mask)
    center_row, center_col = rows_idx.mean(), cols_idx.mean()
    x_map, y_map = transform_obj * (center_col, center_row)
    lon, lat = transformer_to_wgs84.transform(x_map, y_map)
    WATER_POINTS.append({
        "area_km2": round(n_pixels * 1.0, 2),
        "lat": round(lat, 6), "lon": round(lon, 6),
        "utm_x": x_map, "utm_y": y_map,
    })

print(f"✅ [router.py] Found {len(WATER_POINTS)} distinct water bodies")


def find_nearby_features(route_waypoints, corridor_km=5, max_each=5):
    """
    Given a route's waypoints, returns minor pasture patches and water
    points within `corridor_km` of the route line, nearest first,
    capped at `max_each` of each.
    """
    utm_coords = [transformer_to_utm.transform(wp["lon"], wp["lat"]) for wp in route_waypoints]
    route_line = LineString(utm_coords)
    corridor_m = corridor_km * 1000

    def nearby(features):
        results = []
        for f in features:
            pt = Point(f["utm_x"], f["utm_y"])
            dist_m = route_line.distance(pt)
            if dist_m <= corridor_m:
                clean = {k: v for k, v in f.items() if k not in ("utm_x", "utm_y")}
                clean["distance_from_route_km"] = round(dist_m / 1000, 2)
                results.append(clean)
        results.sort(key=lambda r: r["distance_from_route_km"])
        return results[:max_each]

    return {
        "minor_pasture_patches": nearby(MINOR_PATCHES),
        "water_points": nearby(WATER_POINTS),
    }


# ============================================================
# find_nearest_valid_node
# ============================================================

def find_nearest_valid_node(lat, lon, search_radius=5):
    row, col = latlon_to_rowcol(lat, lon)
    if 0 <= row < GRID_HEIGHT and 0 <= col < GRID_WIDTH and node_ids[row, col] >= 0:
        return node_ids[row, col], row, col

    for radius in range(1, search_radius + 1):
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                rr, cc = row + dr, col + dc
                if 0 <= rr < GRID_HEIGHT and 0 <= cc < GRID_WIDTH and node_ids[rr, cc] >= 0:
                    return node_ids[rr, cc], rr, cc
    return None, row, col



# ============================================================
# find_optimal_route — core routing function
# ============================================================

def find_optimal_route(start_lat, start_lon, n_cattle, month, livestock_type="cattle_adult_zebu"):
    month_name = normalize_month(month)
    requested_tlu = cattle_to_tlu(n_cattle, livestock_type)

    start_node, start_row, start_col = find_nearest_valid_node(start_lat, start_lon)
    if start_node is None:
        return {"error": "Could not snap start location to the routable grid — "
                          "point may be far outside the study area."}

    month_data = zone_month_capacity_df[zone_month_capacity_df["month"] == month_name]
    qualifying_zone_ids = month_data[month_data["capacity_tlu"] >= requested_tlu]["zone_id"].tolist()

    if len(qualifying_zone_ids) == 0:
        return {"error": f"No zone can sustain {n_cattle} {livestock_type} in {month_name}."}

    distances, paths = nx.single_source_dijkstra(G, source=start_node, weight="weight")

    reachable_candidates = [
        (zid, distances[zone_centroid_nodes[zid]])
        for zid in qualifying_zone_ids
        if zone_centroid_nodes.get(zid) in distances
    ]

    if len(reachable_candidates) == 0:
        return {"error": "No qualifying zone is reachable from this location "
                          "(may be cut off by hard barriers)."}

    best_zone_id, best_cost = min(reachable_candidates, key=lambda x: x[1])
    best_node = zone_centroid_nodes[best_zone_id]
    path_nodes = paths[best_node]

    waypoints = []
    total_physical_dist_m = 0.0
    for i, node in enumerate(path_nodes):
        r, c = node_to_rc[node]
        lat, lon = rowcol_to_latlon(r, c)
        waypoints.append({"lat": round(lat, 6), "lon": round(lon, 6)})
        if i > 0:
            total_physical_dist_m += G[path_nodes[i-1]][node]["dist_m"]

    total_distance_km = total_physical_dist_m / 1000
    estimated_days = total_distance_km / TRANSHUMANCE_SPEED_KM_PER_DAY

    zone_info = zone_df_attrs[zone_df_attrs["zone_id"] == best_zone_id].iloc[0].to_dict()
    zone_capacity_row = month_data[month_data["zone_id"] == best_zone_id].iloc[0]

    month_idx = month_names_full.index(month_name)
    zone_mask_for_info = (zone_raster == best_zone_id)

    mean_biomass = float(np.nanmean(biomass_monthly_mean[month_idx][zone_mask_for_info]))
    mean_water_dist_m = float(np.nanmean(dist_water_raster[zone_mask_for_info]))

    return {
        "zone_id": int(best_zone_id),
        "zone_info": {
            "quality": zone_info["quality"], "area_km2": zone_info["area_km2"],
            "mean_score": zone_info["mean_score"], "best_month": zone_info["best_month"],
            "worst_month": zone_info["worst_month"],
            "mean_biomass_kg_dm_ha": round(mean_biomass, 1) if not np.isnan(mean_biomass) else None,
            "mean_water_distance_km": round(mean_water_dist_m / 1000, 2) if not np.isnan(mean_water_dist_m) else None,
        },
        "capacity_info": {
            "month": month_name, "capacity_tlu": float(zone_capacity_row["capacity_tlu"]),
            "requested_tlu": round(requested_tlu, 2),
            "surplus_tlu": round(float(zone_capacity_row["capacity_tlu"]) - requested_tlu, 2),
        },
        "route": {
            "waypoints": waypoints, "n_waypoints": len(waypoints),
            "total_distance_km": round(total_distance_km, 1),
            "estimated_travel_days": round(estimated_days, 1),
            "network_cost": round(best_cost, 2),
        },
    }

import itertools
import time

def find_best_zone_with_route_alternatives(start_lat, start_lon, n_cattle, month,
                                              livestock_type="cattle_adult_zebu", n_alternatives=3):
    """
    Finds the single best (nearest qualifying, quality as tiebreaker) grazing
    zone, then returns the weighted-cost-optimal route to it PLUS n_alternatives
    additional distinct routes. The "best" route is the LOWEST-COST path (which
    already avoids farmland/settlements/steep slopes and favors water/roads,
    per the cost surface) — NOT necessarily the physically shortest path.
    """
    month_name = normalize_month(month)
    requested_tlu = cattle_to_tlu(n_cattle, livestock_type)

    start_node, start_row, start_col = find_nearest_valid_node(start_lat, start_lon)
    if start_node is None:
        return {"error": "Could not snap start location to the routable grid — "
                          "point may be far outside the study area."}

    month_data = zone_month_capacity_df[zone_month_capacity_df["month"] == month_name]
    qualifying_zone_ids = month_data[month_data["capacity_tlu"] >= requested_tlu]["zone_id"].tolist()

    if len(qualifying_zone_ids) == 0:
        return {"error": f"No zone can sustain {n_cattle} {livestock_type} in {month_name}."}

    # --- Step 1: find distances to ALL qualifying zones (cheap, single run) ---
    distances, _ = nx.single_source_dijkstra(G, source=start_node, weight="weight")

    candidates = []
    for zid in qualifying_zone_ids:
        centroid_node = zone_centroid_nodes.get(zid)
        if centroid_node not in distances:
            continue
        capacity_row = month_data[month_data["zone_id"] == zid].iloc[0]
        zone_quality = zone_df_attrs[zone_df_attrs["zone_id"] == zid].iloc[0]["quality"]
        candidates.append({
            "zone_id": zid,
            "network_cost": distances[centroid_node],
            "capacity_tlu": float(capacity_row["capacity_tlu"]),
            "surplus_tlu": float(capacity_row["capacity_tlu"]) - requested_tlu,
            "quality_rank": {"High": 3, "Moderate": 2, "Low": 1, "Unsuitable": 0}.get(zone_quality, 0),
        })

    if len(candidates) == 0:
        return {"error": "No qualifying zone is reachable from this location "
                          "(may be cut off by hard barriers)."}

    # --- Step 2: pick THE single best zone (distance primary, quality tiebreaks) ---
    DISTANCE_TIER_SIZE = 1000
    for c in candidates:
        c["distance_tier"] = round(c["network_cost"] / DISTANCE_TIER_SIZE)

    candidates.sort(key=lambda c: (c["distance_tier"], -c["quality_rank"], c["network_cost"]))
    best_zone = candidates[0]
    zid = best_zone["zone_id"]
    centroid_node = zone_centroid_nodes[zid]

    # --- Step 3: find n_alternatives+1 distinct paths to that ONE zone ---
    # Yen's algorithm (shortest_simple_paths) returns paths in increasing
    # total-weight order — the first is the lowest-cost (= "best") path.
    print(f"🔍 Computing {n_alternatives + 1} route alternatives to zone {zid}...")
    t0 = time.time()

    try:
        path_generator = nx.shortest_simple_paths(G, source=start_node, target=centroid_node, weight="weight")
        found_paths = list(itertools.islice(path_generator, n_alternatives + 1))
    except nx.NetworkXNoPath:
        return {"error": f"Zone {zid} is not reachable from this location (blocked by hard barriers)."}

    elapsed = time.time() - t0
    print(f"✅ Found {len(found_paths)} route(s) in {elapsed:.2f}s")

    if len(found_paths) < n_alternatives + 1:
        print(f"⚠️ Only {len(found_paths)} distinct route(s) exist to this zone "
              f"(requested {n_alternatives + 1}).")

    # --- Step 4: build full details for each path ---
    month_idx = month_names.index(month_name)
    zone_mask_for_info = (zone_raster == zid)
    mean_biomass = float(np.nanmean(biomass_monthly_mean[month_idx][zone_mask_for_info]))
    mean_water_dist_m = float(np.nanmean(dist_water_raster[zone_mask_for_info]))
    zone_info = zone_df_attrs[zone_df_attrs["zone_id"] == zid].iloc[0].to_dict()

    results = []
    for rank, path_nodes in enumerate(found_paths, start=1):
        waypoints = []
        total_physical_dist_m = 0.0
        total_weighted_cost = 0.0
        for i, node in enumerate(path_nodes):
            r, c = node_to_rc[node]
            lat, lon = rowcol_to_latlon(r, c)
            waypoints.append({"lat": round(lat, 6), "lon": round(lon, 6)})
            if i > 0:
                edge_data = G[path_nodes[i - 1]][node]
                total_physical_dist_m += edge_data["dist_m"]
                total_weighted_cost += edge_data["weight"]

        total_distance_km = total_physical_dist_m / 1000
        estimated_days = total_distance_km / TRANSHUMANCE_SPEED_KM_PER_DAY

        results.append({
            "rank": rank,
            "is_best": rank == 1,  # lowest weighted cost = avoids farmland/slopes, favors water/roads
            "waypoints": waypoints,
            "n_waypoints": len(waypoints),
            "total_distance_km": round(total_distance_km, 1),
            "estimated_travel_days": round(estimated_days, 1),
            "total_weighted_cost": round(total_weighted_cost, 2),
        })
    nearby_features = find_nearby_features(results[0]["waypoints"], corridor_km=5, max_each=5)
    return {
        "zone_id": int(zid),
        "month": month_name,
        "zone_info": {
            "quality": zone_info["quality"], "area_km2": zone_info["area_km2"],
            "mean_score": zone_info["mean_score"], "best_month": zone_info["best_month"],
            "worst_month": zone_info["worst_month"],
            "mean_biomass_kg_dm_ha": round(mean_biomass, 1) if not np.isnan(mean_biomass) else None,
            "mean_water_distance_km": round(mean_water_dist_m / 1000, 2) if not np.isnan(mean_water_dist_m) else None,
        },
        "capacity_info": {
            "month": month_name, "capacity_tlu": round(best_zone["capacity_tlu"], 2),
            "requested_tlu": round(requested_tlu, 2),
            "surplus_tlu": round(best_zone["surplus_tlu"], 2),
        },
        "routes": results,
        "n_routes_returned": len(results),
        "minor_pasture_patches": nearby_features["minor_pasture_patches"],
        "water_points": nearby_features["water_points"],
    }