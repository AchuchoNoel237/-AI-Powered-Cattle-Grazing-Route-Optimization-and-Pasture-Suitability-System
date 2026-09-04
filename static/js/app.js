// ============================================================
// Adamawa Transhumance Route Finder — frontend logic (FULL FILE)
// GPS detection, form handling, API calls, 6-route rendering,
// offline caching. This replaces the entire app.js.
// ============================================================

// --- Register service worker for offline app-shell caching ---
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/static/js/sw.js")
            .then((reg) => console.log("Service worker registered:", reg.scope))
            .catch((err) => console.log("Service worker registration failed:", err));
    });
}

// --- Map setup, centered roughly on Adamawa region ---
const map = L.map('map').setView([7.3, 13.0], 7);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
}).addTo(map);

let herderMarker = null;
let currentLat = null;
let currentLon = null;
let routeLayers = [];  // tracks all drawn routes/markers so we can clear them between searches

// --- Color palette: gold for the best route, distinct hues for the rest ---
const ROUTE_COLORS = {
    best: "#ffd700",
    others: ["#4fc3f7", "#ff8a65", "#ba68c8", "#81c784", "#f06292"],
};

// --- Auto-detect current month from the device, pre-fill (still editable) ---
const monthSelect = document.getElementById('month-select');
const now = new Date();
monthSelect.value = String(now.getMonth() + 1);

// --- GPS detection ---
const gpsBtn = document.getElementById('gps-btn');
const gpsStatus = document.getElementById('gps-status');

gpsBtn.addEventListener('click', () => {
    if (!navigator.geolocation) {
        gpsStatus.textContent = 'Geolocation is not supported by your browser. Tap the map instead.';
        return;
    }
    gpsStatus.textContent = 'Detecting location...';
    navigator.geolocation.getCurrentPosition(
        (position) => {
            setHerderLocation(position.coords.latitude, position.coords.longitude);
            gpsStatus.textContent = `📍 Location set (${position.coords.latitude.toFixed(4)}, ${position.coords.longitude.toFixed(4)})`;
            gpsStatus.classList.add('active');
        },
        (err) => {
            gpsStatus.textContent = `Could not get location (${err.message}). Tap the map instead.`;
        },
        { enableHighAccuracy: true, timeout: 10000 }
    );
});

// --- Manual location: tap the map ---
map.on('click', (e) => {
    setHerderLocation(e.latlng.lat, e.latlng.lng);
    gpsStatus.textContent = `📍 Location set manually (${e.latlng.lat.toFixed(4)}, ${e.latlng.lng.toFixed(4)})`;
    gpsStatus.classList.add('active');
});

function setHerderLocation(lat, lon) {
    currentLat = lat;
    currentLon = lon;

    if (herderMarker) {
        map.removeLayer(herderMarker);
    }
    const herderIcon = L.divIcon({
        className: '',
        html: '<div style="background:#7ed957; width:16px; height:16px; border-radius:50%; border:3px solid #0d2818;"></div>',
        iconSize: [16, 16],
    });
    herderMarker = L.marker([lat, lon], { icon: herderIcon }).addTo(map);
    map.setView([lat, lon], 9);
}

// --- Find route button ---
const findRouteBtn = document.getElementById('find-route-btn');
const errorBanner = document.getElementById('error-banner');
const resultPanel = document.getElementById('result-panel');


findRouteBtn.addEventListener('click', async () => {
    hideError();

    if (currentLat === null || currentLon === null) {
        showError('Please set your location first — tap "Detect My Location" or tap the map.');
        return;
    }

    const cattleSize = parseFloat(document.getElementById('cattle-size').value);
    if (!cattleSize || cattleSize <= 0) {
        showError('Please enter a valid herd size.');
        return;
    }

    const livestockType = document.getElementById('livestock-type').value;
    const month = parseInt(monthSelect.value, 10);

    findRouteBtn.disabled = true;
    findRouteBtn.textContent = 'Finding routes...';

    let data;

    // --- Stage 1: network call only ---
    try {
        const response = await fetch('/api/find-route/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                lat: currentLat, lon: currentLon,
                cattle_size: cattleSize, livestock_type: livestockType, month: month,
            }),
        });
        data = await response.json();

        if (!response.ok || data.error) {
            showError(data.error || 'Something went wrong finding routes.');
            findRouteBtn.disabled = false;
            findRouteBtn.textContent = 'Find Best Grazing Route';
            return;
        }
    } catch (err) {
        console.error('Network/fetch error:', err);
        showError('Could not reach the server. Check your connection and try again.');
        findRouteBtn.disabled = false;
        findRouteBtn.textContent = 'Find Best Grazing Route';
        return;
    }

    // --- Stage 2: rendering, separate from network concerns ---
    try {
        renderAllRoutes(data);
        renderResultPanel(data);
        try { localStorage.setItem('last_route', JSON.stringify(data)); } catch (e) {}
    } catch (err) {
        console.error('Rendering error:', err);
        showError('Routes were received but could not be displayed. Check console for details.');
    } finally {
        findRouteBtn.disabled = false;
        findRouteBtn.textContent = 'Find Best Grazing Route';
    }
});



