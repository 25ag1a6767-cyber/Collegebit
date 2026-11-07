/* CampusBite AI v2.0 — Dedicated Staff Kitchen & Counter Portal Controller */
(function () {
  "use strict";

  const TOKEN_KEY = "cb_staff_access_token";
  const REFRESH_KEY = "cb_staff_refresh_token";

  const state = {
    accessToken: sessionStorage.getItem(TOKEN_KEY) || "",
    refreshToken: sessionStorage.getItem(REFRESH_KEY) || "",
    mfaChallengeToken: "",
    user: null,
    counters: [],
    selectedCounterId: "",
    inventoryData: { menuItems: [], ingredients: [] },
    ws: null,
    cameraStream: null,
    scanTimer: null
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showBanner(msg, kind) {
    const b = $("staff-banner");
    if (!b) return;
    if (!msg) {
      b.hidden = true;
      return;
    }
    b.textContent = msg;
    b.className = "status-banner" + (kind ? " " + kind : "");
    b.hidden = false;
  }

  function setBackendStatus(status, detail) {
    const pill = $("staff-ws-pill");
    const label = $("staff-ws-label");
    if (!pill || !label) return;
    const labels = {
      checking: "Checking service…",
      online: "Service online",
      unavailable: "Backend unavailable",
      serverError: "Server error",
      notReady: "Service not ready",
      offline: "Network disconnected"
    };
    pill.className = "ws-pill " + (status === "online" ? "live" : status === "checking" ? "unknown" : "offline");
    label.textContent = labels[status] || labels.unavailable;
    pill.title = detail || label.textContent;
  }

  async function fetchBackend(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      if (!navigator.onLine) {
        setBackendStatus("offline");
        throw new Error("Network disconnected. Check your internet connection and try again.");
      }
      setBackendStatus("unavailable");
      throw new Error("Backend unavailable. Check your connection or try again shortly.");
    }
    if (response.status === 503) setBackendStatus("unavailable");
    else if (response.status >= 500) setBackendStatus("serverError");
    else setBackendStatus("online");
    return response;
  }

  async function checkBackendConnectivity() {
    if (!navigator.onLine) {
      setBackendStatus("offline");
      return false;
    }
    setBackendStatus("checking");
    try {
      const response = await fetchBackend("/api/ready", { headers: { Accept: "application/json" }, cache: "no-store" });
      const health = await response.json().catch(() => ({}));
      if (response.ok && health.status === "ready") {
        setBackendStatus("online");
        return true;
      }
      const checks = health.checks || {};
      const failed = Object.keys(checks).filter((key) => checks[key] !== "ok");
      setBackendStatus("notReady", failed.length ? `Service not ready: ${failed.join(", ")}.` : "Service is not ready.");
    } catch (_) {}
    return false;
  }

  function apiErrorMessage(response, payload) {
    const error = payload.error || {};
    const code = error.code || "";
    if (code === "INVALID_CREDENTIALS") return "Invalid email or password.";
    if (code === "ACCOUNT_DISABLED") return "Staff account inactive. Contact an administrator.";
    if (code === "STAFF_PORTAL_FORBIDDEN") return "This account is not authorized for the staff portal.";
    if (response.status === 503) return "Backend unavailable. Try again shortly.";
    if (response.status >= 500) return "Server error. Try again later.";
    if (response.status === 401) return "Your session expired. Sign in again.";
    return error.message || (typeof payload.detail === "string" ? payload.detail : "") || `Request failed (${response.status}).`;
  }

  async function api(path, opts) {
    const options = opts || {};
    const headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
    if (state.accessToken) headers["Authorization"] = "Bearer " + state.accessToken;
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    options.headers = headers;

    let res = await fetchBackend("/api/v1" + path, options);
    if (res.status === 401 && state.refreshToken && !path.startsWith("/auth/")) {
      const refreshed = await tryRefresh();
      if (refreshed) {
        options.headers["Authorization"] = "Bearer " + state.accessToken;
        res = await fetchBackend("/api/v1" + path, options);
      }
    }
    const payload = await res.json().catch(() => ({}));
    if (!res.ok || payload.success === false || payload.ok === false) {
      if (res.status === 401 && !path.startsWith("/auth/")) clearSession();
      throw new Error(apiErrorMessage(res, payload));
    }
    return payload.data !== undefined ? payload.data : payload;
  }

  async function tryRefresh() {
    const response = await fetchBackend("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken: state.refreshToken })
    });
    const payload = await response.json().catch(() => ({}));
    if (response.ok && payload.success && payload.data && payload.data.accessToken) {
      saveTokens(payload.data.accessToken, payload.data.refreshToken);
      return true;
    }
    clearSession();
    return false;
  }

  function saveTokens(access, refresh) {
    state.accessToken = access || "";
    state.refreshToken = refresh || "";
    sessionStorage.setItem(TOKEN_KEY, state.accessToken);
    if (state.refreshToken) sessionStorage.setItem(REFRESH_KEY, state.refreshToken);
  }

  function clearSession() {
    state.accessToken = "";
    state.refreshToken = "";
    state.user = null;
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(REFRESH_KEY);
    if (state.ws) {
      try { state.ws.close(); } catch (_) {}
      state.ws = null;
    }
    stopCamera();
    $("staff-workspace").hidden = true;
    $("staff-login-section").hidden = false;
    $("staff-login-form").hidden = false;
    $("staff-reset-form").hidden = true;
    $("staff-mfa-section").hidden = true;
    $("staff-change-password-section").hidden = true;
    $("staff-logout-btn").hidden = true;
    $("staff-user-badge").hidden = true;
    $("staff-counter-select").hidden = true;
    checkBackendConnectivity();
  }

  function connectWebSocket() {
    if (!state.accessToken) return;
    if (state.ws) {
      try { state.ws.close(); } catch (_) {}
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${location.host}/api/v1/ws?token=${encodeURIComponent(state.accessToken)}`;
    const sock = new WebSocket(wsUrl);
    state.ws = sock;

    sock.addEventListener("close", () => {
      checkBackendConnectivity();
      if (state.accessToken) {
        setTimeout(() => {
          if (state.accessToken && state.ws === sock) connectWebSocket();
        }, 4000);
      }
    });
    sock.addEventListener("message", (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        const topic = String(msg.topic || "");
        if (topic.startsWith("kitchen.") || topic.startsWith("order.") || topic.startsWith("inventory.")) {
          loadQueue();
          if (!$("tab-inventory").hidden) loadInventory();
          if (!$("tab-performance").hidden) loadPerformance();
        }
      } catch (_) {}
    });
  }

  async function bootstrapAuthenticated() {
    try {
      const me = await api("/me");
      const role = String(me.role || "").toUpperCase();
      if (role !== "STAFF" && role !== "ADMIN") {
        clearSession();
        showBanner("This account is not authorized for the staff portal.", "error");
        return;
      }
      state.user = me;
      if (me.mustChangePassword) {
        $("staff-login-section").hidden = true;
        $("staff-workspace").hidden = true;
        $("staff-change-password-section").hidden = false;
        $("staff-logout-btn").hidden = false;
        $("staff-user-badge").textContent = `${me.fullName} (${role})`;
        $("staff-user-badge").hidden = false;
        showBanner("Change your temporary password to continue.", "error");
        return;
      }
      $("staff-change-password-section").hidden = true;
      $("staff-login-section").hidden = true;
      $("staff-workspace").hidden = false;
      $("staff-logout-btn").hidden = false;
      const badge = $("staff-user-badge");
      badge.textContent = `${me.fullName} (${role})`;
      badge.hidden = false;

      // Apply permission gating to stock movement form
      const perms = me.staffPermissions || {};
      $("stock-submit-btn").disabled = perms.canManageInventory === false;

      await loadCounters();
      await loadQueue();
      connectWebSocket();
      showBanner("", "");
    } catch (err) {
      clearSession();
      showBanner(err.message, "error");
    }
  }

  async function loadCounters() {
    try {
      const d = await api("/counters");
      state.counters = d.items || [];
      const sel = $("staff-counter-select");
      const assigned = (state.user && state.user.assignedCounterIds) || [];
      const visible = assigned.length ? state.counters.filter((c) => assigned.includes(c.id)) : state.counters;
      sel.replaceChildren(el("option", "", "All Assigned Counters"));
      sel.firstChild.value = "";
      visible.forEach((c) => {
        const opt = el("option", "", `${c.name} (${c.location})`);
        opt.value = String(c.id);
        sel.appendChild(opt);
      });
      sel.hidden = false;
    } catch (_) {}
  }

  function counterQuery(prefix) {
    return state.selectedCounterId ? `${prefix}counterId=${encodeURIComponent(state.selectedCounterId)}` : "";
  }

  // --- TAB 1: Live Orders & KDS ---
  async function loadQueue() {
    try {
      const statusVal = $("kds-status-filter").value;
      const qs = [];
      if (state.selectedCounterId) qs.push("counterId=" + encodeURIComponent(state.selectedCounterId));
      if (statusVal) qs.push("status=" + encodeURIComponent(statusVal));
      const data = await api("/staff/orders" + (qs.length ? "?" + qs.join("&") : ""));
      const s = data.summary || {};
      $("kpi-new").textContent = String(s.newCount || 0);
      $("kpi-accepted").textContent = String(s.acceptedCount || 0);
      $("kpi-preparing").textContent = String(s.preparingCount || 0);
      $("kpi-ready").textContent = String(s.readyCount || 0);
      $("kpi-overdue").textContent = String(s.overdueCount || 0);
      $("kpi-collected").textContent = String(s.collectedToday || 0);

      renderKdsTickets(data.items || []);
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function renderKdsTickets(orders) {
    const grid = $("kds-tickets");
    if (!orders.length) {
      grid.replaceChildren(el("p", "ticket-meta", "No active paid orders in queue right now."));
      return;
    }
    const perms = (state.user && state.user.staffPermissions) || {};
    const frag = document.createDocumentFragment();

    orders.forEach((o) => {
      const raw = String(o.rawStatus || "").toLowerCase();
      const cls = "ticket-card" + (o.isOverdue ? " overdue" : raw === "ready" ? " ready" : "");
      const card = el("article", cls);

      const top = el("div", "ticket-top");
      top.appendChild(el("span", "token-badge", `Token #${o.tokenNumber || o.id}`));
      top.appendChild(el("span", "state-pill", o.status));
      card.appendChild(top);

      const meta = el("div", "ticket-meta");
      const custName = o.customer ? o.customer.fullName : "Student";
      meta.textContent = `${o.orderNumber} · ${custName} · ${o.elapsedMinutes || 0}m ago · ₹${o.total}`;
      card.appendChild(meta);

      const ul = el("ul", "ticket-items");
      (o.items || []).forEach((it) => {
        const custPairs = it.customizations ? Object.entries(it.customizations).map(([k, v]) => `${k}: ${v}`).join(", ") : "";
        ul.appendChild(el("li", "", `${it.quantity}× ${it.name}${custPairs ? " (" + custPairs + ")" : ""}`));
      });
      card.appendChild(ul);

      if (o.notes) {
        card.appendChild(el("div", "ticket-meta", "Note: " + o.notes));
      }

      const actions = el("div", "ticket-actions");
      if (raw === "pending" || raw === "confirmed") {
        const btnAcc = el("button", "btn btn-primary", "Accept");
        btnAcc.type = "button";
        btnAcc.disabled = perms.canAcceptOrders === false;
        btnAcc.addEventListener("click", () => transitionOrder(o.id, "accept"));
        actions.appendChild(btnAcc);

        const btnRej = el("button", "btn btn-danger", "Reject");
        btnRej.type = "button";
        btnRej.disabled = perms.canRejectOrders === false;
        btnRej.addEventListener("click", () => openReasonModal(o.id, "reject"));
        actions.appendChild(btnRej);
      }

      if (raw === "accepted" || raw === "confirmed") {
        const btnStart = el("button", "btn btn-accent", "Start Prep");
        btnStart.type = "button";
        btnStart.disabled = perms.canUpdatePrep === false;
        btnStart.addEventListener("click", () => transitionOrder(o.id, "start"));
        actions.appendChild(btnStart);
      }

      if (raw === "accepted" || raw === "preparing") {
        const btnReady = el("button", "btn btn-ok", "Mark Ready");
        btnReady.type = "button";
        btnReady.disabled = perms.canUpdatePrep === false;
        btnReady.addEventListener("click", () => transitionOrder(o.id, "ready"));
        actions.appendChild(btnReady);
      }

      if (raw === "ready") {
        const btnGoPickup = el("button", "btn btn-primary", `Verify Pickup (ID ${o.id})`);
        btnGoPickup.type = "button";
        btnGoPickup.addEventListener("click", () => {
          $("manual-order-id").value = String(o.id);
          switchTab("pickup");
        });
        actions.appendChild(btnGoPickup);
      }

      const btnExc = el("button", "btn btn-outline", "Issue / Exception");
      btnExc.type = "button";
      btnExc.addEventListener("click", () => openReasonModal(o.id, "exception"));
      actions.appendChild(btnExc);

      card.appendChild(actions);
      frag.appendChild(card);
    });

    grid.replaceChildren(frag);
  }

  async function transitionOrder(orderId, action) {
    try {
      await api(`/staff/orders/${orderId}/${action}`, { method: "POST", body: {} });
      showBanner(`Order #${orderId} updated (${action.toUpperCase()}).`, "success");
      await loadQueue();
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function openReasonModal(orderId, kind) {
    $("reason-order-id").value = String(orderId);
    $("reason-action-kind").value = kind;
    $("reason-text-input").value = "";
    $("reason-modal-title").textContent =
      kind === "reject" ? `Reject Order #${orderId} & Initiate Full Refund` : `Flag Kitchen Exception on Order #${orderId}`;
    $("staff-reason-modal").hidden = false;
    $("reason-text-input").focus();
  }

  // --- TAB 2: Dedicated QR & Token Inspection and Collection Screen (Part 5 & Part 6) ---
  let activeInspectedOrder = null;

  async function inspectPickupPayload(body) {
    const box = $("pickup-result-box");
    box.hidden = true;
    try {
      const res = await api("/pickups/inspect", { method: "POST", body });
      activeInspectedOrder = res;
      renderInspectionScreen(res, body);
    } catch (err) {
      activeInspectedOrder = null;
      $("inspection-empty").hidden = false;
      $("inspection-card").hidden = true;
      box.className = "status-banner error";
      box.textContent = err.message;
      box.hidden = false;
    }
  }

  function renderInspectionScreen(data, requestedPayload) {
    $("inspection-empty").hidden = true;
    const card = $("inspection-card");
    card.hidden = false;

    const t = data.token || {};
    const c = data.customer || {};
    const p = data.payment || {};
    const prep = data.preparation || {};
    const col = data.collection || {};
    const items = data.items || [];

    const frag = document.createDocumentFragment();

    // 1. Top Token & Order Banner (XSS Safe DOM)
    const head = el("div", "inspect-head");
    const leftTok = el("div");
    const tokSpan = el("span", "token-hero-num", t.tokenDisplay || ("#" + t.tokenNumber));
    const ordSmall = el("small", "token-hero-sub", `Order ${t.orderNumber}`);
    leftTok.append(tokSpan, ordSmall);
    head.appendChild(leftTok);

    const rightBadge = el("span", "state-pill", prep.status);
    head.appendChild(rightBadge);
    frag.appendChild(head);

    // 2. Customer Information (Part 5A) (XSS Safe DOM)
    const custBox = el("div", "inspect-subcard");
    const studentLine = el("div", "student-name-line", `Student: ${c.fullName || ""}`);
    const metaLine = el("div", "student-meta-line");
    metaLine.append(
      document.createTextNode("College ID: "),
      el("strong", "", c.studentId || "N/A"),
      document.createTextNode(" · Phone: "),
      el("strong", "", c.phoneMasked || "N/A"),
      document.createTextNode(" · Counter: "),
      el("strong", "", t.counterName || "Main Counter")
    );
    custBox.append(studentLine, metaLine);
    frag.appendChild(custBox);

    // 3. Complete Food Order Breakdown (Part 5C) (XSS Safe DOM)
    const itemsHead = el("h4", "items-inspect-head", "Ordered Food Items:");
    frag.appendChild(itemsHead);

    const table = el("table", "items-inspect-table data-table");
    const tbody = el("tbody");
    const selectedItemCheckboxes = [];

    items.forEach((it) => {
      const tr = el("tr", "item-inspect-row");
      const checkTd = el("td", "item-check-cell");
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.value = it.id;
      chk.dataset.itemId = it.id;
      if (it.isCollected) {
        chk.checked = true;
        chk.disabled = true;
      } else {
        chk.checked = true;
        selectedItemCheckboxes.push(chk);
      }
      checkTd.appendChild(chk);

      const custText = it.customizations && Object.keys(it.customizations).length ? ` (${Object.entries(it.customizations).map(([k, v]) => `${k}: ${v}`).join(", ")})` : "";
      const nameTd = el("td", "item-name-cell", `${it.quantity}× ${it.name}${custText}`);
      if (it.isCollected) {
        const badge = el("span", "badge badge-success", " (Collected)");
        badge.style.fontSize = "11px";
        badge.style.marginLeft = "6px";
        nameTd.appendChild(badge);
      }
      const counterTd = el("td", "item-counter-cell", `[${it.counterName || "Kitchen"}]`);
      const totalTd = el("td", "item-total-cell", `₹${it.lineTotalRupees}`);
      tr.append(checkTd, nameTd, counterTd, totalTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    frag.appendChild(table);

    // 4. Payment Verification (Part 5D) (XSS Safe DOM)
    const payBox = el("div", `pay-box-inspect ${p.status === "PAID" ? "pay-box-paid" : "pay-box-pending"}`);
    const payInfo = el("div");
    payInfo.append(
      el("strong", "", `Payment: ${p.status}`),
      document.createTextNode(` · ${p.method} · Ref: `),
      el("code", "", p.referenceMasked || "—")
    );
    const totalDiv = el("div", "pay-total-amount", `Total: ₹${p.totalRupees}`);
    payBox.append(payInfo, totalDiv);
    frag.appendChild(payBox);

    // 5. Decision & Handover Action (Part 6)
    const actBox = el("div", "handover-actions");
    actBox.style.textAlign = "center";

    if (data.eligibleForCollection) {
      const btnCollect = el("button", "btn btn-ok btn-handover-full", "✅ Confirm Handover & Mark Collected");
      btnCollect.type = "button";
      btnCollect.addEventListener("click", () => {
        const chosenIds = selectedItemCheckboxes.filter(c => c.checked).map(c => Number(c.value));
        if (selectedItemCheckboxes.length > 0 && chosenIds.length === 0) {
          alert("Please select at least one item to hand over.");
          return;
        }
        verifyPickupPayload({
          orderId: t.orderId,
          pickupToken: requestedPayload.pickupToken || undefined,
          manualCode: requestedPayload.manualCode || undefined,
          itemIds: chosenIds.length > 0 ? chosenIds : undefined,
        });
      });
      actBox.appendChild(btnCollect);
    } else {
      const alertDiv = el("div", "status-banner error", data.blockReason || "Order is not currently eligible for handover.");
      alertDiv.style.marginBottom = "10px";
      alertDiv.style.fontSize = "14px";
      actBox.appendChild(alertDiv);

      const btnDisabled = el("button", "btn btn-outline", "Handover Blocked");
      btnDisabled.type = "button";
      btnDisabled.disabled = true;
      btnDisabled.style.width = "100%";
      actBox.appendChild(btnDisabled);
    }
    frag.appendChild(actBox);

    card.replaceChildren(frag);
  }

  async function verifyPickupPayload(body) {
    const box = $("pickup-result-box");
    try {
      const res = await api("/pickups/verify", { method: "POST", body });
      box.className = "status-banner success";
      box.textContent = `Verified & Collected! Order ${res.order.orderNumber} (Token #${res.order.tokenNumber}) handed over.`;
      box.hidden = false;
      $("qr-token-input").value = "";
      $("quick-token-input").value = "";
      $("manual-code-input").value = "";
      $("manual-order-id").value = "";

      // Re-inspect to update card with collected stamp
      await inspectPickupPayload({ orderId: res.order.id });
      await loadQueue();
    } catch (err) {
      box.className = "status-banner error";
      box.textContent = err.message;
      box.hidden = false;
    }
  }

  // --- Smart Token Management Dashboard (Part 3) ---
  async function loadTokenManagement() {
    const tbody = $("token-table-body");
    try {
      const searchVal = $("token-search-input").value.trim();
      const statusVal = $("token-status-filter").value;
      const qs = [];
      if (searchVal) qs.push("search=" + encodeURIComponent(searchVal));
      if (statusVal && statusVal !== "all") qs.push("status=" + encodeURIComponent(statusVal));
      if (state.selectedCounterId) qs.push("counterId=" + encodeURIComponent(state.selectedCounterId));
      const res = await api("/tokens" + (qs.length ? "?" + qs.join("&") : ""));
      const items = res.items || [];
      if (!items.length) {
        tbody.replaceChildren();
        const trEmpty = el("tr");
        const tdEmpty = el("td", "empty-tokens-cell", "No tokens match your search criteria.");
        tdEmpty.colSpan = 8;
        trEmpty.appendChild(tdEmpty);
        tbody.appendChild(trEmpty);
        return;
      }
      const frag = document.createDocumentFragment();
      items.forEach((t) => {
        const tr = el("tr");
        const tokTd = el("td", "tok-display-cell", t.tokenDisplay || "");
        const ordTd = el("td", "", t.orderNumber || "");
        const custTd = el("td");
        custTd.append(
          el("strong", "", t.customerName || ""),
          el("br"),
          el("small", "token-hero-sub", t.studentId || "")
        );
        const cntTd = el("td", "", t.counterName || "");
        const summaryTd = el("td", "item-counter-cell", t.itemsSummary || "");
        const amtTd = el("td", "item-name-cell", `₹${t.totalRupees || 0}`);
        const stateTd = el("td");
        stateTd.appendChild(el("span", "state-pill", t.status || ""));
        const actTd = el("td");
        const btnInspect = el("button", "btn btn-outline", "Inspect / Verify");
        btnInspect.type = "button";
        btnInspect.addEventListener("click", () => {
          $("quick-token-input").value = t.tokenDisplay || "";
          switchTab("pickup");
          inspectPickupPayload({ orderId: t.orderId });
        });
        actTd.appendChild(btnInspect);
        tr.append(tokTd, ordTd, custTd, cntTd, summaryTd, amtTd, stateTd, actTd);
        frag.appendChild(tr);
      });
      tbody.replaceChildren(frag);
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  async function toggleCameraScanner() {
    const video = $("qr-video");
    if (state.cameraStream) {
      stopCamera();
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showBanner("Camera access is not supported in this browser; paste the QR token or use the 6-char manual code.", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      state.cameraStream = stream;
      video.srcObject = stream;
      video.hidden = false;
      await video.play();

      if ("BarcodeDetector" in window) {
        const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        state.scanTimer = setInterval(async () => {
          try {
            const codes = await detector.detect(video);
            if (codes && codes.length && codes[0].rawValue) {
              const raw = codes[0].rawValue;
              stopCamera();
              $("qr-token-input").value = raw;
              await inspectPickupPayload({ pickupToken: raw });
            }
          } catch (_) {}
        }, 600);
      } else {
        showBanner("Camera active — browser lacks native BarcodeDetector; use scanner input or manual 6-char code.", "");
      }
    } catch (err) {
      showBanner("Could not open camera: " + err.message, "error");
    }
  }

  function stopCamera() {
    if (state.scanTimer) {
      clearInterval(state.scanTimer);
      state.scanTimer = null;
    }
    if (state.cameraStream) {
      state.cameraStream.getTracks().forEach((t) => t.stop());
      state.cameraStream = null;
    }
    const video = $("qr-video");
    if (video) {
      video.srcObject = null;
      video.hidden = true;
    }
  }

  // --- TAB 3: Availability, Inventory & Low-Stock Alerts ---
  async function loadInventory() {
    try {
      const [inv, alerts] = await Promise.all([
        api("/staff/inventory" + counterQuery("?")),
        api("/staff/inventory/alerts")
      ]);
      state.inventoryData = inv;
      renderAlerts(alerts);
      populateStockTargetSelect();
      renderMenuAvailabilityTable(inv.menuItems || []);
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function renderAlerts(alerts) {
    const box = $("staff-alerts-box");
    const lowItems = alerts.lowStockMenuItems || [];
    const lowIngs = alerts.lowStockIngredients || [];
    const expBatches = alerts.expiringBatches || [];
    if (!lowItems.length && !lowIngs.length && !expBatches.length) {
      box.replaceChildren(el("span", "badge-ok", "All menu items, ingredients & batches are healthy."));
      return;
    }
    const ul = el("ul", "ticket-items");
    lowItems.forEach((p) => ul.appendChild(el("li", "", `LOW MENU STOCK: ${p.name} — ${p.stockQuantity} left (threshold ${p.lowStockThreshold})`)));
    lowIngs.forEach((i) => ul.appendChild(el("li", "", `LOW INGREDIENT: ${i.name} — ${i.currentQuantity} ${i.unit} (reorder ${i.reorderLevel} ${i.unit})`)));
    expBatches.forEach((b) => ul.appendChild(el("li", "", `BATCH EXPIRY: ${b.itemName} [${b.batchCode}] — ${b.quantityRemaining} units (exp ${b.expiryDate})`)));
    box.replaceChildren(ul);
  }

  function populateStockTargetSelect() {
    const kind = $("stock-target-kind").value;
    const sel = $("stock-target-id");
    sel.replaceChildren();
    if (kind === "menuItem") {
      (state.inventoryData.menuItems || []).forEach((p) => {
        const opt = el("option", "", `${p.name} (${p.stockQuantity} units)`);
        opt.value = String(p.id);
        sel.appendChild(opt);
      });
    } else {
      (state.inventoryData.ingredients || []).forEach((ing) => {
        const opt = el("option", "", `${ing.name} (${ing.currentQuantity} ${ing.unit})`);
        opt.value = String(ing.id);
        sel.appendChild(opt);
      });
    }
  }

  function renderMenuAvailabilityTable(items) {
    const tbody = $("staff-menu-tbody");
    const perms = (state.user && state.user.staffPermissions) || {};
    const frag = document.createDocumentFragment();
    items.forEach((p) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", p.name));
      tr.appendChild(el("td", "", String(p.stockQuantity)));
      const statusTd = el("td");
      statusTd.appendChild(el("span", p.isAvailable ? "badge-ok" : "badge-low", p.isAvailable ? "Available" : "Paused / Out"));
      tr.appendChild(statusTd);

      const actTd = el("td");
      const btn = el("button", "btn btn-outline", p.isAvailable ? "Pause" : "Enable");
      btn.type = "button";
      btn.disabled = perms.canManageAvailability === false;
      btn.addEventListener("click", async () => {
        try {
          await api(`/staff/menu-items/${p.id}/availability`, {
            method: "PATCH",
            body: { isAvailable: !p.isAvailable, reason: "Updated on Staff Portal" }
          });
          await loadInventory();
        } catch (err) {
          showBanner(err.message, "error");
        }
      });
      actTd.appendChild(btn);
      tr.appendChild(actTd);
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
  }

  // --- TAB 4: Completed Order History ---
  async function loadHistory() {
    try {
      const dt = $("history-date-input").value;
      const qs = [];
      if (state.selectedCounterId) qs.push("counterId=" + encodeURIComponent(state.selectedCounterId));
      if (dt) qs.push("serviceDate=" + encodeURIComponent(dt));
      const d = await api("/staff/orders/history" + (qs.length ? "?" + qs.join("&") : ""));
      const tbody = $("staff-history-tbody");
      const frag = document.createDocumentFragment();
      (d.items || []).forEach((o) => {
        const tr = el("tr");
        tr.appendChild(el("td", "", o.orderNumber));
        tr.appendChild(el("td", "", o.tokenNumber ? "#" + o.tokenNumber : "-"));
        tr.appendChild(el("td", "", o.customer ? o.customer.fullName : "-"));
        tr.appendChild(el("td", "", (o.items || []).map((i) => `${i.quantity}x ${i.name}`).join(", ")));
        tr.appendChild(el("td", "", o.status));
        tr.appendChild(el("td", "", "₹" + o.total));
        tr.appendChild(el("td", "", o.collectedAt ? new Date(o.collectedAt).toLocaleTimeString() : (o.cancelledAt ? new Date(o.cancelledAt).toLocaleTimeString() : "-")));
        frag.appendChild(tr);
      });
      tbody.replaceChildren(frag);
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  // --- TAB 5: Daily Staff Performance & Processing Stats ---
  async function loadPerformance() {
    try {
      const d = await api("/staff/performance" + counterQuery("?"));
      const my = d.myStatsToday || {};
      const cs = d.counterStatsToday || {};
      $("perf-my-accepted").textContent = String(my.ordersAccepted || 0);
      $("perf-my-ready").textContent = String(my.ordersMarkedReady || 0);
      $("perf-my-collected").textContent = String(my.ordersCollected || 0);
      $("perf-avg-prep").textContent = (cs.avgPrepMinutes || 0) + "m";
      $("perf-med-prep").textContent = (cs.medianPrepMinutes || 0) + "m";
      $("perf-sla-pct").textContent = (cs.onTimeSlaPct !== undefined ? cs.onTimeSlaPct : 100) + "%";

      const tp = cs.hourlyThroughput || [];
      if (window.CampusBiteCharts) {
        window.CampusBiteCharts.renderBarChart($("staff-throughput-chart"), {
          title: "Hourly Order Throughput",
          labels: tp.map((x) => x.hour),
          series: [{ name: "Paid Orders", values: tp.map((x) => x.orders), color: "#0f766e" }]
        });
      }
    } catch (err) {
      showBanner(err.message, "error");
    }
  }

  function switchTab(tabName) {
    document.querySelectorAll("[data-staff-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-staff-tab") === tabName);
    });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
      panel.hidden = panel.getAttribute("data-tab-panel") !== tabName;
    });
    if (tabName !== "pickup") stopCamera();
    if (tabName === "queue") loadQueue();
    if (tabName === "tokens") loadTokenManagement();
    if (tabName === "inventory") loadInventory();
    if (tabName === "history") loadHistory();
    if (tabName === "performance") loadPerformance();
  }

  // --- Wire Events ---
  document.addEventListener("DOMContentLoaded", () => {
    $("staff-login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      showBanner("", "");
      try {
        const identifier = $("staff-login-id").value.trim();
        const password = $("staff-login-pw").value;
        const res = await api("/auth/staff/login", {
          method: "POST",
          body: { identifier, password }
        });
        if (res.mfaRequired) {
          state.mfaChallengeToken = res.mfaChallengeToken;
          $("staff-login-section").hidden = true;
          $("staff-mfa-section").hidden = false;
          $("staff-mfa-code").focus();
          return;
        }
        saveTokens(res.accessToken, res.refreshToken);
        await bootstrapAuthenticated();
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    $("staff-forgot-btn").addEventListener("click", () => {
      $("staff-login-form").hidden = true;
      $("staff-reset-form").hidden = false;
      $("staff-reset-email").value = $("staff-login-id").value.includes("@") ? $("staff-login-id").value.trim() : "";
      $("staff-reset-email").focus();
      showBanner("", "");
    });

    $("staff-reset-cancel").addEventListener("click", () => {
      $("staff-reset-form").hidden = true;
      $("staff-login-form").hidden = false;
      showBanner("", "");
    });

    $("staff-reset-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const response = await fetchBackend("/api/auth/forgot-password", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ email: $("staff-reset-email").value.trim().toLowerCase() })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(response.status === 503 ? "Backend unavailable. Try again shortly." : "Could not request a reset link. Try again later.");
        }
        $("staff-reset-form").hidden = true;
        $("staff-login-form").hidden = false;
        showBanner(payload.message || "If an account exists for that email, a reset link has been sent.", "success");
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    $("staff-mfa-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const result = await api("/auth/mfa/verify-login", {
          method: "POST",
          body: {
            mfaChallengeToken: state.mfaChallengeToken,
            code: $("staff-mfa-code").value.trim()
          }
        });
        saveTokens(result.accessToken, result.refreshToken);
        state.mfaChallengeToken = "";
        $("staff-mfa-code").value = "";
        $("staff-mfa-section").hidden = true;
        await bootstrapAuthenticated();
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    $("staff-mfa-cancel").addEventListener("click", () => {
      clearSession();
      showBanner("Sign in was cancelled.", "");
    });

    $("staff-change-password-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const result = await api("/users/me/change-password", {
          method: "POST",
          body: {
            currentPassword: $("staff-current-password").value,
            newPassword: $("staff-new-password").value
          }
        });
        saveTokens(result.accessToken, result.refreshToken);
        $("staff-change-password-form").reset();
        await bootstrapAuthenticated();
        showBanner("Password updated.", "success");
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    $("staff-logout-btn").addEventListener("click", async () => {
      let logoutError = "";
      try {
        await api("/auth/logout", { method: "POST", body: { refreshToken: state.refreshToken } });
      } catch (err) {
        logoutError = err.message;
      }
      clearSession();
      if (logoutError) showBanner(`Signed out on this device. The server session could not be revoked: ${logoutError}`, "error");
      else showBanner("Signed out.", "success");
    });

    $("staff-counter-select").addEventListener("change", (e) => {
      state.selectedCounterId = e.target.value;
      loadQueue();
    });

    document.querySelectorAll("[data-staff-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-staff-tab")));
    });

    $("kds-status-filter").addEventListener("change", loadQueue);
    $("kds-refresh-btn").addEventListener("click", loadQueue);
    $("inv-refresh-btn").addEventListener("click", loadInventory);
    $("history-load-btn").addEventListener("click", loadHistory);
    $("perf-refresh-btn").addEventListener("click", loadPerformance);
    $("stock-target-kind").addEventListener("change", populateStockTargetSelect);

    $("qr-camera-btn").addEventListener("click", toggleCameraScanner);
    $("qr-verify-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const tok = $("qr-token-input").value.trim();
      if (tok) inspectPickupPayload({ pickupToken: tok });
    });

    const quickTokForm = $("quick-token-form");
    if (quickTokForm) {
      quickTokForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const val = $("quick-token-input").value.trim();
        if (val) inspectPickupPayload({ tokenString: val });
      });
    }

    $("manual-verify-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const oid = Number($("manual-order-id").value);
      const code = $("manual-code-input").value.trim();
      if (oid && code) inspectPickupPayload({ orderId: oid, manualCode: code });
      else if (oid) inspectPickupPayload({ orderId: oid });
    });

    const btnTokenRefresh = $("token-refresh-btn");
    if (btnTokenRefresh) btnTokenRefresh.addEventListener("click", loadTokenManagement);
    const tokenSearchInput = $("token-search-input");
    if (tokenSearchInput) {
      tokenSearchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          loadTokenManagement();
        }
      });
    }
    const tokenStatusFilter = $("token-status-filter");
    if (tokenStatusFilter) tokenStatusFilter.addEventListener("change", loadTokenManagement);

    $("staff-stock-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const kind = $("stock-target-kind").value;
        const targetId = Number($("stock-target-id").value);
        const movementType = $("stock-movement-type").value;
        const quantity = Number($("stock-qty").value);
        const reason = $("stock-reason").value.trim();
        const body = { movementType, quantity, reason };
        if (kind === "menuItem") body.menuItemId = targetId;
        else body.ingredientId = targetId;
        await api("/staff/inventory/movements", { method: "POST", body });
        $("stock-qty").value = "";
        $("stock-reason").value = "";
        showBanner("Stock movement recorded.", "success");
        await loadInventory();
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    $("reason-cancel-btn").addEventListener("click", () => {
      $("staff-reason-modal").hidden = true;
    });
    $("staff-reason-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const oid = Number($("reason-order-id").value);
      const kind = $("reason-action-kind").value;
      const reason = $("reason-text-input").value.trim();
      $("staff-reason-modal").hidden = true;
      try {
        if (kind === "reject") {
          await api(`/staff/orders/${oid}/reject`, { method: "POST", body: { reason } });
          showBanner(`Order #${oid} rejected; stock restored & full refund initiated.`, "success");
        } else {
          await api(`/staff/orders/${oid}/exception`, { method: "POST", body: { reason, cancelAndRefund: false } });
          showBanner(`Kitchen exception logged on Order #${oid}.`, "success");
        }
        await loadQueue();
      } catch (err) {
        showBanner(err.message, "error");
      }
    });

    window.addEventListener("offline", () => setBackendStatus("offline"));
    window.addEventListener("online", checkBackendConnectivity);
    checkBackendConnectivity();
    window.setInterval(checkBackendConnectivity, 30000);

    if (state.accessToken) {
      bootstrapAuthenticated();
    }
  });
})();
