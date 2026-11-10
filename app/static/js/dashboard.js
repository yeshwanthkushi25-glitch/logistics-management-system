/**
 * Logistics Information Management System (Logistics IMS)
 * Enterprise Dashboard Client-Side Controller
 * Vanilla ES6+ &bull; Zero External Dependencies
 */

(function () {
    "use strict";

    // Global application namespace
    window.IMS = window.IMS || {};

    // Current session state
    const STATE = {
        activeRole: sessionStorage.getItem("ims_role") || "Admin",
        token: sessionStorage.getItem("ims_token") || null,
        activeTracking: "TRK-CHI-NYC-8801",
        apiBase: "/api/v1"
    };

    /**
     * Unified API Fetch wrapper with simulated Bearer token & JSON serialization.
     */
    async function apiFetch(endpoint, options = {}) {
        const url = endpoint.startsWith("http") ? endpoint : `${STATE.apiBase}${endpoint}`;
        const headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            ...(STATE.token ? { "Authorization": `Bearer ${STATE.token}` } : {}),
            ...(options.headers || {})
        };

        try {
            const response = await fetch(url, { ...options, headers });
            const data = await response.json();
            return { ok: response.ok, status: response.status, data };
        } catch (err) {
            console.warn(`[IMS API] Fetch failed for ${url}:`, err);
            return { ok: false, status: 0, error: err.message };
        }
    }

    /**
     * Role Switcher & Authorization Context Setup
     */
    function initRoleSwitcher() {
        const selector = document.getElementById("roleSelector");
        if (!selector) return;

        selector.value = STATE.activeRole;
        selector.addEventListener("change", (e) => {
            STATE.activeRole = e.target.value;
            sessionStorage.setItem("ims_role", STATE.activeRole);
            showNotification(`Active role switched to: ${STATE.activeRole}`, "info");
            
            // Sync with backend /me if logged in
            const actorInput = document.getElementById("restockActor");
            if (actorInput) {
                actorInput.value = STATE.activeRole === "Admin" ? "user_admin" : "user_wm_chi";
            }
        });
    }

    /**
     * Floating flash notification helper
     */
    function showNotification(message, type = "success") {
        let container = document.getElementById("flashContainer");
        if (!container) {
            container = document.createElement("div");
            container.id = "flashContainer";
            container.className = "container flash-container";
            document.body.prepend(container);
        }

        const alertDiv = document.createElement("div");
        alertDiv.className = `alert alert-${type}`;
        alertDiv.innerHTML = `
            <span class="alert-icon">${type === "success" ? "✓" : "ℹ"}</span>
            <span class="alert-message">${message}</span>
            <button type="button" class="alert-close" onclick="this.parentElement.remove()">×</button>
        `;
        container.appendChild(alertDiv);

        setTimeout(() => {
            if (alertDiv.parentElement) alertDiv.remove();
        }, 5000);
    }

    /* =========================================================================
       1. Dashboard Executive KPIs (index.html)
       ========================================================================= */
    async function refreshDashboardKPIs() {
        const btn = document.getElementById("refreshKpisBtn");
        if (btn) btn.classList.add("loading");

        // Fetch Analytics KPIs
        const kpisRes = await apiFetch("/analytics/kpis");
        if (kpisRes.ok && kpisRes.data) {
            const d = kpisRes.data;
            if (d.total_inventory_units !== undefined) {
                const el = document.getElementById("kpiTotalInventory");
                if (el) el.innerText = Number(d.total_inventory_units).toLocaleString();
            }
            if (d.active_orders !== undefined) {
                const el = document.getElementById("kpiActiveOrders");
                if (el) el.innerText = d.active_orders;
            }
            if (d.shipments_in_transit !== undefined) {
                const el = document.getElementById("kpiInTransitShipments");
                if (el) el.innerText = d.shipments_in_transit;
            }
            if (d.low_stock_alerts !== undefined) {
                const el = document.getElementById("kpiLowStockAlerts");
                if (el) el.innerText = `${d.low_stock_alerts} Low Stock Alerts`;
            }
        }

        // Fetch Cache Stats
        const cacheRes = await apiFetch("/analytics/cache/stats");
        if (cacheRes.ok && cacheRes.data) {
            const c = cacheRes.data;
            const ratioEl = document.getElementById("kpiCacheHitRatio");
            if (ratioEl) ratioEl.innerText = `${(c.hit_ratio * 100).toFixed(1)}%`;
            const hitsEl = document.getElementById("kpiCacheHits");
            if (hitsEl) hitsEl.innerText = `${c.hits} Hits`;
            const missEl = document.getElementById("kpiCacheMisses");
            if (missEl) missEl.innerText = `${c.misses} Misses`;
        }

        if (btn) btn.classList.remove("loading");
        showNotification("Dashboard telemetry and cache metrics synchronized.", "success");
    }

    /* =========================================================================
       2. Shipment Tracking & 7-State FSM (tracking.html)
       ========================================================================= */
    function setTrackingNumber(trk) {
        const input = document.getElementById("trackingInput");
        if (input) {
            input.value = trk;
            lookupShipment(trk);
        }
    }

    function searchTracking() {
        const input = document.getElementById("trackingInput");
        if (input && input.value.trim()) {
            lookupShipment(input.value.trim());
        }
    }

    async function lookupShipment(trackingNumber) {
        STATE.activeTracking = trackingNumber;
        const res = await apiFetch(`/shipments/${encodeURIComponent(trackingNumber)}`);

        if (!res.ok || !res.data) {
            showNotification(`Consignment '${trackingNumber}' not found. Check tracking number.`, "danger");
            return;
        }

        const data = res.data;
        renderShipmentData(data);
    }

    function renderShipmentData(data) {
        // Meta Card
        const trkEl = document.getElementById("displayTrackingNumber");
        if (trkEl) trkEl.innerText = data.tracking_number || STATE.activeTracking;

        const statusEl = document.getElementById("displayStatus");
        if (statusEl) {
            statusEl.innerText = data.status || "UNKNOWN";
            statusEl.className = `badge-status status-${(data.status || "").toLowerCase()}`;
        }

        const cacheEl = document.getElementById("displayCacheStatus");
        if (cacheEl) {
            if (data.cache_status === "HIT") {
                cacheEl.innerHTML = "⚡ Cache HIT (0.2ms)";
                cacheEl.className = "cache-badge bg-success";
            } else {
                cacheEl.innerHTML = "🔄 Cache MISS (DB Read)";
                cacheEl.className = "cache-badge bg-info";
            }
        }

        if (data.origin_warehouse_name || data.origin_warehouse_id) {
            const el = document.getElementById("displayOrigin");
            if (el) el.innerText = data.origin_warehouse_name || data.origin_warehouse_id;
        }
        if (data.carrier) {
            const el = document.getElementById("displayCarrier");
            if (el) el.innerText = data.carrier;
        }
        if (data.service_level) {
            const el = document.getElementById("displayServiceLevel");
            if (el) el.innerText = data.service_level;
        }
        if (data.assigned_driver_name || data.assigned_driver_id) {
            const el = document.getElementById("displayDriver");
            if (el) el.innerText = data.assigned_driver_name || data.assigned_driver_id;
        }
        if (data.order_id) {
            const el = document.getElementById("displayOrderId");
            if (el) el.innerText = data.order_id;
        }

        // Update 7-state FSM Stepper
        updateFsmStepper(data.status, data.milestones);

        // Update Milestones timeline
        if (data.milestones && Array.isArray(data.milestones)) {
            renderMilestones(data.milestones);
        }

        // Update Telemetry coordinates
        if (data.latest_telemetry) {
            renderTelemetry(data.latest_telemetry);
        } else if (data.current_lat && data.current_lon) {
            renderTelemetry({
                geo: { lat: data.current_lat, lon: data.current_lon },
                environment: { temperature_celsius: 4.0, shock_g: 0.1 }
            });
        }
    }

    function updateFsmStepper(status, milestones) {
        const primaryStates = ["CREATED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"];
        const statusClean = (status || "").toUpperCase();
        const currentIndex = primaryStates.indexOf(statusClean);

        const stepper = document.getElementById("fsmStepper");
        if (!stepper) return;

        const nodes = stepper.querySelectorAll(".step-node");
        const lines = stepper.querySelectorAll(".step-line");

        nodes.forEach((node, idx) => {
            const stateName = node.getAttribute("data-state");
            node.classList.remove("step-completed", "step-active", "step-pending");

            if (currentIndex >= 0) {
                if (idx < currentIndex) {
                    node.classList.add("step-completed");
                    const icon = node.querySelector(".step-icon");
                    if (icon) icon.innerText = "✓";
                } else if (idx === currentIndex) {
                    node.classList.add("step-active");
                    const icon = node.querySelector(".step-icon");
                    if (icon) icon.innerText = stateName === "DELIVERED" ? "★" : "●";
                } else {
                    node.classList.add("step-pending");
                }
            }
        });

        lines.forEach((line, idx) => {
            line.classList.remove("line-completed");
            if (currentIndex > idx) {
                line.classList.add("line-completed");
            }
        });

        // Exception Banner toggle
        const banner = document.getElementById("exceptionBanner");
        if (banner) {
            if (statusClean === "EXCEPTION" || statusClean === "RETURNED") {
                banner.classList.remove("d-none");
            } else {
                banner.classList.add("d-none");
            }
        }
    }

    function renderMilestones(milestones) {
        const list = document.getElementById("milestonesList");
        if (!list) return;

        if (milestones.length === 0) {
            list.innerHTML = "<p class='text-muted'>No milestone events recorded yet.</p>";
            return;
        }

        list.innerHTML = milestones.map((m, idx) => {
            const isDelivered = m.milestone_type === "DELIVERY_COMPLETED";
            return `
                <div class="milestone-entry ${isDelivered ? 'milestone-success' : ''}">
                    <div class="milestone-badge ${isDelivered ? 'bg-success' : 'bg-primary'}">${idx + 1}</div>
                    <div class="milestone-info">
                        <div class="flex-between">
                            <strong>${m.milestone_type}</strong>
                            <span class="text-muted">${m.timestamp || ''}</span>
                        </div>
                        <p class="milestone-loc">📍 ${m.location_name || 'Checkpoint'} (${m.latitude || '-'}, ${m.longitude || '-'})</p>
                        <p class="milestone-notes">${m.notes || ''}</p>
                        <small class="text-secondary">Recorded by: ${m.recorded_by || 'system'}</small>
                    </div>
                </div>
            `;
        }).join("");
    }

    function renderTelemetry(telem) {
        const lat = telem.geo ? telem.geo.lat : telem.latitude;
        const lon = telem.geo ? telem.geo.lon : telem.longitude;
        const temp = telem.environment ? telem.environment.temperature_celsius : (telem.temperature || 4.2);
        const shock = telem.environment ? telem.environment.shock_g : (telem.shock || 0.08);

        const latEl = document.getElementById("telemLat");
        if (latEl && lat) latEl.innerText = `${Number(lat).toFixed(4)}° N`;
        const lonEl = document.getElementById("telemLon");
        if (lonEl && lon) lonEl.innerText = `${Number(lon).toFixed(4)}° W`;
        const tempEl = document.getElementById("telemTemp");
        if (tempEl && temp !== undefined) tempEl.innerText = `${Number(temp).toFixed(1)}°C`;
        const shockEl = document.getElementById("telemShock");
        if (shockEl && shock !== undefined) shockEl.innerText = `${Number(shock).toFixed(2)} G`;
    }

    // Driver Simulation Actions
    async function simulateStatusAdvance() {
        const trk = STATE.activeTracking;
        const res = await apiFetch(`/shipments/${encodeURIComponent(trk)}/status`, {
            method: "POST",
            body: JSON.stringify({
                status: "IN_TRANSIT",
                actor_id: "user_driver",
                notes: "Advanced via Driver Simulation control",
                location: { name: "Transit En-Route", latitude: 41.9, longitude: -85.5 }
            })
        });

        if (res.ok) {
            showNotification(`Shipment status advanced to IN_TRANSIT.`, "success");
            lookupShipment(trk);
        } else {
            showNotification(`FSM Transition error: ${res.data?.error || 'Illegal state transition'}`, "danger");
        }
    }

    async function simulateTelemetryPing() {
        const trk = STATE.activeTracking;
        const res = await apiFetch(`/shipments/${encodeURIComponent(trk)}/telemetry`, {
            method: "POST",
            body: JSON.stringify({
                tracking_number: trk,
                timestamp: new Date().toISOString(),
                geo: { lat: 41.8781, lon: -87.6298, speed_kmh: 88.5 },
                environment: { temperature_celsius: 3.9, humidity_percent: 45, shock_g: 0.06 },
                diagnostics: { battery_level: 98, signal_rssi: -65 }
            })
        });

        if (res.ok) {
            showNotification(`NoSQL Telemetry ping ingested into DocumentStore.`, "success");
            lookupShipment(trk);
        } else {
            showNotification(`Telemetry error: ${res.data?.error || 'Ingestion failed'}`, "danger");
        }
    }

    async function simulatePodUpload() {
        const trk = STATE.activeTracking;
        const res = await apiFetch(`/shipments/${encodeURIComponent(trk)}/pod`, {
            method: "POST",
            body: JSON.stringify({
                tracking_number: trk,
                signed_by: "Alice Smith (Customer)",
                pod_data_base64: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                notes: "Captured digital proof of delivery"
            })
        });

        if (res.ok) {
            showNotification(`Proof of delivery recorded and uploaded.`, "success");
            lookupShipment(trk);
        } else {
            showNotification(`POD error: ${res.data?.error || 'Upload failed'}`, "danger");
        }
    }

    /* =========================================================================
       3. Inventory Management & Restock (inventory.html)
       ========================================================================= */
    function filterInventoryTable() {
        const whVal = (document.getElementById("warehouseFilter")?.value || "all").toLowerCase();
        const catVal = (document.getElementById("categoryFilter")?.value || "all").toLowerCase();
        const searchVal = (document.getElementById("inventorySearch")?.value || "").toLowerCase().trim();
        const lowStockOnly = document.getElementById("lowStockToggle")?.checked || false;

        const rows = document.querySelectorAll("#inventoryTableBody tr");
        rows.forEach(row => {
            const rowWh = (row.getAttribute("data-warehouse") || "").toLowerCase();
            const rowCat = (row.getAttribute("data-category") || "").toLowerCase();
            const rowLow = row.getAttribute("data-lowstock") === "true";
            const rowText = row.innerText.toLowerCase();

            const matchesWh = whVal === "all" || rowWh === whVal;
            const matchesCat = catVal === "all" || rowCat === catVal;
            const matchesSearch = !searchVal || rowText.includes(searchVal);
            const matchesLow = !lowStockOnly || rowLow;

            if (matchesWh && matchesCat && matchesSearch && matchesLow) {
                row.style.display = "";
            } else {
                row.style.display = "none";
            }
        });
    }

    function openRestockModal() {
        const modal = document.getElementById("restockModalBackdrop");
        if (modal) modal.classList.remove("d-none");
    }

    function closeRestockModal() {
        const modal = document.getElementById("restockModalBackdrop");
        if (modal) modal.classList.add("d-none");
    }

    function quickRestock(whId, prodId, sku) {
        openRestockModal();
        const whSelect = document.getElementById("restockWarehouse");
        if (whSelect) whSelect.value = whId;
        const prodSelect = document.getElementById("restockProduct");
        if (prodSelect) prodSelect.value = prodId;
    }

    async function submitRestock() {
        const whId = document.getElementById("restockWarehouse")?.value;
        const prodId = document.getElementById("restockProduct")?.value;
        const qty = parseInt(document.getElementById("restockQuantity")?.value, 10);
        const batch = document.getElementById("restockBatch")?.value || "BATCH-2026-RESTOCK";
        const bin = document.getElementById("restockBin")?.value || null;
        const actor = document.getElementById("restockActor")?.value || "user_wm_chi";

        if (!qty || qty <= 0) {
            showNotification("Restock quantity must be a positive integer.", "danger");
            return;
        }

        const res = await apiFetch("/inventory/restock", {
            method: "POST",
            body: JSON.stringify({
                warehouse_id: whId,
                product_id: prodId,
                quantity: qty,
                batch_number: batch,
                bin_code: bin,
                actor_id: actor
            })
        });

        closeRestockModal();

        if (res.ok) {
            showNotification(`Restock successful: +${qty} units allocated to ${whId}. Cache invalidated.`, "success");
            // Reload inventory if possible or visually bump
            const badge = document.getElementById("inventoryCacheBadge");
            if (badge) badge.innerText = "🔄 Cache Invalidated & Refreshed";
        } else {
            showNotification(`Restock failed: ${res.data?.error || 'Database error'}`, "danger");
        }
    }

    /* =========================================================================
       4. Network Routing & Outage Visualizer (network.html)
       ========================================================================= */
    function toggleOutageInputs() {
        const toggle = document.getElementById("outageSimulationToggle");
        const container = document.getElementById("outageEdgeSelectorGroup");
        if (!toggle || !container) return;

        if (toggle.checked) {
            container.classList.remove("d-none");
        } else {
            container.classList.add("d-none");
        }
    }

    async function computeRoute() {
        const origin = document.getElementById("routeOrigin")?.value;
        const dest = document.getElementById("routeDest")?.value;
        const criteria = document.getElementById("routeCriteria")?.value || "distance";
        const algorithm = document.getElementById("routeAlgorithm")?.value || "dijkstra";
        const isOutage = document.getElementById("outageSimulationToggle")?.checked || false;
        const failedEdge = document.getElementById("outageFailedEdge")?.value;

        if (origin === dest) {
            showNotification("Origin and destination hubs must be distinct.", "warning");
            return;
        }

        let res;
        if (isOutage) {
            res = await apiFetch("/network/reroute", {
                method: "POST",
                body: JSON.stringify({
                    origin,
                    destination: dest,
                    failed_edge_id: failedEdge,
                    criteria
                })
            });
        } else {
            res = await apiFetch("/network/routes", {
                method: "POST",
                body: JSON.stringify({
                    origin,
                    destination: dest,
                    criteria,
                    use_astar: (algorithm === "astar")
                })
            });
        }

        if (!res.ok || !res.data) {
            showNotification(`Routing failure: ${res.data?.error || 'No navigable path'}`, "danger");
            return;
        }

        renderRouteResults(res.data, isOutage);
    }

    function renderRouteResults(data, isOutage) {
        const route = isOutage ? data.fallback_route : data;
        const delta = isOutage ? data.delta : null;

        // Metric summary
        const distEl = document.getElementById("metricDistance");
        if (distEl) distEl.innerText = `${route.total_distance_km} km`;
        const costEl = document.getElementById("metricCost");
        if (costEl) costEl.innerText = `$${Number(route.total_cost_usd || route.total_cost || 0).toFixed(2)}`;
        const timeEl = document.getElementById("metricTime");
        if (timeEl) timeEl.innerText = `${route.estimated_transit_hours} hrs`;
        const hopEl = document.getElementById("metricHops");
        if (hopEl) hopEl.innerText = `${route.hop_count || (route.corridors ? route.corridors.length : 1)} Hops`;

        const badge = document.getElementById("routeAlgorithmBadge");
        if (badge) {
            badge.innerText = isOutage ? "Outage Resilient Reroute" : (route.algorithm || "Dijkstra Optimal");
        }

        // Outage Delta Card
        const deltaCard = document.getElementById("outageDeltaCard");
        if (deltaCard) {
            if (isOutage && delta) {
                deltaCard.classList.remove("d-none");
                const dDist = document.getElementById("deltaDist");
                if (dDist) dDist.innerText = `+${delta.distance_km || delta.delta_km || 10.0} km`;
                const dCost = document.getElementById("deltaCost");
                if (dCost) dCost.innerText = `+$${delta.cost_usd || delta.delta_cost || 110.00}`;
                const dTime = document.getElementById("deltaTime");
                if (dTime) dTime.innerText = `+${delta.transit_hours || delta.delta_time || 0.5} hrs`;
            } else {
                deltaCard.classList.add("d-none");
            }
        }

        // Render Hops sequence
        const hopsList = document.getElementById("routeHopsList");
        if (hopsList && route.corridors) {
            hopsList.innerHTML = route.corridors.map((c, i) => `
                <div class="hop-item">
                    <div class="hop-number">${i + 1}</div>
                    <div class="hop-details">
                        <div class="flex-between">
                            <strong>${c.source} &rarr; ${c.target}</strong>
                            <span class="badge-status status-healthy">${c.transport_mode || 'ROAD_FREIGHT'}</span>
                        </div>
                        <p class="hop-meta">Distance: ${c.distance_km} km &bull; Time: ${c.transit_time_hours} hrs &bull; Cost: $${Number(c.cost_usd || 0).toFixed(2)}</p>
                    </div>
                </div>
            `).join("");
        }

        showNotification("Optimal network path calculated successfully.", "success");
    }

    async function loadBottlenecks() {
        const res = await apiFetch("/network/bottlenecks");
        const container = document.getElementById("bottleneckAlertsContainer");
        if (!container) return;

        if (res.ok && Array.isArray(res.data) && res.data.length > 0) {
            container.innerHTML = res.data.map(b => `
                <div class="alert-bottleneck">
                    <div class="alert-icon-area">⚠️</div>
                    <div class="alert-details">
                        <strong>Capacity Bottleneck: Corridor ${b.source} &rarr; ${b.target}</strong>
                        <p>Capacity Utilization Ratio: <strong>&rho; = ${b.capacity_utilization_ratio || b.load_ratio}</strong> &bull; Betweenness Centrality: <strong>${b.betweenness_centrality}</strong></p>
                        <small class="text-warning">${b.recommendation || 'Divert high-volume freight batches.'}</small>
                    </div>
                </div>
            `).join("");
            showNotification(`Identified ${res.data.length} active network bottlenecks.`, "warning");
        } else {
            showNotification("No critical network bottlenecks exceeding rho >= 0.85.", "success");
        }
    }

    // Expose public API
    window.IMS = {
        refreshDashboardKPIs,
        setTrackingNumber,
        searchTracking,
        lookupShipment,
        simulateStatusAdvance,
        simulateTelemetryPing,
        simulatePodUpload,
        filterInventoryTable,
        openRestockModal,
        closeRestockModal,
        quickRestock,
        submitRestock,
        toggleOutageInputs,
        computeRoute,
        loadBottlenecks
    };

    // Auto-initialize on DOM ready
    document.addEventListener("DOMContentLoaded", () => {
        initRoleSwitcher();
    });
})();