function renderAllRoutes(data) {
    routeLayers.forEach(layer => map.removeLayer(layer));
    routeLayers = [];

    const allBounds = [];

    // Draw non-best routes first, so the best route line renders on top
    const sorted = [...data.routes].sort((a, b) => a.is_best - b.is_best);

    sorted.forEach((route) => {
        const latlngs = route.waypoints.map(wp => [wp.lat, wp.lon]);
        const color = route.is_best
            ? ROUTE_COLORS.best
            : ROUTE_COLORS.others[(route.rank - 2) % ROUTE_COLORS.others.length];

        const line = L.polyline(latlngs, {
            color: color,
            weight: route.is_best ? 6 : 3,
            opacity: route.is_best ? 1.0 : 0.6,
        }).addTo(map);
        routeLayers.push(line);
        allBounds.push(...latlngs);
    });

    // Single destination marker (all routes lead to the same zone)
    const destination = data.routes[0].waypoints[data.routes[0].waypoints.length - 1];
    const marker = L.marker([destination.lat, destination.lon]).addTo(map);
    marker.bindPopup(`
        <h4>⭐ Zone ${data.zone_id} (${data.zone_info.quality})</h4>
        <b>Area:</b> ${data.zone_info.area_km2} km²<br/>
        <b>Biomass:</b> ${data.zone_info.mean_biomass_kg_dm_ha ?? 'N/A'} kg DM/ha<br/>
        <b>Water distance:</b> ${data.zone_info.mean_water_distance_km ?? 'N/A'} km<br/>
        <b>Capacity:</b> ${data.capacity_info.capacity_tlu} TLU<br/>
        <b>Surplus:</b> ${data.capacity_info.surplus_tlu} TLU<br/>
        <hr/>
        <b>Best route:</b> ${data.routes[0].total_distance_km} km, ~${data.routes[0].estimated_travel_days} days<br/>
        <small>Best = avoids farmland/settlements/steep slopes, favors water access — not necessarily shortest.</small>
    `).openPopup();
    routeLayers.push(marker);
    
        // --- Minor pasture patches (small light-green dots) ---
        data.minor_pasture_patches.forEach((patch) => {
            const connector = L.polyline(
                [[patch.connector_lat, patch.connector_lon], [patch.lat, patch.lon]],
                { color: "#a5d6a7", weight: 2, opacity: 0.6, dashArray: "4, 6" }
            ).addTo(map);
            routeLayers.push(connector);

            const dot = L.circleMarker([patch.lat, patch.lon], {
                radius: 5, color: "#a5d6a7", fillColor: "#a5d6a7", fillOpacity: 0.8, weight: 1,
            }).addTo(map);
            dot.bindPopup(`
                <b>Minor pasture patch</b><br/>
                Area: ${patch.area_km2} km²<br/>
                Score: ${patch.mean_score}<br/>
                ${patch.distance_from_route_km} km from route
            `);
            routeLayers.push(dot);
        });

    // --- Water points (small blue dots) ---
    data.water_points.forEach((wp) => {
        const connector = L.polyline(
            [[wp.connector_lat, wp.connector_lon], [wp.lat, wp.lon]],
            { color: "#4fc3f7", weight: 2, opacity: 0.6, dashArray: "4, 6" }
        ).addTo(map);
        routeLayers.push(connector);

        const dot = L.circleMarker([wp.lat, wp.lon], {
            radius: 4, color: "#4fc3f7", fillColor: "#4fc3f7", fillOpacity: 0.9, weight: 1,
        }).addTo(map);
        dot.bindPopup(`
            <b>💧 Water point</b><br/>
            Area: ${wp.area_km2} km²<br/>
            ${wp.distance_from_route_km} km from route
        `);
        routeLayers.push(dot);
    });

    if (allBounds.length > 0) {
        map.fitBounds(L.latLngBounds(allBounds), { padding: [40, 40] });
    }
}

function renderResultPanel(data) {
    const bestRoute = data;
    document.getElementById('result-zone-id').textContent = `#${data.zone_id}`;
    document.getElementById('result-quality').textContent = data.zone_info.quality;
    document.getElementById('result-area').textContent = `${data.zone_info.area_km2} km²`;
    document.getElementById('result-capacity').textContent = `${data.capacity_info.capacity_tlu} TLU`;
    document.getElementById('result-requested').textContent = `${data.capacity_info.requested_tlu} TLU`;
    document.getElementById('result-surplus').textContent = `${data.capacity_info.surplus_tlu} TLU`;
    document.getElementById('result-best-month').textContent = data.zone_info.best_month;
    document.getElementById('result-worst-month').textContent = data.zone_info.worst_month;
    document.getElementById('result-distance').textContent = `${bestRoute.total_distance_km} km`;
    document.getElementById('result-days').textContent = `${bestRoute.estimated_travel_days} days (${data.n_routes_returned} routes shown)`;
    resultPanel.classList.add('visible');
}

function showError(message) {
    errorBanner.textContent = message;
    errorBanner.classList.add('visible');
}

function hideError() {
    errorBanner.classList.remove('visible');
}

// --- On load: if offline, show the last cached route automatically ---
window.addEventListener("load", () => {
    if (!navigator.onLine) {
        const cached = localStorage.getItem("last_route");
        if (cached) {
            try {
                const data = JSON.parse(cached);
                renderAllRoutes(data);
                renderResultPanel(data);
                showError("You're offline — showing your last saved routes.");
            } catch (e) {
                // corrupted cache entry, ignore
            }
        }
    }
});

window.addEventListener("offline", () => {
    showError("You've lost connection. Your last routes (if any) remain available below.");
});

window.addEventListener("online", () => {
    hideError();
});