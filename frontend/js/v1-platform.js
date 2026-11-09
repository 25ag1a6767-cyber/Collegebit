"use strict";

(() => {
  const $ = sel => document.querySelector(sel);
  const $$ = sel => Array.from(document.querySelectorAll(sel));
  const paise = val => `₹${(Number(val || 0) / 100).toFixed(2)}`;
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));

  // ============================================================================
  // 1. Dependency-Free ISO/IEC 18004 QR Code SVG Generator (V1-V5, ECC-L, Byte)
  // ============================================================================
  const QR_VERSIONS = [
    null,
    { size: 21, dataBytes: 19, ecBytes: 7, align: [] },
    { size: 25, dataBytes: 34, ecBytes: 10, align: [6, 18] },
    { size: 29, dataBytes: 55, ecBytes: 15, align: [6, 22] },
    { size: 33, dataBytes: 80, ecBytes: 20, align: [6, 26] },
    { size: 37, dataBytes: 108, ecBytes: 26, align: [6, 30] }
  ];

  function gfMul(a, b) {
    let p = 0;
    for (let i = 0; i < 8; i++) {
      if (b & 1) p ^= a;
      const hi = a & 0x80;
      a = (a << 1) & 0xff;
      if (hi) a ^= 0x11d;
      b >>= 1;
    }
    return p;
  }

  function rsGeneratorPoly(degree) {
    let poly = [1];
    let root = 1;
    for (let i = 0; i < degree; i++) {
      const next = new Array(poly.length + 1).fill(0);
      for (let j = 0; j < poly.length; j++) {
        next[j] ^= poly[j];
        next[j + 1] ^= gfMul(poly[j], root);
      }
      poly = next;
      root = gfMul(root, 2);
    }
    return poly;
  }

  function rsRemainder(data, ecLen) {
    const gen = rsGeneratorPoly(ecLen);
    const rem = new Array(ecLen).fill(0);
    for (const byte of data) {
      const factor = byte ^ rem[0];
      rem.shift();
      rem.push(0);
      for (let i = 0; i < ecLen; i++) {
        rem[i] ^= gfMul(gen[i + 1], factor);
      }
    }
    return rem;
  }

  function renderQrSvg(text) {
    const utf8 = Array.from(new TextEncoder().encode(String(text)));
    let ver = 1;
    while (ver <= 5 && utf8.length + 2 > QR_VERSIONS[ver].dataBytes) ver++;
    if (ver > 5) {
      // Truncate only if unexpectedly oversized; pickup tokens are ~46 bytes (V3)
      utf8.length = QR_VERSIONS[5].dataBytes - 2;
      ver = 5;
    }
    const spec = QR_VERSIONS[ver];
    const bits = [];
    const pushBits = (val, len) => {
      for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1);
    };
    pushBits(0b0100, 4); // Byte mode
    pushBits(utf8.length, 8);
    for (const b of utf8) pushBits(b, 8);
    const maxBits = spec.dataBytes * 8;
    pushBits(0, Math.min(4, maxBits - bits.length));
    while (bits.length % 8 !== 0) bits.push(0);
    const dataCodewords = [];
    for (let i = 0; i < bits.length; i += 8) {
      let byte = 0;
      for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
      dataCodewords.push(byte);
    }
    let padToggle = true;
    while (dataCodewords.length < spec.dataBytes) {
      dataCodewords.push(padToggle ? 0xec : 0x11);
      padToggle = !padToggle;
    }
    const allCodewords = dataCodewords.concat(rsRemainder(dataCodewords, spec.ecBytes));

    const n = spec.size;
    const grid = Array.from({ length: n }, () => new Array(n).fill(false));
    const reserved = Array.from({ length: n }, () => new Array(n).fill(false));

    const setMod = (r, c, dark) => {
      if (r >= 0 && r < n && c >= 0 && c < n) {
        grid[r][c] = Boolean(dark);
        reserved[r][c] = true;
      }
    };

    const placeFinder = (top, left) => {
      for (let dr = -1; dr <= 7; dr++) {
        for (let dc = -1; dc <= 7; dc++) {
          const inOuter = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
          const inBorder = dr === 0 || dr === 6 || dc === 0 || dc === 6;
          const inCore = dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4;
          setMod(top + dr, left + dc, inOuter && (inBorder || inCore));
        }
      }
    };
    placeFinder(0, 0);
    placeFinder(0, n - 7);
    placeFinder(n - 7, 0);

    for (const r of spec.align) {
      for (const c of spec.align) {
        if (reserved[r][c]) continue;
        for (let dr = -2; dr <= 2; dr++) {
          for (let dc = -2; dc <= 2; dc++) {
            const dark = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
            setMod(r + dr, c + dc, dark);
          }
        }
      }
    }

    for (let i = 8; i < n - 8; i++) {
      if (!reserved[6][i]) setMod(6, i, i % 2 === 0);
      if (!reserved[i][6]) setMod(i, 6, i % 2 === 0);
    }
    setMod(n - 8, 8, true);

    // Reserve format info areas
    for (let i = 0; i < 9; i++) {
      if (!reserved[8][i]) reserved[8][i] = true;
      if (!reserved[i][8]) reserved[i][8] = true;
    }
    for (let i = 0; i < 8; i++) {
      reserved[8][n - 1 - i] = true;
      reserved[n - 1 - i][8] = true;
    }

    // Place data bits with Mask 0: (r + c) % 2 === 0
    let bitIdx = 0;
    const totalBits = allCodewords.length * 8;
    let upward = true;
    for (let right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < n; vert++) {
        const r = upward ? n - 1 - vert : vert;
        for (let j = 0; j < 2; j++) {
          const c = right - j;
          if (reserved[r][c]) continue;
          let dark = false;
          if (bitIdx < totalBits) {
            dark = ((allCodewords[bitIdx >> 3] >> (7 - (bitIdx & 7))) & 1) === 1;
            bitIdx++;
          }
          if ((r + c) % 2 === 0) dark = !dark;
          grid[r][c] = dark;
        }
      }
      upward = !upward;
    }

    // Format info for ECC Level L (01) + Mask 0 (000) => 0x77c4
    const fmt = 0x77c4;
    for (let i = 0; i <= 5; i++) grid[8][i] = ((fmt >> (14 - i)) & 1) === 1;
    grid[8][7] = ((fmt >> 8) & 1) === 1;
    grid[8][8] = ((fmt >> 7) & 1) === 1;
    grid[7][8] = ((fmt >> 6) & 1) === 1;
    for (let i = 9; i < 15; i++) grid[14 - i][8] = ((fmt >> (14 - i)) & 1) === 1;
    for (let i = 0; i < 8; i++) grid[8][n - 1 - i] = ((fmt >> i) & 1) === 1;
    for (let i = 8; i < 15; i++) grid[n - 15 + i][8] = ((fmt >> i) & 1) === 1;

    const quiet = 2;
    const dim = n + quiet * 2;
    let paths = "";
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (grid[r][c]) paths += `M${c + quiet},${r + quiet}h1v1h-1z`;
      }
    }
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${dim} ${dim}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Pickup QR Code");
    const bg = document.createElementNS(svgNS, "rect");
    bg.setAttribute("width", String(dim));
    bg.setAttribute("height", String(dim));
    bg.setAttribute("fill", "#ffffff");
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", paths);
    path.setAttribute("fill", "#12372a");
    svg.append(bg, path);
    return svg;
  }

  // ============================================================================
  // 2. Shared v1 API Client & State
  // ============================================================================
  const state = {
    selectedCounterId: null,
    counters: [],
    activeDiscounts: [],
    ws: null,
    wsReconnectTimer: null,
    wsBackoffMs: 1500,
    qrCountdownTimers: new Map()
  };

  async function v1Fetch(path, options = {}) {
    const headers = {
      Accept: "application/json",
      ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...(window.aceCanteenToken ? { Authorization: `Bearer ${window.aceCanteenToken}` } : {}),
      ...(options.headers || {})
    };
    const res = await fetch(path, { ...options, headers, cache: "no-store" });
    const json = await res.json().catch(() => null);
    if (!res.ok || (json && json.success === false)) {
      const msg = json?.error?.message || json?.detail || `Request failed (${res.status})`;
      const err = new Error(msg);
      err.code = json?.error?.code || `HTTP_${res.status}`;
      throw err;
    }
    return json && "data" in json ? json.data : json;
  }

  // ============================================================================
  // 3. Real-Time WebSocket Hub Client (/api/v1/ws)
  // ============================================================================
  function setWsBadge(online, text) {
    const pill = $("#v1-ws-status");
    const label = $("#v1-ws-label");
    if (!pill || !label) return;
    pill.classList.toggle("offline", !online);
    label.textContent = text;
  }

  function connectRealtimeWs() {
    clearTimeout(state.wsReconnectTimer);
    if (state.ws) {
      try { state.ws.onclose = null; state.ws.close(); } catch { /* ignore */ }
      state.ws = null;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const tokenParam = window.aceCanteenToken ? `?token=${encodeURIComponent(window.aceCanteenToken)}` : "";
    const url = `${proto}//${location.host}/api/v1/ws${tokenParam}`;
    try {
      const ws = new WebSocket(url);
      state.ws = ws;
      ws.onopen = () => {
        state.wsBackoffMs = 1500;
        setWsBadge(true, "Live updates");
      };
      ws.onmessage = ev => {
        try {
          const msg = JSON.parse(ev.data);
          handleRealtimeEvent(msg);
        } catch { /* ignore malformed frame */ }
      };
      ws.onclose = () => {
        setWsBadge(false, "Reconnecting…");
        state.wsReconnectTimer = setTimeout(connectRealtimeWs, state.wsBackoffMs);
        state.wsBackoffMs = Math.min(state.wsBackoffMs * 1.8, 20000);
      };
      ws.onerror = () => {
        setWsBadge(false, "Offline");
      };
    } catch {
      setWsBadge(false, "Offline");
    }
  }

  function handleRealtimeEvent(msg) {
    const topic = String(msg?.topic || "");
    if (!topic || topic === "connected" || topic === "pong") return;
    if (topic.startsWith("order.") || topic.startsWith("pickup.")) {
      loadCounters();
      if (window.aceCanteenUser?.role === "student") loadStudentActiveOrders();
      if (["staff", "admin"].includes(window.aceCanteenUser?.role)) loadStaffKds();
      if (typeof window.loadPublicQueue === "function") window.loadPublicQueue();
    } else if (topic.startsWith("kitchen.") || topic.startsWith("inventory.")) {
      loadCounters();
      if (["staff", "admin"].includes(window.aceCanteenUser?.role)) {
        loadStaffKds();
        loadStaffIngredients();
      }
    }
  }

  // ============================================================================
  // 4. S01 — Counters, Active Offers & Personalized AI Recommendations
  // ============================================================================
  async function loadCounters() {
    const grid = $("#v1-counters-grid");
    if (!grid) return;
    try {
      const data = await v1Fetch("/api/v1/counters");
      const counters = Array.isArray(data) ? data : (data?.items || []);
      state.counters = counters;
      grid.replaceChildren();

      const filterSelect = $("#v1-filter-counter");
      const checkoutSelect = $("#v1-checkout-counter");
      const staffSelect = $("#v1-staff-counter-select");
      if (filterSelect) {
        const prev = filterSelect.value;
        filterSelect.replaceChildren(new Option("All counters", ""));
        counters.forEach(c => filterSelect.append(new Option(c.name, String(c.id))));
        filterSelect.value = prev;
      }
      if (checkoutSelect) {
        const prev = checkoutSelect.value;
        checkoutSelect.replaceChildren();
        counters.forEach(c => checkoutSelect.append(new Option(`${c.name} (~${c.estimatedWaitMinutes ?? 5}m wait)`, String(c.id))));
        if (prev) checkoutSelect.value = prev;
      }
      if (staffSelect) {
        const prev = staffSelect.value;
        staffSelect.replaceChildren(new Option("All assigned counters", ""));
        counters.forEach(c => staffSelect.append(new Option(`#${c.id} · ${c.name}`, String(c.id))));
        staffSelect.value = prev;
      }

      for (const c of counters) {
        const card = document.createElement("div");
        card.className = `v1-counter-card ${state.selectedCounterId === c.id ? "is-selected" : ""}`;
        const head = document.createElement("div");
        head.className = "v1-counter-head";
        const name = document.createElement("strong");
        name.textContent = c.name;
        const badge = document.createElement("span");
        badge.className = `badge-tag ${c.isOpen !== false && c.isActive !== false ? "badge-green" : "badge-red"}`;
        badge.textContent = c.isOpen !== false && c.isActive !== false ? "Open" : "Paused";
        head.append(name, badge);

        const meta = document.createElement("div");
        meta.className = "v1-counter-meta";
        meta.textContent = `Wait ~${c.estimatedWaitMinutes ?? 5} min · Active queue: ${c.activeOrders ?? 0}`;
        card.append(head, meta);
        card.addEventListener("click", () => {
          state.selectedCounterId = state.selectedCounterId === c.id ? null : c.id;
          if (filterSelect) filterSelect.value = state.selectedCounterId ? String(state.selectedCounterId) : "";
          if (checkoutSelect && state.selectedCounterId) checkoutSelect.value = String(state.selectedCounterId);
          loadCounters();
          applyV1MenuFilters();
          loadRecommendations();
        });
        grid.append(card);
      }
    } catch { /* silent fallback */ }
  }

  async function loadActiveOffers() {
    const strip = $("#v1-offers-strip");
    if (!strip) return;
    try {
      const res = await v1Fetch("/api/v1/discounts/active");
      const offers = Array.isArray(res) ? res : (res?.items || []);
      state.activeDiscounts = offers;
      strip.replaceChildren();
      if (!offers.length) {
        strip.hidden = true;
        return;
      }
      strip.hidden = false;
      for (const offer of offers) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "v1-offer-chip";
        const codeEl = document.createElement("code");
        codeEl.textContent = offer.code;
        const txt = document.createElement("span");
        txt.textContent = `${offer.title || offer.description || "Campus Offer"} (Min ${paise(offer.minOrderPaise || 0)})`;
        chip.append(codeEl, txt);
        chip.addEventListener("click", () => {
          const input = $("#v1-coupon-code");
          if (input) {
            input.value = offer.code;
            $("#cart-section")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
        });
        strip.append(chip);
      }
    } catch {
      strip.hidden = true;
    }
  }

  async function loadRecommendations() {
    const grid = $("#v1-reco-grid");
    if (!grid) return;
    try {
      const qs = new URLSearchParams({ limit: "4" });
      if (state.selectedCounterId) qs.set("counterId", String(state.selectedCounterId));
      const budgetVal = Number($("#v1-filter-budget")?.value || 0);
      if (budgetVal > 0) qs.set("maxPricePaise", String(Math.round(budgetVal * 100)));
      const data = await v1Fetch(`/api/v1/ai/recommendations?${qs}`);
      const items = data?.recommendations || data?.items || [];
      grid.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "management-message";
        empty.textContent = "Browse items below to unlock personalized campus picks.";
        grid.append(empty);
        return;
      }
      for (const rec of items) {
        const item = rec.item || rec;
        const card = document.createElement("div");
        card.className = "v1-reco-card";
        const reason = document.createElement("span");
        reason.className = "v1-reco-reason";
        reason.textContent = (rec.reasons && rec.reasons[0]) || "Popular on campus";
        const title = document.createElement("strong");
        title.textContent = item.name;
        const meta = document.createElement("small");
        meta.className = "muted";
        meta.textContent = `${paise(item.pricePaise)} · ~${item.preparationMinutes || 10}m prep`;
        const actions = document.createElement("div");
        actions.className = "v1-inline-actions";
        const inspectBtn = document.createElement("button");
        inspectBtn.type = "button";
        inspectBtn.className = "secondary-button";
        inspectBtn.textContent = "Details & Customize";
        inspectBtn.addEventListener("click", () => openItemDetailModal(item.id));
        actions.append(inspectBtn);
        card.append(reason, title, meta, actions);
        grid.append(card);
      }
    } catch { /* ignore */ }
  }

  // ============================================================================
  // 5. S02 — Menu Filtering + Verified Item Details & Customizations Modal
  // ============================================================================
  async function applyV1MenuFilters() {
    const grid = $("#catalogue-grid");
    const msg = $("#catalogue-message");
    if (!grid || !msg) return;
    const qs = new URLSearchParams({ limit: "40" });
    const search = $("#product-search")?.value.trim();
    if (search) qs.set("search", search);
    const cat = $("#product-category")?.value;
    if (cat) qs.set("categoryId", cat);
    const counterId = $("#v1-filter-counter")?.value;
    if (counterId) qs.set("counterId", counterId);
    const diet = $("#v1-filter-diet")?.value;
    if (diet) qs.set("dietaryTag", diet);
    const budget = Number($("#v1-filter-budget")?.value || 0);
    if (budget > 0) qs.set("maxPricePaise", String(Math.round(budget * 100)));
    const excluded = $$(".v1-exclude-allergen:checked").map(el => el.value);
    if (excluded.length) qs.set("excludeAllergens", excluded.join(","));

    msg.textContent = "Filtering verified menu…";
    try {
      const data = await v1Fetch(`/api/v1/menu/items?${qs}`);
      const items = Array.isArray(data) ? data : (data?.items || []);
      grid.replaceChildren();
      if (!items.length) {
        msg.textContent = "No available items match your dietary, allergen or budget filters.";
        return;
      }
      window.aceCatalogueProducts = window.aceCatalogueProducts || {};
      for (const item of items) {
        const legacyShape = {
          id: item.id,
          name: item.name,
          price: (item.pricePaise / 100).toFixed(2),
          stock_quantity: item.stockQuantity,
          item_type: item.itemType,
          category_name: item.categoryName
        };
        window.aceCatalogueProducts[item.id] = legacyShape;
        const card = document.createElement("article");
        card.className = "product-card";
        const title = document.createElement("h3");
        title.textContent = item.name;
        const desc = document.createElement("p");
        desc.textContent = item.description || "Freshly prepared campus item";
        const meta = document.createElement("div");
        meta.className = "product-meta";
        const price = document.createElement("span");
        price.className = "product-price";
        price.textContent = paise(item.pricePaise);
        const stock = document.createElement("span");
        stock.className = "stock-label";
        stock.textContent = `${item.stockQuantity} left · ~${item.preparationMinutes || 10}m`;
        meta.append(price, stock);

        const tags = document.createElement("p");
        tags.className = "product-extra";
        const dietStr = (item.dietaryTags || []).join(", ") || "Standard";
        const algStr = (item.allergenTags || []).length ? `Allergens: ${item.allergenTags.join(", ")}` : "Allergens: unverified / none listed";
        tags.textContent = `${dietStr} · ${algStr}`;

        const actions = document.createElement("div");
        actions.className = "product-actions";
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "primary-button product-order-button";
        addBtn.textContent = "Add to cart";
        addBtn.addEventListener("click", () => addItemV1(legacyShape, 1, []));

        const detailBtn = document.createElement("button");
        detailBtn.type = "button";
        detailBtn.className = "secondary-button";
        detailBtn.textContent = "Customize";
        detailBtn.addEventListener("click", () => openItemDetailModal(item.id));
        actions.append(addBtn, detailBtn);

        card.append(title, desc, meta, tags, actions);
        grid.append(card);
      }
      msg.textContent = `${items.length} verified menu item(s) matched.`;
    } catch (err) {
      msg.textContent = err.message;
    }
  }

  async function openItemDetailModal(itemId) {
    const modal = $("#v1-item-modal");
    const body = $("#v1-modal-body");
    const title = $("#v1-modal-title");
    const cat = $("#v1-modal-category");
    if (!modal || !body) return;
    modal.hidden = false;
    body.textContent = "Loading verified nutrition, allergen & recipe data…";
    try {
      const item = await v1Fetch(`/api/v1/menu/items/${itemId}`);
      title.textContent = item.name;
      cat.textContent = `${(item.categoryName || item.itemType || "MENU").toUpperCase()} · ~${item.preparationMinutes || 10} MIN PREP`;
      body.replaceChildren();

      const desc = document.createElement("p");
      desc.textContent = item.description || "Prepared fresh at the assigned campus counter.";
      const priceRow = document.createElement("strong");
      priceRow.textContent = `${paise(item.pricePaise)} · ${item.stockQuantity} in stock`;

      const ingBox = document.createElement("div");
      ingBox.className = "v1-mini-card";
      const ingTitle = document.createElement("h4");
      ingTitle.textContent = "Verified Ingredients & Allergen Information";
      const ingText = document.createElement("small");
      const recipeNames = (item.recipeIngredients || []).map(r => r.name).join(", ");
      ingText.textContent = `Ingredients: ${recipeNames || item.ingredientsSummary || "Not specified"} | Dietary: ${(item.dietaryTags || []).join(", ") || "None"} | Allergens: ${(item.allergenTags || []).join(", ") || "Unverified — ask counter staff if severely allergic"}`;
      ingBox.append(ingTitle, ingText);

      const custChecks = [];
      const custWrap = document.createElement("div");
      custWrap.className = "v1-mini-card";
      const options = Array.isArray(item.customizations) ? item.customizations : [];
      if (options.length) {
        const cTitle = document.createElement("h4");
        cTitle.textContent = "Optional Customizations";
        custWrap.append(cTitle);
        for (const opt of options) {
          const lbl = document.createElement("label");
          lbl.className = "v1-pill-check";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = opt.name || opt.id || "addon";
          custChecks.push({ cb, opt });
          lbl.append(cb, document.createTextNode(` ${opt.name} (+${paise(opt.priceDeltaPaise || 0)})`));
          custWrap.append(lbl);
        }
      }

      const qtyLabel = document.createElement("label");
      qtyLabel.textContent = "Quantity ";
      const qtyInput = document.createElement("input");
      qtyInput.type = "number";
      qtyInput.min = "1";
      qtyInput.max = "20";
      qtyInput.value = "1";
      qtyLabel.append(qtyInput);

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "primary-button";
      confirmBtn.textContent = "Confirm & Add to Cart";
      confirmBtn.addEventListener("click", async () => {
        const chosen = {};
        for (const x of custChecks) {
          if (x.cb.checked) chosen[x.opt.name || "addon"] = true;
        }
        const qty = Math.max(1, Math.min(20, Number(qtyInput.value || 1)));
        await addItemV1({
          id: item.id,
          name: item.name,
          price: (item.pricePaise / 100).toFixed(2),
          stock_quantity: item.stockQuantity
        }, qty, chosen);
        modal.hidden = true;
      });

      body.append(desc, priceRow, ingBox);
      if (options.length) body.append(custWrap);
      body.append(qtyLabel, confirmBtn);
    } catch (err) {
      body.textContent = err.message;
    }
  }

  async function addItemV1(product, qty = 1, customizations = {}) {
    if (typeof window.addToCart === "function") {
      for (let i = 0; i < qty; i++) window.addToCart(product);
    }
    if (window.aceCanteenToken && window.aceCanteenUser?.role === "student") {
      try {
        await v1Fetch(`/api/v1/cart/items/${product.id}`, {
          method: "PUT",
          body: JSON.stringify({ quantity: qty, customizations })
        });
      } catch { /* local cart fallback already updated */ }
    }
  }

  // ============================================================================
  // 6. S03 — Coupon, Counter & Tax Authoritative Quote
  // ============================================================================
  async function quoteCartV1() {
    const breakdown = $("#v1-quote-breakdown");
    const msg = $("#cart-message");
    if (!window.aceCanteenToken || window.aceCanteenUser?.role !== "student") {
      if (msg) msg.textContent = "Sign in with a student account to quote discounts and taxes.";
      return null;
    }
    let rawCart = {};
    try { rawCart = JSON.parse(localStorage.getItem("ace-canteen-cart-v1") || "{}"); } catch { rawCart = {}; }
    const items = Object.entries(rawCart)
      .map(([id, q]) => ({ menuItemId: Number(id), quantity: Number(q), customizations: {} }))
      .filter(x => x.menuItemId > 0 && x.quantity > 0);
    if (!items.length) {
      if (breakdown) breakdown.hidden = true;
      return null;
    }
    const counterId = Number($("#v1-checkout-counter")?.value || 0) || null;
    const couponCode = $("#v1-coupon-code")?.value.trim() || null;
    const pickupSlot = $("#pickup-slot")?.value || null;
    try {
      const q = await v1Fetch("/api/v1/orders/quote", {
        method: "POST",
        body: JSON.stringify({ items, counterId, couponCode, pickupSlot })
      });
      if (breakdown) {
        breakdown.hidden = false;
        $("#v1-q-subtotal").textContent = paise(q.subtotalPaise);
        $("#v1-q-discount").textContent = `−${paise(q.discountPaise || 0)}`;
        $("#v1-q-tax").textContent = paise(q.taxPaise || 0);
        $("#v1-q-total").textContent = paise(q.totalPaise);
      }
      if (msg) {
        msg.textContent = couponCode && q.discountPaise > 0
          ? `Offer ${couponCode.toUpperCase()} applied! Payable total: ${paise(q.totalPaise)}.`
          : `Verified quote ready: ${paise(q.totalPaise)} (includes taxes).`;
      }
      return q;
    } catch (err) {
      if (msg) msg.textContent = err.message;
      return null;
    }
  }

  // ============================================================================
  // 7. S04 — Live Order Timeline, Short-Lived QR & Manual Fallback Code
  // ============================================================================
  async function loadStudentActiveOrders() {
    const panel = $("#v1-active-orders-panel");
    if (!panel || !window.aceCanteenToken || window.aceCanteenUser?.role !== "student") return;
    try {
      const res = await v1Fetch("/api/v1/users/me/orders?limit=10");
      const orders = Array.isArray(res) ? res : (res?.items || []);
      panel.replaceChildren();
      const active = orders.filter(o => !["completed", "collected", "cancelled", "refunded", "refund_pending"].includes(String(o.status).toLowerCase()));
      for (const o of active) {
        const payLower = String(o.paymentStatus || "").toLowerCase();
        const statusNorm = String(o.rawStatus || o.status || "").toLowerCase().replace("pending_payment", "pending");
        const card = document.createElement("div");
        card.className = "v1-order-card";
        const head = document.createElement("div");
        head.className = "management-list-head";
        const title = document.createElement("strong");
        title.textContent = `Order #${o.id} · ${o.tokenDisplay || ("Token #" + (o.tokenNumber ?? "—"))} · ${paise(o.totalPaise ?? o.subtotalPaise)}`;
        const payBadge = document.createElement("span");
        payBadge.className = `badge-tag ${payLower === "paid" ? "badge-green" : "badge-amber"}`;
        payBadge.textContent = `Payment: ${String(o.paymentStatus).toUpperCase()}`;
        head.append(title, payBadge);

        const steps = ["pending", "confirmed", "accepted", "preparing", "ready", "collected"];
        const curIdx = steps.indexOf(statusNorm);
        const timeline = document.createElement("div");
        timeline.className = "v1-timeline";
        for (let i = 0; i < steps.length; i++) {
          const st = document.createElement("span");
          st.className = `v1-step ${i < curIdx ? "done" : i === curIdx ? "active" : ""}`;
          st.textContent = steps[i].toUpperCase();
          timeline.append(st);
        }

        const actions = document.createElement("div");
        actions.className = "v1-inline-actions";
        const qrContainer = document.createElement("div");

        if (payLower === "paid") {
          const qrBtn = document.createElement("button");
          qrBtn.type = "button";
          qrBtn.className = "primary-button";
          qrBtn.textContent = "Show Short-Lived Pickup QR";
          qrBtn.addEventListener("click", () => mintAndRenderPickupQr(o.id, qrContainer));
          actions.append(qrBtn);

          const receiptBtn = document.createElement("button");
          receiptBtn.type = "button";
          receiptBtn.className = "secondary-button";
          receiptBtn.textContent = "Download Receipt (PDF)";
          receiptBtn.addEventListener("click", () => downloadDigitalReceiptPdf(o.id));
          actions.append(receiptBtn);
        }
        if (["pending", "confirmed"].includes(statusNorm)) {
          const cancelBtn = document.createElement("button");
          cancelBtn.type = "button";
          cancelBtn.className = "cancel-order-button";
          cancelBtn.textContent = "Cancel order";
          cancelBtn.addEventListener("click", async () => {
            if (!window.confirm(`Cancel Order #${o.id}?`)) return;
            cancelBtn.disabled = true;
            try {
              await v1Fetch(`/api/v1/orders/${o.id}/cancel`, {
                method: "POST",
                body: JSON.stringify({ reason: "Cancelled by student before preparation" })
              });
              await loadStudentActiveOrders();
              if (typeof window.loadMyOrders === "function") window.loadMyOrders();
            } catch (err) {
              alert(err.message);
              cancelBtn.disabled = false;
            }
          });
          actions.append(cancelBtn);
        }

        card.append(head, timeline, actions, qrContainer);
        panel.append(card);
      }

      const completedPaid = orders.filter(o =>
        ["completed", "collected"].includes(String(o.status).toLowerCase()) &&
        String(o.paymentStatus || "").toLowerCase() === "paid"
      );
      for (const o of completedPaid.slice(0, 4)) {
        const recCard = document.createElement("div");
        recCard.className = "v1-order-card";
        const head = document.createElement("div");
        head.className = "management-list-head";
        const title = document.createElement("strong");
        title.textContent = `Completed Order #${o.id} · ${paise(o.totalPaise ?? o.subtotalPaise)}`;
        const receiptBtn = document.createElement("button");
        receiptBtn.type = "button";
        receiptBtn.className = "secondary-button";
        receiptBtn.textContent = "Download Digital Receipt (PDF)";
        receiptBtn.addEventListener("click", () => downloadDigitalReceiptPdf(o.id));
        head.append(title, receiptBtn);
        recCard.append(head);
        panel.append(recCard);
      }
    } catch { /* ignore */ }
  }

  async function downloadDigitalReceiptPdf(orderId) {
    try {
      const res = await fetch(`/api/v1/orders/${orderId}/receipt?format=pdf`, {
        method: "GET",
        headers: window.aceCanteenToken ? { Authorization: `Bearer ${window.aceCanteenToken}` } : {}
      });
      if (!res.ok) throw new Error(`Could not download receipt (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `collegebits-receipt-order-${orderId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (err) {
      alert(err.message);
    }
  }

  async function mintAndRenderPickupQr(orderId, container) {
    container.textContent = "Generating encrypted short-lived pickup token…";
    try {
      const tok = await v1Fetch(`/api/v1/orders/${orderId}/pickup-token`, { method: "POST" });
      container.replaceChildren();
      const wrap = document.createElement("div");
      wrap.className = "v1-qr-panel";

      const tokBadge = document.createElement("div");
      tokBadge.className = "tok-badge-center";
      const tokNum = document.createElement("div");
      tokNum.className = "tok-num-green";
      tokNum.textContent = tok.tokenDisplay || ("Token #" + tok.tokenNumber);
      const tokMeta = document.createElement("div");
      tokMeta.className = "muted tok-meta-sub";
      tokMeta.textContent = `Assigned: ${tok.counterName || "Main Counter"} · Order ${tok.orderNumber}`;
      tokBadge.append(tokNum, tokMeta);
      wrap.append(tokBadge);

      const svgBox = document.createElement("div");
      svgBox.className = "v1-qr-svg-wrap";
      svgBox.append(renderQrSvg(tok.pickupToken || tok.qrUrl));

      const info = document.createElement("div");
      const manualTitle = document.createElement("div");
      manualTitle.textContent = "Manual fallback code (single-use):";
      const manual = document.createElement("div");
      manual.className = "v1-manual-code";
      manual.textContent = tok.manualPickupCode || tok.manualCode || "—";
      const expNote = document.createElement("small");
      expNote.className = "muted";
      const expTime = new Date(tok.expiresAt).getTime();
      const updateCountdown = () => {
        const sec = Math.max(0, Math.round((expTime - Date.now()) / 1000));
        expNote.textContent = sec > 0
          ? `Expires in ${Math.floor(sec / 60)}m ${sec % 60}s · Single-use verified handover`
          : "QR expired — click Show Short-Lived Pickup QR to refresh.";
      };
      updateCountdown();
      if (state.qrCountdownTimers.has(orderId)) clearInterval(state.qrCountdownTimers.get(orderId));
      state.qrCountdownTimers.set(orderId, setInterval(updateCountdown, 1000));

      info.append(manualTitle, manual, document.createElement("br"), expNote);
      wrap.append(svgBox, info);
      container.append(wrap);
    } catch (err) {
      container.textContent = err.message;
    }
  }

  // ============================================================================
  // 8. S05 — Grounded AI Menu Concierge Drawer
  // ============================================================================
  function toggleAiDrawer(open) {
    const drawer = $("#v1-ai-drawer");
    if (!drawer) return;
    drawer.hidden = open === undefined ? !drawer.hidden : !open;
    if (!drawer.hidden) $("#v1-ai-input")?.focus();
  }

  async function askAiAssistant(queryText) {
    const log = $("#v1-ai-messages");
    if (!log || !queryText.trim()) return;
    if (!window.aceCanteenToken) {
      const warn = document.createElement("div");
      warn.className = "v1-chat-msg assistant";
      warn.textContent = "Please sign in first so I can check live counter availability and your dietary preferences.";
      log.append(warn);
      return;
    }
    const userBubble = document.createElement("div");
    userBubble.className = "v1-chat-msg user";
    userBubble.textContent = queryText;
    const botBubble = document.createElement("div");
    botBubble.className = "v1-chat-msg assistant";
    botBubble.textContent = "Checking live menu & stock…";
    log.append(userBubble, botBubble);
    log.scrollTop = log.scrollHeight;

    try {
      const res = await v1Fetch("/api/v1/ai/assistant", {
        method: "POST",
        body: JSON.stringify({
          message: queryText,
          counterId: state.selectedCounterId || null
        })
      });
      botBubble.replaceChildren();
      const ans = document.createElement("div");
      ans.textContent = res.answer || "Here are verified options from the current menu:";
      botBubble.append(ans);

      for (const s of res.suggestedItems || res.suggestions || []) {
        const box = document.createElement("div");
        box.className = "v1-ai-suggestion";
        const top = document.createElement("strong");
        top.textContent = `${s.name} · ${paise(s.pricePaise)}`;
        const reason = document.createElement("small");
        reason.textContent = `${s.allergenNotice || "Available now"} · ~${s.preparationMinutes || 10}m prep`;
        box.append(top, reason);

        if (!s.allergenVerified) {
          const warn = document.createElement("small");
          warn.className = "error";
          warn.textContent = `⚠️ ${s.allergenNotice || "Allergen info unverified — confirm with staff."}`;
          box.append(warn);
        }
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "secondary-button";
        addBtn.textContent = "Confirm & Add to Cart";
        addBtn.addEventListener("click", async () => {
          await addItemV1({
            id: s.id || s.menuItemId,
            name: s.name,
            price: (s.pricePaise / 100).toFixed(2),
            stock_quantity: s.stockQuantity || 10
          }, 1, {});
          addBtn.textContent = "Added to cart ✓";
          addBtn.disabled = true;
        });
        box.append(addBtn);
        botBubble.append(box);
      }
      log.scrollTop = log.scrollHeight;
    } catch (err) {
      botBubble.textContent = err.message;
    }
  }

  // ============================================================================
  // 9. Profile, Email Verification, Web Push & TOTP 2FA/MFA
  // ============================================================================
  async function loadUserProfileV1() {
    if (!window.aceCanteenToken) return;
    try {
      const me = await v1Fetch("/api/v1/users/me");
      if ($("#v1-profile-phone")) $("#v1-profile-phone").value = me.phone || "";
      if ($("#v1-profile-diet")) $("#v1-profile-diet").value = me.dietaryPreference || "";
      if ($("#v1-profile-allergens")) $("#v1-profile-allergens").value = (me.allergenPreferences || []).join(", ");
      const mfaBtn = $("#v1-mfa-setup-btn");
      if (mfaBtn) {
        mfaBtn.hidden = !["admin", "staff"].includes(me.role);
        mfaBtn.textContent = me.mfaEnabled ? "2FA Enabled (Manage)" : "Setup 2FA / MFA";
      }
      const benefits = $("#rewards-wallet-summary");
      const rewardsPanel = $("#rewards-wallet-panel");
      if (benefits && rewardsPanel) {
        rewardsPanel.hidden = me.role !== "student";
        if (me.role === "student") {
          const account = await v1Fetch("/api/v1/rewards/me");
          benefits.textContent =
            `${account.pointsBalance} points · Wallet ${paise(account.walletBalancePaise)}. ` +
            "Wallet top-ups are issued by the canteen after verified payment.";
          const rewardInput = $("#checkout-reward-points");
          if (rewardInput) rewardInput.max = String(account.pointsBalance);
        }
      }
    } catch { /* ignore */ }
  }

  async function saveUserProfileV1() {
    const msg = $("#v1-profile-msg");
    if (!msg) return;
    msg.textContent = "Saving profile…";
    try {
      const allergens = ($("#v1-profile-allergens")?.value || "")
        .split(",")
        .map(s => s.trim().toLowerCase())
        .filter(Boolean);
      await v1Fetch("/api/v1/users/me", {
        method: "PATCH",
        body: JSON.stringify({
          phone: $("#v1-profile-phone")?.value.trim() || null,
          dietaryPreference: $("#v1-profile-diet")?.value || null,
          allergenPreferences: allergens
        })
      });
      msg.textContent = "Profile & dietary preferences saved.";
      msg.className = "management-message success";
      loadRecommendations();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    return Uint8Array.from([...raw].map(ch => ch.charCodeAt(0)));
  }

  async function toggleWebPush() {
    const msg = $("#v1-profile-msg");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      if (msg) msg.textContent = "Web Push is not supported in this browser.";
      return;
    }
    try {
      const keyData = await v1Fetch("/api/v1/push/vapid-public-key");
      if (!keyData?.publicKey) {
        if (msg) msg.textContent = "Push VAPID key is not configured on this server yet; in-app WebSocket notifications remain active.";
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        if (msg) msg.textContent = "Notification permission was not granted.";
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyData.publicKey)
      });
      const subJson = sub.toJSON();
      await v1Fetch("/api/v1/push/subscribe", {
        method: "POST",
        body: JSON.stringify({
          endpoint: subJson.endpoint,
          keys: {
            p256dh: subJson.keys?.p256dh || "",
            auth: subJson.keys?.auth || ""
          }
        })
      });
      if (msg) {
        msg.textContent = "Push notifications enabled for ready-to-collect alerts!";
        msg.className = "management-message success";
      }
    } catch (err) {
      if (msg) msg.textContent = err.message;
    }
  }

  async function openMfaSetupModal() {
    const modal = $("#v1-item-modal");
    const title = $("#v1-modal-title");
    const cat = $("#v1-modal-category");
    const body = $("#v1-modal-body");
    if (!modal || !body) return;
    modal.hidden = false;
    cat.textContent = "ACCOUNT SECURITY";
    title.textContent = "Two-Factor Authentication (TOTP)";
    body.textContent = "Initializing encrypted TOTP secret…";
    try {
      const setup = await v1Fetch("/api/v1/auth/mfa/setup", { method: "POST" });
      body.replaceChildren();
      const qrBox = document.createElement("div");
      qrBox.className = "v1-qr-svg-wrap";
      qrBox.append(renderQrSvg(setup.otpauthUri));
      const secretText = document.createElement("small");
      secretText.textContent = `Manual key: ${setup.secret}`;
      const codeInput = document.createElement("input");
      codeInput.placeholder = "Enter 6-digit authenticator code";
      codeInput.maxLength = 8;
      const enableBtn = document.createElement("button");
      enableBtn.type = "button";
      enableBtn.className = "primary-button";
      enableBtn.textContent = "Verify & Enable 2FA";
      const status = document.createElement("p");
      status.className = "management-message";
      enableBtn.addEventListener("click", async () => {
        try {
          const res = await v1Fetch("/api/v1/auth/mfa/enable", {
            method: "POST",
            body: JSON.stringify({ code: codeInput.value.trim() })
          });
          status.textContent = `2FA enabled! Save recovery codes: ${(res.recoveryCodes || []).join(", ")}`;
          status.className = "management-message success";
          loadUserProfileV1();
        } catch (err) {
          status.textContent = err.message;
          status.className = "management-message error";
        }
      });
      body.append(qrBox, secretText, codeInput, enableBtn, status);
    } catch (err) {
      body.textContent = err.message;
    }
  }

  // ============================================================================
  // 10. T01–T04 Counter-Scoped Staff Kitchen Display, Pickup & Ingredients
  // ============================================================================
  async function loadStaffKds() {
    const grid = $("#v1-kds-grid");
    const msg = $("#v1-kds-message");
    if (!grid || !msg || !["staff", "admin"].includes(window.aceCanteenUser?.role)) return;
    const counterId = $("#v1-staff-counter-select")?.value;
    const qs = new URLSearchParams();
    if (counterId) qs.set("counterId", counterId);
    try {
      const res = await v1Fetch(`/api/v1/staff/orders?${qs}`);
      const orders = Array.isArray(res) ? res : (res?.items || []);
      grid.replaceChildren();
      const active = orders.filter(o => ["pending", "confirmed", "preparing", "ready"].includes(String(o.status).toLowerCase()));
      msg.textContent = active.length
        ? `${active.length} live paid ticket(s) in kitchen/handover queue.`
        : "No active paid tickets waiting right now.";
      for (const o of active) {
        const ticket = document.createElement("div");
        ticket.className = `v1-kds-ticket ${o.isOverdue ? "is-overdue" : ""}`;
        const head = document.createElement("div");
        head.className = "management-list-head";
        const strong = document.createElement("strong");
        strong.textContent = `${o.tokenDisplay || ("Token #" + (o.tokenNumber ?? o.id))} · ${String(o.status).toUpperCase()}`;
        const sla = document.createElement("span");
        sla.className = `badge-tag ${o.isOverdue ? "badge-red" : "badge-green"}`;
        sla.textContent = o.isOverdue ? `OVERDUE (${o.elapsedMinutes || 0}m)` : `${o.elapsedMinutes || 0}m elapsed`;
        head.append(strong, sla);

        const itemsList = document.createElement("div");
        itemsList.textContent = (o.items || []).map(i => {
          const cust = (i.customizations || []).map(c => c.name).join("+");
          return `${i.name || i.productName} × ${i.quantity}${cust ? ` [${cust}]` : ""}`;
        }).join(" · ");

        const act = document.createElement("div");
        act.className = "v1-kds-actions";
        const addAction = (label, endpoint, body = {}) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "secondary-button";
          b.textContent = label;
          b.addEventListener("click", async () => {
            b.disabled = true;
            try {
              await v1Fetch(`/api/v1/staff/orders/${o.id}/${endpoint}`, {
                method: "POST",
                body: JSON.stringify(body)
              });
              await loadStaffKds();
              if (typeof window.loadStaffWorkflow === "function") window.loadStaffWorkflow();
            } catch (err) {
              alert(err.message);
              b.disabled = false;
            }
          });
          act.append(b);
        };

        const st = String(o.status).toLowerCase();
        if (st === "pending") addAction("Accept Ticket", "accept");
        if (st === "confirmed") addAction("Start Prep", "start");
        if (st === "preparing") addAction("Mark Ready", "ready");
        if (["pending", "confirmed", "preparing"].includes(st)) {
          const exBtn = document.createElement("button");
          exBtn.type = "button";
          exBtn.className = "cancel-order-button";
          exBtn.textContent = "Exception / Refund";
          exBtn.addEventListener("click", async () => {
            const reason = window.prompt("Kitchen exception reason (e.g. ingredient stockout):", "Item unavailable in kitchen");
            if (!reason) return;
            try {
              await v1Fetch(`/api/v1/staff/orders/${o.id}/exception`, {
                method: "POST",
                body: JSON.stringify({ reason, cancelAndRefund: true })
              });
              await loadStaffKds();
            } catch (err) {
              alert(err.message);
            }
          });
          act.append(exBtn);
        }

        ticket.append(head, itemsList, act);
        grid.append(ticket);
      }
    } catch (err) {
      msg.textContent = err.message;
    }
  }

  async function loadStaffIngredients() {
    const sel = $("#v1-staff-ingredient-select");
    if (!sel || !["staff", "admin"].includes(window.aceCanteenUser?.role)) return;
    try {
      const data = await v1Fetch("/api/v1/staff/inventory");
      const ingredients = data?.ingredients || [];
      sel.replaceChildren();
      if (!ingredients.length) {
        sel.append(new Option("No raw ingredients configured yet", ""));
        return;
      }
      for (const ing of ingredients) {
        sel.append(new Option(`${ing.name} (${ing.currentQuantity} ${ing.unit})`, String(ing.id)));
      }
    } catch { /* ignore */ }
  }

  // ============================================================================
  // 11. A01–A08 Enterprise Admin Hub & AI Demand Forecasting
  // ============================================================================
  async function loadAdminAiHub() {
    if (window.aceCanteenUser?.role !== "admin") return;
    const tbody = $("#v1-forecast-tbody");
    const badge = $("#v1-ai-backtest-badge");
    const wasteList = $("#v1-waste-insights-list");
    try {
      const [forecasts, waste] = await Promise.all([
        v1Fetch("/api/v1/ai/demand-forecasts"),
        v1Fetch("/api/v1/ai/insights/waste")
      ]);
      if (badge) {
        const mae = forecasts?.metrics?.mae;
        const wape = forecasts?.metrics?.wapePct;
        badge.textContent = mae != null
          ? `Measured Backtest MAE: ${mae} · WAPE: ${wape ?? "—"}%`
          : "Backtest: awaiting sufficient historical days";
      }
      if (tbody) {
        tbody.replaceChildren();
        const rows = forecasts?.items || [];
        for (const f of rows) {
          const tr = document.createElement("tr");
          const tdName = document.createElement("td");
          tdName.textContent = f.itemName || `Item #${f.menuItemId}`;
          const tdDate = document.createElement("td");
          tdDate.textContent = `${forecasts?.forecastDate || "Today"} · ${f.timeSlot || ""}`;
          const tdPred = document.createElement("td");
          tdPred.textContent = String(f.effectivePrepPlan ?? f.predictedQuantity);
          const tdRange = document.createElement("td");
          tdRange.textContent = `[${f.lowerBound} – ${f.upperBound}]`;
          const tdConf = document.createElement("td");
          tdConf.textContent = f.lowConfidenceWarning ? "Low sample warning" : `${Math.round((f.confidenceScore || 0.7) * 100)}%`;
          const tdAct = document.createElement("td");
          const ovBtn = document.createElement("button");
          ovBtn.type = "button";
          ovBtn.className = "secondary-button";
          ovBtn.textContent = "Override";
          ovBtn.addEventListener("click", async () => {
            const nextVal = window.prompt(`Override prep target for ${tdName.textContent}:`, String(f.effectivePrepPlan ?? f.predictedQuantity));
            if (nextVal === null) return;
            const reason = window.prompt("Reason for override (e.g. exam day / campus event):", "Campus event adjustment") || "Staff adjustment";
            try {
              await v1Fetch(`/api/v1/ai/forecasts/items/${f.id}/override`, {
                method: "PATCH",
                body: JSON.stringify({ approvedQuantity: Number(nextVal), reason })
              });
              await loadAdminAiHub();
            } catch (err) {
              alert(err.message);
            }
          });
          tdAct.append(ovBtn);
          tr.append(tdName, tdDate, tdPred, tdRange, tdConf, tdAct);
          tbody.append(tr);
        }
      }
      if (wasteList) {
        wasteList.replaceChildren();
        const alerts = [
          ...(waste?.predictedIngredientShortages || []).map(s => `Predicted shortage: ${s.ingredientName} (need ${s.projectedNeed} ${s.unit}, have ${s.currentStock})`),
          ...(waste?.wastageHotspots || []).map(w => `Wastage hotspot: ${w.name} (${w.wastedQuantity} wasted)`),
          ...(waste?.expiringBatches || []).map(b => `Expiring batch ${b.batchCode}: ${b.productName} (${b.quantityRemaining} left, exp ${b.expiryDate})`)
        ];
        if (!alerts.length) {
          const okItem = document.createElement("div");
          okItem.className = "notification-item";
          okItem.textContent = "No predicted ingredient shortages or acute wastage anomalies detected.";
          wasteList.append(okItem);
        } else {
          for (const text of alerts) {
            const el = document.createElement("div");
            el.className = "notification-item is-unread";
            el.textContent = text;
            wasteList.append(el);
          }
        }
      }
    } catch { /* ignore */ }
  }

  // ============================================================================
  // 12. Wire DOM Events & Auth Lifecycle
  // ============================================================================
  $("#v1-refresh-reco")?.addEventListener("click", loadRecommendations);
  $("#v1-apply-filters")?.addEventListener("click", applyV1MenuFilters);
  $("#v1-apply-coupon")?.addEventListener("click", quoteCartV1);
  $("#cart-review")?.addEventListener("click", quoteCartV1);
  $("#v1-modal-close")?.addEventListener("click", () => { $("#v1-item-modal").hidden = true; });
  $("#v1-item-modal")?.addEventListener("click", e => { if (e.target.id === "v1-item-modal") e.currentTarget.hidden = true; });

  $("#v1-ai-launcher")?.addEventListener("click", () => toggleAiDrawer());
  $("#v1-hero-ai-btn")?.addEventListener("click", () => toggleAiDrawer(true));
  $("#v1-ai-close")?.addEventListener("click", () => toggleAiDrawer(false));
  $$("[data-ai-prompt]").forEach(chip => chip.addEventListener("click", () => askAiAssistant(chip.dataset.aiPrompt)));
  $("#v1-ai-form")?.addEventListener("submit", e => {
    e.preventDefault();
    const input = $("#v1-ai-input");
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    askAiAssistant(q);
  });

  $("#v1-save-profile-btn")?.addEventListener("click", saveUserProfileV1);
  $("#v1-push-toggle-btn")?.addEventListener("click", toggleWebPush);
  $("#v1-mfa-setup-btn")?.addEventListener("click", openMfaSetupModal);
  $("#v1-verify-email-btn")?.addEventListener("click", async () => {
    const msg = $("#v1-profile-msg");
    try {
      const res = await v1Fetch("/api/v1/auth/send-verification", { method: "POST" });
      if (msg) {
        msg.textContent = res.message || "Verification instructions sent.";
        msg.className = "management-message success";
      }
    } catch (err) {
      if (msg) msg.textContent = err.message;
    }
  });

  $("#v1-refresh-kds")?.addEventListener("click", loadStaffKds);
  $("#v1-staff-counter-select")?.addEventListener("change", loadStaffKds);

  $("#v1-pickup-verify-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-verify-msg");
    msg.textContent = "Verifying single-use pickup credential…";
    try {
      const raw = $("#v1-verify-token").value.trim();
      const orderIdVal = Number($("#v1-verify-order-id")?.value || 0);
      const body = raw.startsWith("pk_") || raw.length > 10
        ? { pickupToken: raw }
        : { orderId: orderIdVal, manualCode: raw.toUpperCase() };
      const res = await v1Fetch("/api/v1/pickups/verify", {
        method: "POST",
        body: JSON.stringify(body)
      });
      const colOrder = res.order || res;
      msg.textContent = `Handover verified for Order #${colOrder.id || colOrder.orderId}.`;
      msg.className = "management-message success";
      e.target.reset();
      loadStaffKds();
      if (typeof window.loadStaffWorkflow === "function") window.loadStaffWorkflow();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-ingredient-move-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-staff-move-msg");
    const ingId = Number($("#v1-staff-ingredient-select")?.value || 0);
    if (!ingId) {
      msg.textContent = "Create an ingredient in Admin first.";
      return;
    }
    try {
      await v1Fetch("/api/v1/staff/inventory/movements", {
        method: "POST",
        body: JSON.stringify({
          ingredientId: ingId,
          movementType: $("#v1-staff-move-type").value,
          quantity: Number($("#v1-staff-move-qty").value),
          reason: $("#v1-staff-move-reason").value.trim()
        })
      });
      msg.textContent = "Ingredient stock movement recorded.";
      msg.className = "management-message success";
      loadStaffIngredients();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-counter-create-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-counter-msg");
    try {
      await v1Fetch("/api/v1/admin/counters", {
        method: "POST",
        body: JSON.stringify({
          name: $("#v1-counter-name").value.trim(),
          location: $("#v1-counter-code").value.trim(),
          capacityPerSlot: Number($("#v1-counter-cap").value),
          isActive: true
        })
      });
      msg.textContent = "Counter created.";
      msg.className = "management-message success";
      e.target.reset();
      loadCounters();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-staff-assign-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-assign-msg");
    try {
      const uid = Number($("#v1-assign-user-id").value);
      const ids = $("#v1-assign-counter-ids").value.split(",").map(s => Number(s.trim())).filter(n => n > 0);
      await v1Fetch(`/api/v1/admin/users/${uid}/counters`, {
        method: "PUT",
        body: JSON.stringify({ counterIds: ids })
      });
      msg.textContent = `Assigned counters [${ids.join(", ")}] to user #${uid}.`;
      msg.className = "management-message success";
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-upload-photo-btn")?.addEventListener("click", async () => {
    const msg = $("#v1-assign-msg");
    const itemId = Number($("#v1-upload-item-id")?.value || 0);
    const file = $("#v1-upload-file")?.files?.[0];
    if (!itemId || !file) {
      msg.textContent = "Choose a valid Menu Item ID and PNG/JPEG/WEBP file.";
      return;
    }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await v1Fetch(`/api/v1/admin/menu-items/${itemId}/image`, {
          method: "POST",
          body: JSON.stringify({ imageBase64: reader.result })
        });
        msg.textContent = `Uploaded verified image: ${res.imageUrl}`;
        msg.className = "management-message success";
      } catch (err) {
        msg.textContent = err.message;
        msg.className = "management-message error";
      }
    };
    reader.readAsDataURL(file);
  });

  $("#v1-discount-create-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-disc-msg");
    try {
      const dtype = $("#v1-disc-type").value;
      const rawVal = Number($("#v1-disc-val").value);
      await v1Fetch("/api/v1/admin/discounts", {
        method: "POST",
        body: JSON.stringify({
          code: $("#v1-disc-code").value.trim().toUpperCase(),
          description: $("#v1-disc-title").value.trim(),
          discountPercent: dtype === "percentage" ? rawVal : null,
          discountPaise: dtype === "fixed" ? Math.round(rawVal * 100) : null,
          minOrderPaise: Math.round(Number($("#v1-disc-min").value || 0) * 100),
          isActive: true
        })
      });
      msg.textContent = "Discount offer saved.";
      msg.className = "management-message success";
      loadActiveOffers();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-set-tax-btn")?.addEventListener("click", async () => {
    const pct = window.prompt("Enter campus food tax / GST percentage (e.g. 5 for 5%):", "5");
    if (pct === null) return;
    const msg = $("#v1-disc-msg");
    try {
      const bps = Math.max(0, Math.min(2800, Math.round(Number(pct) * 100)));
      await v1Fetch("/api/v1/admin/settings", {
        method: "PATCH",
        body: JSON.stringify({ taxRateBps: bps })
      });
      msg.textContent = `Tax rate updated to ${(bps / 100).toFixed(2)}%.`;
      msg.className = "management-message success";
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-refund-form")?.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#v1-refund-msg");
    try {
      const pid = Number($("#v1-refund-payment-id").value);
      const amtStr = $("#v1-refund-amount").value;
      const body = { reason: $("#v1-refund-reason").value.trim() };
      if (amtStr) body.amountPaise = Math.round(Number(amtStr) * 100);
      const res = await v1Fetch(`/api/v1/payments/${pid}/refunds`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      msg.textContent = `Refund #${res.id} (${res.status}) recorded for ${paise(res.amountPaise)}.`;
      msg.className = "management-message success";
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "management-message error";
    }
  });

  $("#v1-run-forecast-btn")?.addEventListener("click", async () => {
    const btn = $("#v1-run-forecast-btn");
    btn.disabled = true;
    try {
      await v1Fetch("/api/v1/ai/forecasts/run", {
        method: "POST",
        body: JSON.stringify({})
      });
      await loadAdminAiHub();
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });
  $("#v1-refresh-admin-hub")?.addEventListener("click", loadAdminAiHub);

  function syncV1ForAuth(user) {
    connectRealtimeWs();
    loadRecommendations();
    const adminHub = $("#v1-admin-enterprise-hub");
    if (adminHub) adminHub.hidden = user?.role !== "admin";
    if (!user) return;
    loadUserProfileV1();
    if (user.role === "student") loadStudentActiveOrders();
    if (["staff", "admin"].includes(user.role)) {
      loadStaffKds();
      loadStaffIngredients();
    }
    if (user.role === "admin") loadAdminAiHub();
  }

  document.addEventListener("ace-auth-changed", ev => syncV1ForAuth(ev.detail));

  // Initial boot
  loadCounters();
  loadActiveOffers();
  loadRecommendations();
  connectRealtimeWs();
  if (window.aceCanteenUser) syncV1ForAuth(window.aceCanteenUser);
})();
