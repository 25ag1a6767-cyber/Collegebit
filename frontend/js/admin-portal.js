/* CampusBite AI v2.0 — Dedicated Enterprise Admin & Business Intelligence Portal Controller.
 * Strictly enforces ADMIN RBAC, CSP (zero inline styles/scripts), real PostgreSQL/SQLAlchemy metrics,
 * 10 KPI cards, 16 interactive SVG charts, 14 PDF/XLSX/CSV reports, 12 AI BI modules, and full operations.
 */
(function () {
  "use strict";

  const TOKEN_KEY = "cb_admin_access_token";
  const REFRESH_KEY = "cb_admin_refresh_token";

  const state = {
    accessToken: sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem("ace_v1_access_token") || "",
    refreshToken: sessionStorage.getItem(REFRESH_KEY) || localStorage.getItem("ace_v1_refresh_token") || "",
    user: null,
    activeTab: "bi",
    biOptionsLoaded: false,
    usersCache: [],
    categoriesCache: [],
    countersCache: [],
    menuItemsCache: [],
    suppliersCache: [],
    ingredientsCache: []
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatINRFromPaise(paise) {
    const val = Number(paise || 0) / 100;
    return "₹" + val.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatRupeesNum(paise) {
    return Number((Number(paise || 0) / 100).toFixed(2));
  }

  function fmtCurrencyShort(rupeesVal) {
    const n = Number(rupeesVal || 0);
    if (Math.abs(n) >= 100000) return "₹" + (n / 100000).toFixed(2) + "L";
    if (Math.abs(n) >= 1000) return "₹" + (n / 1000).toFixed(1) + "k";
    return "₹" + n.toFixed(0);
  }

  function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "short" });
  }

  function showAlert(msg, kind) {
    const bar = $("admin-alert");
    if (!bar) return;
    if (!msg) {
      bar.hidden = true;
      return;
    }
    bar.textContent = msg;
    bar.className = "alert-bar" + (kind ? " " + kind : "");
    bar.hidden = false;
  }

  function saveTokens(access, refresh) {
    state.accessToken = access || "";
    state.refreshToken = refresh || "";
    if (state.accessToken) {
      sessionStorage.setItem(TOKEN_KEY, state.accessToken);
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
    }
    if (state.refreshToken) {
      sessionStorage.setItem(REFRESH_KEY, state.refreshToken);
    } else {
      sessionStorage.removeItem(REFRESH_KEY);
    }
  }

  async function apiFetch(path, opts) {
    if (!navigator.onLine) {
      throw new Error("Network disconnected. Please check your internet connection.");
    }
    const options = Object.assign({ method: "GET", headers: {} }, opts || {});
    options.headers = Object.assign({}, options.headers);
    if (state.accessToken) {
      options.headers.Authorization = "Bearer " + state.accessToken;
    }
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }

    let res;
    try {
      res = await fetch(path, options);
    } catch (err) {
      throw new Error("Backend server is offline or unreachable at http://127.0.0.1:8000. Please start the server.");
    }
    if (res.status === 401 && state.refreshToken && path !== "/api/v1/auth/refresh" && path !== "/api/v1/auth/login") {
      const refRes = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refreshToken: state.refreshToken })
      });
      if (refRes.ok) {
        const refJson = await refRes.json();
        if (refJson && refJson.data && refJson.data.accessToken) {
          saveTokens(refJson.data.accessToken, refJson.data.refreshToken || state.refreshToken);
          options.headers.Authorization = "Bearer " + state.accessToken;
          try {
            res = await fetch(path, options);
          } catch (err) {
            throw new Error("Backend server is offline or unreachable at http://127.0.0.1:8000. Please start the server.");
          }
        }
      }
    }

    const ctype = res.headers.get("content-type") || "";
    const payload = ctype.includes("application/json") ? await res.json() : null;
    if (!res.ok) {
      if (res.status === 401) {
        throw new Error("Invalid email/phone or password. Please verify your administrator credentials.");
      }
      if (res.status === 403) {
        const code = (payload && payload.error && payload.error.code) || "";
        if (code === "ACCOUNT_DISABLED") {
          throw new Error("This administrator account has been deactivated.");
        }
        throw new Error("Access denied: Administrator role is required.");
      }
      if (res.status >= 500) {
        throw new Error("Server error (" + res.status + "). Please try again in a moment.");
      }
      const errMsg = (payload && payload.error && payload.error.message) || ("Request failed (" + res.status + ")");
      throw new Error(errMsg);
    }
    return payload ? payload.data : null;
  }

  async function downloadAuthenticatedFile(url, fallbackFilename) {
    const res = await fetch(url, {
      method: "GET",
      headers: state.accessToken ? { Authorization: "Bearer " + state.accessToken } : {}
    });
    if (!res.ok) {
      let msg = "Download failed (" + res.status + ")";
      try {
        const err = await res.json();
        if (err && err.error && err.error.message) msg = err.error.message;
      } catch (_) {}
      throw new Error(msg);
    }
    const blob = await res.blob();
    const disp = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disp);
    const filename = (match && match[1]) || fallbackFilename;
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
    return filename;
  }

  // ============================================================================
  // Authentication & Session Gate (Strictly ADMIN Only)
  // ============================================================================
  function setSignedInUI(user) {
    state.user = user;
    const isAuthed = Boolean(user && String(user.role).toUpperCase() === "ADMIN");
    $("admin-login-view").hidden = isAuthed;
    $("admin-workspace").hidden = !isAuthed;
    $("btn-admin-refresh").hidden = !isAuthed;
    $("btn-admin-logout").hidden = !isAuthed;
    if (isAuthed) {
      $("admin-Identity-subtitle").textContent = "Signed in as " + user.fullName + " (" + user.email + ") · Role: ADMIN";
    } else {
      $("admin-Identity-subtitle").textContent = "Centralized College Canteen Governance, Analytics & Operations";
    }
  }

  async function verifySessionOnBoot() {
    if (!state.accessToken) {
      setSignedInUI(null);
      return;
    }
    try {
      const me = await apiFetch("/api/v1/me");
      if (!me || String(me.role).toUpperCase() !== "ADMIN") {
        saveTokens("", "");
        setSignedInUI(null);
        showAlert("Administrator role is required for /admin. Student and staff accounts cannot access administrative modules.", "error");
        return;
      }
      setSignedInUI(me);
      await loadActiveTab();
    } catch (_) {
      saveTokens("", "");
      setSignedInUI(null);
    }
  }

  async function onAdminLoginSubmit(ev) {
    ev.preventDefault();
    showAlert("", "");
    const identifier = $("admin-identifier").value.trim();
    const password = $("admin-password").value;
    try {
      const data = await apiFetch("/api/v1/auth/login", {
        method: "POST",
        body: { identifier, password }
      });
      if (!data.user || String(data.user.role).toUpperCase() !== "ADMIN") {
        showAlert("Access denied: Only ADMIN accounts may sign in to the Enterprise Administration Console.", "error");
        return;
      }
      saveTokens(data.accessToken, data.refreshToken);
      setSignedInUI(data.user);
      showAlert("Authenticated as " + data.user.fullName + ".", "success");
      await loadActiveTab();
    } catch (err) {
      showAlert(err.message, "error");
    }
  }

  async function onAdminLogout() {
    try {
      if (state.refreshToken) {
        await apiFetch("/api/v1/auth/logout", { method: "POST", body: { refreshToken: state.refreshToken } });
      }
    } catch (_) {}
    saveTokens("", "");
    localStorage.removeItem("ace_v1_access_token");
    localStorage.removeItem("ace_v1_refresh_token");
    setSignedInUI(null);
    showAlert("Signed out of Admin Console.", "success");
  }

  // ============================================================================
  // Tab Navigation
  // ============================================================================
  async function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tabName);
    });
    document.querySelectorAll(".admin-tab-pane").forEach((pane) => {
      pane.hidden = pane.id !== "tab-" + tabName;
    });
    await loadActiveTab();
  }

  async function loadActiveTab() {
    if (!state.user) return;
    try {
      if (state.activeTab === "bi") await loadBIDashboard();
      else if (state.activeTab === "reports") await loadReportsStudio();
      else if (state.activeTab === "aibi") await loadAIBIDashboard();
      else if (state.activeTab === "users") await loadUsersModule();
      else if (state.activeTab === "menu") await loadMenuModule();
      else if (state.activeTab === "procurement") await loadProcurementModule();
      else if (state.activeTab === "operations") await loadOperationsModule();
      else if (state.activeTab === "settings") await loadSettingsModule();
      else if (state.activeTab === "governance") await loadGovernanceModule();
    } catch (err) {
      showAlert(err.message, "error");
    }
  }

  // ============================================================================
  // TAB 1: ADVANCED BI ANALYTICS (10 KPI CARDS + 16 INTERACTIVE CHARTS)
  // ============================================================================
  function populateSelectOptions(selectEl, items, defaultLabel, valueKey, labelKey, keepCurrent) {
    if (!selectEl) return;
    const prev = keepCurrent ? selectEl.value : "";
    const frag = document.createDocumentFragment();
    if (defaultLabel !== null) {
      const opt0 = el("option", "", defaultLabel);
      opt0.value = "";
      frag.appendChild(opt0);
    }
    (items || []).forEach((it) => {
      const opt = el("option", "", it[labelKey]);
      opt.value = String(it[valueKey]);
      frag.appendChild(opt);
    });
    selectEl.replaceChildren(frag);
    if (keepCurrent && prev) selectEl.value = prev;
  }

  async function loadBIDashboard() {
    const params = new URLSearchParams();
    const preset = $("bi-preset").value || "last_30_days";
    params.set("preset", preset);
    if (preset === "custom") {
      if ($("bi-start-date").value) params.set("startDate", $("bi-start-date").value);
      if ($("bi-end-date").value) params.set("endDate", $("bi-end-date").value);
    }
    if ($("bi-category").value) params.set("categoryId", $("bi-category").value);
    if ($("bi-product").value) params.set("productId", $("bi-product").value);
    if ($("bi-counter").value) params.set("counterId", $("bi-counter").value);
    params.set("compare", $("bi-compare").value || "today_vs_yesterday");

    const d = await apiFetch("/api/v1/admin/analytics/dashboard?" + params.toString());

    if (d.filterOptions) {
      state.categoriesCache = d.filterOptions.categories || [];
      state.countersCache = d.filterOptions.counters || [];
      populateSelectOptions($("bi-category"), d.filterOptions.categories, "All Categories", "id", "name", true);
      populateSelectOptions($("bi-product"), d.filterOptions.products, "All Products", "id", "name", true);
      populateSelectOptions($("bi-counter"), d.filterOptions.counters, "All Counters", "id", "name", true);
    }

    // Render 10 Required KPI Cards
    const c = d.cards || {};
    const kpiDefs = [
      { label: "1. Today's Revenue", val: formatINRFromPaise(c.todayRevenuePaise), sub: "Filtered window: " + formatINRFromPaise(c.filteredRevenuePaise) },
      { label: "2. Today's Total Orders", val: String(c.todayTotalOrders || 0), sub: "Filtered window: " + (c.filteredTotalOrders || 0) + " orders" },
      { label: "3. Successful Payments", val: String(c.successfulPayments || 0), sub: "Verified paid captures" },
      { label: "4. Failed Payments", val: String(c.failedPayments || 0), sub: "Declined or expired intents" },
      { label: "5. Pending Orders", val: String(c.pendingOrders || 0), sub: "Awaiting prep / kitchen queue" },
      { label: "6. Completed Orders", val: String(c.completedOrders || 0), sub: "Picked up & closed" },
      { label: "7. Cancelled Orders", val: String(c.cancelledOrders || 0), sub: "Cancelled or refund pending" },
      { label: "8. Average Order Value", val: formatINRFromPaise(c.averageOrderValuePaise), sub: "Mean paid basket" },
      { label: "9. Active Customers", val: String(c.activeCustomers || 0), sub: (c.registeredActiveStudents || 0) + " registered students" },
      { label: "10. Estimated Profit", val: formatINRFromPaise(c.estimatedProfitPaise), sub: "Expenses + COGS: " + formatINRFromPaise(c.totalExpensesPaise) }
    ];
    const kpiFrag = document.createDocumentFragment();
    kpiDefs.forEach((k) => {
      const tile = el("article", "kpi-tile");
      tile.appendChild(el("span", "", k.label));
      tile.appendChild(el("strong", "", k.val));
      tile.appendChild(el("small", "", k.sub));
      kpiFrag.appendChild(tile);
    });
    $("bi-kpi-cards").replaceChildren(kpiFrag);

    // Render Period Comparison Banner
    const cmp = d.activeComparison;
    const banner = $("bi-comparison-banner");
    if (cmp && cmp.deltas) {
      const fmtDelta = (pct) => (pct === null || pct === undefined ? "0%" : (pct >= 0 ? "+" + pct + "%" : pct + "%"));
      banner.replaceChildren(
        el("strong", "", "Comparison (" + cmp.label + "):"),
        el("span", "", "Revenue: " + formatINRFromPaise(cmp.current.revenuePaise) + " vs " + formatINRFromPaise(cmp.previous.revenuePaise) + " (" + fmtDelta(cmp.deltas.revenuePct) + ")"),
        el("span", "", "Orders: " + cmp.current.totalOrders + " vs " + cmp.previous.totalOrders + " (" + fmtDelta(cmp.deltas.ordersPct) + ")"),
        el("span", "", "AOV: " + fmtDelta(cmp.deltas.aovPct)),
        el("span", "", "Est. Profit: " + fmtDelta(cmp.deltas.profitPct))
      );
    }

    // Render All 16 Interactive Charts
    const Ch = window.CampusBiteCharts;
    const g = d.graphs || {};
    if (!Ch) return;

    // 1. Daily Sales Line Graph
    const daily = g.dailySales || [];
    Ch.renderLineChart($("chart-daily-sales"), {
      title: "Daily Sales Revenue",
      labels: daily.map((r) => r.date),
      series: [{ name: "Revenue (₹)", color: "#0f766e", values: daily.map((r) => formatRupeesNum(r.revenuePaise)) }],
      formatValue: fmtCurrencyShort
    });

    // 2. Weekly Sales Graph
    const weekly = g.weeklySales || [];
    Ch.renderBarChart($("chart-weekly-sales"), {
      labels: weekly.map((r) => r.week),
      series: [{ name: "Weekly Revenue (₹)", color: "#2563eb", values: weekly.map((r) => formatRupeesNum(r.revenuePaise)) }],
      formatValue: fmtCurrencyShort
    });

    // 3. Monthly Sales Graph
    const monthly = g.monthlySales || [];
    Ch.renderBarChart($("chart-monthly-sales"), {
      labels: monthly.map((r) => r.month),
      series: [{ name: "Monthly Revenue (₹)", color: "#7c3aed", values: monthly.map((r) => formatRupeesNum(r.revenuePaise)) }],
      formatValue: fmtCurrencyShort
    });

    // 4. Yearly Sales Graph
    const yearly = g.yearlySales || [];
    Ch.renderBarChart($("chart-yearly-sales"), {
      labels: yearly.map((r) => r.year),
      series: [{ name: "Yearly Revenue (₹)", color: "#0891b2", values: yearly.map((r) => formatRupeesNum(r.revenuePaise)) }],
      formatValue: fmtCurrencyShort
    });

    // 5. Top-Selling Food Bar Graph
    const topFoods = g.topSellingFoods || [];
    Ch.renderBarChart($("chart-top-foods"), {
      horizontal: true,
      labels: topFoods.map((r) => r.name),
      series: [{ name: "Units Sold", color: "#16a34a", values: topFoods.map((r) => r.unitsSold) }]
    });

    // 6. Least-Selling Food Graph
    const leastFoods = g.leastSellingFoods || [];
    Ch.renderBarChart($("chart-least-foods"), {
      horizontal: true,
      labels: leastFoods.map((r) => r.name),
      series: [{ name: "Units Sold", color: "#d97706", values: leastFoods.map((r) => r.unitsSold) }]
    });

    // 7. Category-Wise Sales Pie Chart
    const catPie = g.categorySalesPie || [];
    Ch.renderPieChart($("chart-category-pie"), {
      centerTitle: "Category ₹",
      slices: catPie.map((r) => ({ label: r.categoryName, value: formatRupeesNum(r.revenuePaise) })),
      formatValue: fmtCurrencyShort
    });

    // 8. Peak Ordering Hours Graph
    const peakHrs = (g.peakOrderingHours || []).filter((_, h) => h >= 7 && h <= 20);
    Ch.renderBarChart($("chart-peak-hours"), {
      labels: peakHrs.map((r) => r.hour),
      series: [{ name: "Paid Orders", color: "#ea580c", values: peakHrs.map((r) => r.orders) }]
    });

    // 9. Customer Purchase Trends
    const custDaily = (g.customerPurchaseTrends && g.customerPurchaseTrends.dailySeries) || [];
    Ch.renderLineChart($("chart-customer-trends"), {
      labels: custDaily.map((r) => r.date),
      series: [
        { name: "Unique Buyers", color: "#2563eb", values: custDaily.map((r) => r.uniqueBuyers) },
        { name: "Items Purchased", color: "#0f766e", values: custDaily.map((r) => r.itemsPurchased) }
      ]
    });

    // 10. Payment Success/Failure Graph
    const payTotals = (g.paymentSuccessFailure && g.paymentSuccessFailure.totals) || [];
    Ch.renderPieChart($("chart-payment-status"), {
      centerTitle: "Payments",
      slices: payTotals.map((r) => ({ label: r.status, value: r.count }))
    });

    // 11. Inventory Consumption Graph
    const invCons = g.inventoryConsumption || [];
    Ch.renderBarChart($("chart-inventory-consumption"), {
      horizontal: true,
      labels: invCons.map((r) => r.name),
      series: [{ name: "Consumed", color: "#4f46e5", values: invCons.map((r) => r.consumed) }]
    });

    // 12. Food Wastage Graph
    const wasteItems = (g.foodWastage && g.foodWastage.byItem) || [];
    const wasteDaily = (g.foodWastage && g.foodWastage.dailySeries) || [];
    if (wasteItems.length > 0) {
      Ch.renderBarChart($("chart-food-wastage"), {
        horizontal: true,
        labels: wasteItems.map((r) => r.name),
        series: [{ name: "Loss (₹)", color: "#dc2626", values: wasteItems.map((r) => formatRupeesNum(r.lossPaise)) }],
        formatValue: fmtCurrencyShort
      });
    } else {
      Ch.renderLineChart($("chart-food-wastage"), {
        labels: wasteDaily.map((r) => r.date),
        series: [{ name: "Wastage Loss (₹)", color: "#dc2626", values: wasteDaily.map((r) => formatRupeesNum(r.lossPaise)) }],
        formatValue: fmtCurrencyShort
      });
    }

    // 13. Staff Performance Graph
    const staffPerf = g.staffPerformance || [];
    Ch.renderBarChart($("chart-staff-performance"), {
      labels: staffPerf.map((r) => r.staffName),
      series: [
        { name: "Orders Handled", color: "#0f766e", values: staffPerf.map((r) => r.ordersHandled) },
        { name: "Pickups Verified", color: "#2563eb", values: staffPerf.map((r) => r.ordersCollected) }
      ]
    });

    // 14. Counter-Wise Sales Graph
    const cntSales = g.counterWiseSales || [];
    Ch.renderBarChart($("chart-counter-sales"), {
      labels: cntSales.map((r) => r.counterName),
      series: [{ name: "Revenue (₹)", color: "#0891b2", values: cntSales.map((r) => formatRupeesNum(r.revenuePaise)) }],
      formatValue: fmtCurrencyShort
    });

    // 15. Revenue vs Expenses Graph
    const revExp = g.revenueVsExpenses || [];
    Ch.renderLineChart($("chart-rev-vs-exp"), {
      labels: revExp.map((r) => r.date),
      series: [
        { name: "Revenue (₹)", color: "#16a34a", values: revExp.map((r) => formatRupeesNum(r.revenuePaise)) },
        { name: "COGS + Expenses + Waste (₹)", color: "#dc2626", values: revExp.map((r) => formatRupeesNum(r.expensesPaise)) }
      ],
      formatValue: fmtCurrencyShort
    });

    // 16. Profitability Graph
    const prof = g.profitability || [];
    Ch.renderLineChart($("chart-profitability"), {
      labels: prof.map((r) => r.date),
      series: [{ name: "Net Profit (₹)", color: "#0f2942", values: prof.map((r) => formatRupeesNum(r.profitPaise)) }],
      formatValue: fmtCurrencyShort
    });
  }

  // ============================================================================
  // TAB 2: 14 REPORTS STUDIO (PDF / XLSX / CSV + AUTOMATED SCHEDULE)
  // ============================================================================
  function buildReportQueryParams() {
    const p = new URLSearchParams();
    p.set("reportType", $("rep-type").value || "daily_sales");
    if ($("rep-start").value) p.set("startDate", $("rep-start").value);
    if ($("rep-end").value) p.set("endDate", $("rep-end").value);
    if ($("rep-counter").value) p.set("counterId", $("rep-counter").value);
    return p;
  }

  async function loadReportsStudio() {
    const [cat, countersRes] = await Promise.all([
      apiFetch("/api/v1/admin/reports/catalog"),
      apiFetch("/api/v1/admin/counters")
    ]);
    populateSelectOptions($("rep-counter"), countersRes.items || [], "All Counters", "id", "name", true);
    $("rep-emails").value = cat.dailyReportEmails || "";
    $("rep-smtp-badge").textContent = cat.smtpConfigured ? "SMTP Ready" : "SMTP Optional (Log Fallback)";
    $("rep-smtp-badge").className = "pill " + (cat.smtpConfigured ? "pill-ok" : "pill-muted");

    const tbody = $("rep-recent-tbody");
    const frag = document.createDocumentFragment();
    (cat.recentGeneratedReports || []).forEach((r) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", fmtDateTime(r.createdAt)));
      tr.appendChild(el("td", "", r.reportType));
      tr.appendChild(el("td", "mono", r.startDate + " → " + r.endDate));
      tr.appendChild(el("td", "", String(r.format).toUpperCase()));
      tr.appendChild(el("td", "", String(r.rowCount)));
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);

    await previewSelectedReport();
  }

  async function previewSelectedReport() {
    const q = buildReportQueryParams();
    const d = await apiFetch("/api/v1/admin/reports/preview?" + q.toString());
    $("rep-preview-title").textContent = d.title + " (" + d.startDate + " to " + d.endDate + ")";
    $("rep-preview-meta").textContent = d.rowCount + " total rows";

    const sumBox = $("rep-preview-summary");
    const sumFrag = document.createDocumentFragment();
    const sumObj = d.summary || {};
    Object.keys(sumObj).forEach((k) => {
      sumFrag.appendChild(el("span", "pill pill-actual", k + ": " + sumObj[k]));
    });
    sumBox.replaceChildren(sumFrag);

    const trHead = el("tr");
    (d.columns || []).forEach((col) => trHead.appendChild(el("th", "", col)));
    $("rep-preview-thead").replaceChildren(trHead);

    const tbody = $("rep-preview-tbody");
    const bodyFrag = document.createDocumentFragment();
    if (!(d.rows || []).length) {
      const emptyTr = el("tr");
      const td = el("td", "muted", "No records matched the selected date window.");
      td.colSpan = Math.max(1, (d.columns || []).length);
      emptyTr.appendChild(td);
      bodyFrag.appendChild(emptyTr);
    } else {
      (d.rows || []).forEach((row) => {
        const tr = el("tr");
        row.forEach((cell) => tr.appendChild(el("td", "", cell === null || cell === undefined ? "" : String(cell))));
        bodyFrag.appendChild(tr);
      });
    }
    tbody.replaceChildren(bodyFrag);
  }

  async function triggerReportDownload(fmt) {
    try {
      const q = buildReportQueryParams();
      q.set("format", fmt);
      const fname = await downloadAuthenticatedFile(
        "/api/v1/admin/reports/download?" + q.toString(),
        "campusbite-report." + fmt
      );
      showAlert("Downloaded " + fname, "success");
      await loadReportsStudio();
    } catch (err) {
      showAlert(err.message, "error");
    }
  }

  async function triggerBiReportDownload(fmt) {
    try {
      const q = new URLSearchParams();
      const preset = $("bi-preset").value || "today";
      let repType = "daily_sales";
      if (preset === "weekly" || preset === "last_7_days") repType = "weekly_sales";
      else if (preset === "this_month" || preset === "last_month") repType = "monthly_financial";
      else if (preset === "this_year") repType = "yearly_performance";

      q.set("reportType", repType);
      q.set("format", fmt);
      if ($("bi-start-date").value) q.set("startDate", $("bi-start-date").value);
      if ($("bi-end-date").value) q.set("endDate", $("bi-end-date").value);
      if ($("bi-counter").value) q.set("counterId", $("bi-counter").value);

      const fname = await downloadAuthenticatedFile(
        "/api/v1/admin/reports/download?" + q.toString(),
        `campusbite-analytics-${preset}.${fmt}`
      );
      showAlert("Downloaded analytics report: " + fname, "success");
    } catch (err) {
      showAlert(err.message, "error");
    }
  }

  // ============================================================================
  // TAB 3: AI BUSINESS INTELLIGENCE SUITE (12 MODULES)
  // ============================================================================
  async function loadAIBIDashboard() {
    const days = $("aibi-days").value || "30";
    const d = await apiFetch("/api/v1/ai/bi/dashboard?days=" + encodeURIComponent(days));
    $("aibi-transparency").textContent = d.transparencyNotice || "";
    $("aibi-narrative").textContent = (d.dailyBusinessSummary && d.dailyBusinessSummary.narrative) || "";

    const Ch = window.CampusBiteCharts;
    if (Ch) {
      const f7 = d.salesForecast7Day || [];
      Ch.renderLineChart($("chart-aibi-forecast7d"), {
        labels: f7.map((r) => r.weekday.slice(0, 3) + " " + r.date.slice(5)),
        series: [
          { name: "Projected Revenue (₹)", color: "#7c3aed", values: f7.map((r) => formatRupeesNum(r.projectedRevenuePaise)) },
          { name: "Upper Band (₹)", color: "#94a3b8", values: f7.map((r) => formatRupeesNum(r.upperBoundPaise)) }
        ],
        formatValue: fmtCurrencyShort
      });

      const ph = d.peakHourPredictions || [];
      Ch.renderBarChart($("chart-aibi-peakhours"), {
        labels: ph.map((r) => r.hour),
        series: [{ name: "Predicted Orders", color: "#2563eb", values: ph.map((r) => r.predictedOrders) }]
      });
    }

    // Demand table
    const demFrag = document.createDocumentFragment();
    (d.foodDemandPredictions || []).forEach((it) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", it.name));
      tr.appendChild(el("td", "", String(it.predictedUnits)));
      tr.appendChild(el("td", "", String(it.recommendedPrep)));
      tr.appendChild(el("td", "", String(it.currentStock)));
      const sfTd = el("td");
      sfTd.appendChild(el("span", "pill " + (it.shortfall > 0 ? "pill-danger" : "pill-ok"), it.shortfall > 0 ? ("Short " + it.shortfall) : "Covered"));
      tr.appendChild(sfTd);
      demFrag.appendChild(tr);
    });
    $("aibi-demand-tbody").replaceChildren(demFrag);

    // Reorder recommendations
    const reoFrag = document.createDocumentFragment();
    (d.inventoryReorderRecommendations || []).forEach((r) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", r.kind));
      tr.appendChild(el("td", "", r.name));
      tr.appendChild(el("td", "mono", r.currentStock));
      tr.appendChild(el("td", "mono", r.suggestedReorderQty));
      const urgTd = el("td");
      urgTd.appendChild(el("span", "pill " + (r.urgency === "CRITICAL" ? "pill-danger" : "pill-warn"), r.urgency));
      tr.appendChild(urgTd);
      reoFrag.appendChild(tr);
    });
    $("aibi-reorder-tbody").replaceChildren(reoFrag);

    // Menu Engineering Matrix
    const matFrag = document.createDocumentFragment();
    (d.menuEngineeringMatrix || []).forEach((m) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", m.name));
      const qTd = el("td");
      const qClass = m.quadrant === "STAR" ? "pill-ok" : (m.quadrant === "DOG" ? "pill-danger" : "pill-actual");
      qTd.appendChild(el("span", "pill " + qClass, m.quadrant));
      tr.appendChild(qTd);
      tr.appendChild(el("td", "", String(m.unitsSold)));
      tr.appendChild(el("td", "mono", formatINRFromPaise(m.unitMarginPaise)));
      tr.appendChild(el("td", "muted", m.action));
      matFrag.appendChild(tr);
    });
    $("aibi-matrix-tbody").replaceChildren(matFrag);

    // Anomalies, combos & slow items summary
    const anomBox = $("aibi-anomalies-box");
    const boxFrag = document.createDocumentFragment();
    const anoms = d.salesAnomalies || [];
    boxFrag.appendChild(el("h4", "", "Detected Revenue Anomalies (|Z| >= 1.75): " + anoms.length));
    if (!anoms.length) {
      boxFrag.appendChild(el("p", "muted", "No abnormal revenue spikes or drops detected in this observation window."));
    } else {
      anoms.forEach((a) => {
        boxFrag.appendChild(el("p", "", a.date + " [" + a.direction + ", Z=" + a.zScore + "]: " + formatINRFromPaise(a.revenuePaise) + " — " + a.explanation));
      });
    }
    const combos = (d.customerPurchasingTrends && d.customerPurchasingTrends.topComboPairs) || [];
    boxFrag.appendChild(el("h4", "", "Top Co-Purchased Student Combos"));
    combos.forEach((c) => {
      boxFrag.appendChild(el("span", "pill pill-actual", c.pair + " (" + c.coOccurrences + "×) "));
    });
    anomBox.replaceChildren(boxFrag);
  }

  // ============================================================================
  // TAB 4: USERS, ROLES & STAFF PERMISSIONS
  // ============================================================================
  async function loadUsersModule() {
    const q = new URLSearchParams();
    if ($("usr-filter-role").value) q.set("role", $("usr-filter-role").value);
    if ($("usr-filter-search").value.trim()) q.set("search", $("usr-filter-search").value.trim());
    const data = await apiFetch("/api/v1/admin/users?" + q.toString());
    state.usersCache = data.items || [];

    const staffList = state.usersCache.filter((u) => String(u.role).toUpperCase() === "STAFF");
    populateSelectOptions(
      $("perm-user-select"),
      staffList.map((u) => ({ id: u.id, label: u.fullName + " (" + u.email + ")" })),
      "Select staff user…",
      "id",
      "label",
      true
    );

    const tbody = $("users-tbody");
    const frag = document.createDocumentFragment();
    state.usersCache.forEach((u) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + u.id));
      const nameTd = el("td");
      nameTd.appendChild(el("strong", "", u.fullName));
      nameTd.appendChild(el("div", "muted", u.email + (u.phone ? " · " + u.phone : "")));
      tr.appendChild(nameTd);
      tr.appendChild(el("td", "", String(u.role).toUpperCase()));
      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (u.isActive ? "pill-ok" : "pill-danger"), u.isActive ? "ACTIVE" : "SUSPENDED"));
      tr.appendChild(stTd);

      const metaTd = el("td", "muted");
      if (String(u.role).toUpperCase() === "STAFF" && u.staffPermissions) {
        const enabledPerms = Object.keys(u.staffPermissions).filter((k) => u.staffPermissions[k]);
        metaTd.textContent = "Counters: [" + (u.assignedCounterIds || []).join(",") + "] · " + enabledPerms.length + "/6 perms";
      } else {
        metaTd.textContent = "UUID: " + String(u.publicUuid || "").slice(0, 8) + "…";
      }
      tr.appendChild(metaTd);

      const actTd = el("td");
      const toggleBtn = el("button", "btn btn-outline", u.isActive ? "Suspend" : "Activate");
      toggleBtn.type = "button";
      toggleBtn.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/admin/users/" + u.id + "/status", {
            method: "PATCH",
            body: { isActive: !u.isActive }
          });
          showAlert("Updated status for " + u.email, "success");
          await loadUsersModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      actTd.appendChild(toggleBtn);
      tr.appendChild(actTd);
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
  }

  // ============================================================================
  // TAB 5: MENU, CATEGORIES, PRICING, COSTS & DISCOUNTS
  // ============================================================================
  async function loadMenuModule() {
    const [catsRes, countersRes, itemsRes, discRes] = await Promise.all([
      apiFetch("/api/v1/menu/categories"),
      apiFetch("/api/v1/admin/counters"),
      apiFetch("/api/v1/admin/menu-items?includeArchived=true"),
      apiFetch("/api/v1/admin/discounts")
    ]);
    const cats = catsRes.items || catsRes || [];
    populateSelectOptions($("itm-cat"), cats, null, "id", "name", true);
    populateSelectOptions($("itm-counter"), countersRes.items || [], "Default Counter", "id", "name", true);
    state.menuItemsCache = itemsRes.items || [];

    const mFrag = document.createDocumentFragment();
    state.menuItemsCache.forEach((it) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + it.id));
      const infoTd = el("td");
      infoTd.appendChild(el("strong", "", it.name));
      infoTd.appendChild(el("div", "muted", it.categoryName || ""));
      tr.appendChild(infoTd);

      const counterTd = el("td");
      const counterSelect = el("select");
      (countersRes.items || []).forEach((c) => {
        const opt = el("option", "", c.name);
        opt.value = String(c.id);
        if (Number(it.counterId) === Number(c.id)) {
          opt.selected = true;
        }
        counterSelect.appendChild(opt);
      });
      counterTd.appendChild(counterSelect);
      tr.appendChild(counterTd);

      const priceInput = el("input");
      priceInput.type = "number";
      priceInput.step = "0.5";
      priceInput.value = String(formatRupeesNum(it.pricePaise));
      const pTd = el("td");
      pTd.appendChild(priceInput);
      tr.appendChild(pTd);

      const costInput = el("input");
      costInput.type = "number";
      costInput.step = "0.5";
      costInput.value = String(formatRupeesNum(it.costPaise || 0));
      const cTd = el("td");
      cTd.appendChild(costInput);
      tr.appendChild(cTd);

      const stockInput = el("input");
      stockInput.type = "number";
      stockInput.value = String(it.stockQuantity || 0);
      const sTd = el("td");
      sTd.appendChild(stockInput);
      tr.appendChild(sTd);

      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (it.isAvailable ? "pill-ok" : "pill-warn"), it.isAvailable ? "AVAILABLE" : "PAUSED/OUT"));
      tr.appendChild(stTd);

      const actTd = el("td", "inline-controls");
      const saveBtn = el("button", "btn btn-primary", "Save");
      saveBtn.type = "button";
      saveBtn.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/admin/menu-items/" + it.id, {
            method: "PATCH",
            body: {
              counterId: counterSelect.value ? Number(counterSelect.value) : null,
              pricePaise: Math.round(Number(priceInput.value || 0) * 100),
              costPaise: Math.round(Number(costInput.value || 0) * 100),
              stockQuantity: Math.round(Number(stockInput.value || 0))
            }
          });
          showAlert("Updated " + it.name, "success");
          await loadMenuModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      const toggleBtn = el("button", "btn btn-outline", it.rawIsAvailable ? "Pause" : "Enable");
      toggleBtn.type = "button";
      toggleBtn.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/admin/menu-items/" + it.id, {
            method: "PATCH",
            body: { isAvailable: !it.rawIsAvailable }
          });
          await loadMenuModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      const deleteBtn = el("button", "btn btn-danger", "Delete");
      deleteBtn.type = "button";
      deleteBtn.addEventListener("click", async () => {
        if (!confirm("Are you sure you want to delete '" + it.name + "'?")) return;
        try {
          await apiFetch("/api/v1/admin/menu-items/" + it.id, {
            method: "DELETE"
          });
          showAlert("Deleted " + it.name, "success");
          await loadMenuModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      actTd.appendChild(saveBtn);
      actTd.appendChild(toggleBtn);
      actTd.appendChild(deleteBtn);
      tr.appendChild(actTd);
      mFrag.appendChild(tr);
    });
    $("menu-items-tbody").replaceChildren(mFrag);

    const dFrag = document.createDocumentFragment();
    (discRes.items || []).forEach((d) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", d.code));
      const rule = Number(d.discountPercent) > 0 ? (d.discountPercent + "% off") : (formatINRFromPaise(d.discountPaise) + " flat");
      tr.appendChild(el("td", "", rule));
      tr.appendChild(el("td", "", formatINRFromPaise(d.minOrderPaise)));
      tr.appendChild(el("td", "", d.usageCount + (d.usageLimit ? " / " + d.usageLimit : "")));
      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (d.isActive ? "pill-ok" : "pill-muted"), d.isActive ? "ACTIVE" : "DISABLED"));
      tr.appendChild(stTd);
      const actTd = el("td");
      const btn = el("button", "btn btn-outline", d.isActive ? "Disable" : "Enable");
      btn.type = "button";
      btn.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/admin/discounts/" + d.id, { method: "PATCH", body: { isActive: !d.isActive } });
          await loadMenuModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      actTd.appendChild(btn);
      tr.appendChild(actTd);
      dFrag.appendChild(tr);
    });
    $("discounts-tbody").replaceChildren(dFrag);
  }

  // ============================================================================
  // TAB 6: PROCUREMENT (SUPPLIERS, PURCHASE ORDERS, INGREDIENTS, EXPENSES)
  // ============================================================================
  async function loadProcurementModule() {
    const [supRes, poRes, ingRes, expRes, itemsRes] = await Promise.all([
      apiFetch("/api/v1/admin/suppliers"),
      apiFetch("/api/v1/admin/purchase-orders"),
      apiFetch("/api/v1/admin/ingredients"),
      apiFetch("/api/v1/admin/expenses"),
      apiFetch("/api/v1/admin/menu-items")
    ]);
    state.suppliersCache = supRes.items || [];
    state.ingredientsCache = ingRes.items || [];
    state.menuItemsCache = itemsRes.items || [];

    populateSelectOptions($("po-supplier"), state.suppliersCache.filter((s) => s.isActive), null, "id", "name", true);
    populateSelectOptions($("po-menuitem"), state.menuItemsCache, null, "id", "name", true);
    populateSelectOptions($("po-ingredient"), state.ingredientsCache, null, "id", "name", true);

    // Suppliers table
    const sFrag = document.createDocumentFragment();
    state.suppliersCache.forEach((s) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + s.id));
      tr.appendChild(el("td", "", s.name));
      tr.appendChild(el("td", "muted", [s.contactName, s.phone, s.email].filter(Boolean).join(" · ")));
      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (s.isActive ? "pill-ok" : "pill-muted"), s.isActive ? "ACTIVE" : "INACTIVE"));
      tr.appendChild(stTd);
      const actTd = el("td");
      const b = el("button", "btn btn-outline", s.isActive ? "Deactivate" : "Activate");
      b.type = "button";
      b.addEventListener("click", async () => {
        await apiFetch("/api/v1/admin/suppliers/" + s.id, { method: "PATCH", body: { isActive: !s.isActive } });
        await loadProcurementModule();
      });
      actTd.appendChild(b);
      tr.appendChild(actTd);
      sFrag.appendChild(tr);
    });
    $("suppliers-tbody").replaceChildren(sFrag);

    // Purchase Orders table
    const pFrag = document.createDocumentFragment();
    (poRes.items || []).forEach((po) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", po.poNumber));
      tr.appendChild(el("td", "", po.supplierName));
      tr.appendChild(el("td", "mono", formatINRFromPaise(po.totalCostPaise)));
      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (po.status === "RECEIVED" ? "pill-ok" : "pill-warn"), po.status));
      tr.appendChild(stTd);
      const actTd = el("td");
      if (po.status !== "RECEIVED" && po.status !== "CANCELLED") {
        const recBtn = el("button", "btn btn-ok", "Receive into Stock");
        recBtn.type = "button";
        recBtn.addEventListener("click", async () => {
          try {
            await apiFetch("/api/v1/admin/purchase-orders/" + po.id + "/receive", { method: "POST", body: {} });
            showAlert(po.poNumber + " received into stock batches & operational expenses.", "success");
            await loadProcurementModule();
          } catch (err) {
            showAlert(err.message, "error");
          }
        });
        actTd.appendChild(recBtn);
      } else {
        actTd.textContent = po.receivedAt ? fmtDateTime(po.receivedAt) : "—";
      }
      tr.appendChild(actTd);
      pFrag.appendChild(tr);
    });
    $("pos-tbody").replaceChildren(pFrag);

    // Ingredients table
    const iFrag = document.createDocumentFragment();
    state.ingredientsCache.forEach((ing) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", ing.name));
      tr.appendChild(el("td", "mono", ing.currentQuantity + " " + ing.unit));
      tr.appendChild(el("td", "mono", ing.reorderLevel + " " + ing.unit));
      tr.appendChild(el("td", "mono", ing.costPerUnit ? ("₹" + ing.costPerUnit) : "—"));
      const alTd = el("td");
      alTd.appendChild(el("span", "pill " + (ing.isLowStock ? "pill-danger" : "pill-ok"), ing.isLowStock ? "LOW STOCK" : "OK"));
      tr.appendChild(alTd);
      iFrag.appendChild(tr);
    });
    $("ingredients-tbody").replaceChildren(iFrag);

    // Expenses table
    const eFrag = document.createDocumentFragment();
    (expRes.items || []).forEach((ex) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", ex.expenseDate));
      tr.appendChild(el("td", "", ex.category));
      tr.appendChild(el("td", "", ex.title));
      tr.appendChild(el("td", "mono", formatINRFromPaise(ex.amountPaise)));
      const actTd = el("td");
      const delBtn = el("button", "btn btn-outline", "Delete");
      delBtn.type = "button";
      delBtn.addEventListener("click", async () => {
        await apiFetch("/api/v1/admin/expenses/" + ex.id, { method: "DELETE" });
        await loadProcurementModule();
      });
      actTd.appendChild(delBtn);
      tr.appendChild(actTd);
      eFrag.appendChild(tr);
    });
    $("expenses-tbody").replaceChildren(eFrag);
  }

  // ============================================================================
  // TAB 7: ORDERS, PAYMENTS, REFUNDS & CUSTOMER FEEDBACK
  // ============================================================================
  async function loadOperationsModule() {
    const oq = new URLSearchParams();
    if ($("ord-filter-status").value) oq.set("status", $("ord-filter-status").value);
    if ($("ord-filter-search").value.trim()) oq.set("search", $("ord-filter-search").value.trim());

    const [ordRes, payRes, fbRes] = await Promise.all([
      apiFetch("/api/v1/admin/orders?" + oq.toString()),
      apiFetch("/api/v1/admin/payments/reconciliation"),
      apiFetch("/api/v1/admin/feedback")
    ]);

    // Orders table
    const oFrag = document.createDocumentFragment();
    (ordRes.items || []).forEach((o) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", o.orderNumber || ("#" + o.id)));
      tr.appendChild(el("td", "", (o.customer && o.customer.fullName) || ("User #" + o.userId)));
      tr.appendChild(el("td", "muted", (o.items || []).map((i) => i.quantity + "× " + i.name).join(", ")));
      tr.appendChild(el("td", "mono", formatINRFromPaise(o.totalPaise)));
      tr.appendChild(el("td", "", o.paymentStatus));
      tr.appendChild(el("td", "", o.status));

      const actTd = el("td", "inline-controls");
      const sel = el("select");
      ["accepted", "preparing", "ready", "collected", "cancelled"].forEach((st) => {
        const op = el("option", "", st.toUpperCase());
        op.value = st;
        sel.appendChild(op);
      });
      const goBtn = el("button", "btn btn-outline", "Apply");
      goBtn.type = "button";
      goBtn.addEventListener("click", async () => {
        try {
          await apiFetch("/api/v1/admin/orders/" + o.id + "/status", {
            method: "POST",
            body: { status: sel.value, reason: "Admin console status override" }
          });
          showAlert("Order " + (o.orderNumber || o.id) + " updated to " + sel.value.toUpperCase(), "success");
          await loadOperationsModule();
        } catch (err) {
          showAlert(err.message, "error");
        }
      });
      actTd.appendChild(sel);
      actTd.appendChild(goBtn);
      tr.appendChild(actTd);
      oFrag.appendChild(tr);
    });
    $("admin-orders-tbody").replaceChildren(oFrag);

    // Payments & discrepancies
    const discBox = $("payment-discrepancies-box");
    const discrepancies = payRes.discrepancies || [];
    discBox.textContent = discrepancies.length
      ? ("⚠ " + discrepancies.length + " gateway reconciliation alert(s) flagged for review.")
      : "✓ Zero gateway webhook discrepancies.";
    discBox.className = discrepancies.length ? "alert-bar error" : "muted";

    const payFrag = document.createDocumentFragment();
    (payRes.payments || []).slice(0, 60).forEach((p) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + p.id));
      tr.appendChild(el("td", "mono", p.orderNumber || ("#" + p.orderId)));
      tr.appendChild(el("td", "mono", p.providerPaymentId || p.providerOrderId || "—"));
      tr.appendChild(el("td", "mono", formatINRFromPaise(p.amountPaise)));
      tr.appendChild(el("td", "", p.status));
      payFrag.appendChild(tr);
    });
    $("payments-tbody").replaceChildren(payFrag);

    // Feedback
    $("feedback-summary-pill").textContent = (fbRes.totalReviews || 0) + " reviews · Avg: " + (fbRes.averageRating || "—") + " ★";
    const fbFrag = document.createDocumentFragment();
    (fbRes.items || []).forEach((rv) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", rv.orderNumber));
      tr.appendChild(el("td", "", rv.customerName));
      tr.appendChild(el("td", "", rv.rating + " / 5 ★"));
      tr.appendChild(el("td", "", (rv.comment || "No comment") + " [" + (rv.itemsOrdered || []).join(", ") + "]"));
      fbFrag.appendChild(tr);
    });
    $("feedback-tbody").replaceChildren(fbFrag);
  }

  // ============================================================================
  // TAB 8: COUNTERS & CANTEEN OPERATING HOURS
  // ============================================================================
  async function loadSettingsModule() {
    const [cfg, cntRes] = await Promise.all([
      apiFetch("/api/v1/admin/settings"),
      apiFetch("/api/v1/admin/counters")
    ]);
    $("set-open-time").value = cfg.canteenOpenTime || "08:30";
    $("set-close-time").value = cfg.canteenCloseTime || "17:30";
    $("set-tax-bps").value = String(cfg.taxRateBps || 0);
    $("set-audit-days").value = String(cfg.auditRetentionDays || 365);
    $("set-paused").checked = Boolean(cfg.orderingPaused);
    $("set-pause-msg").value = cfg.pauseMessage || "";

    const cFrag = document.createDocumentFragment();
    (cntRes.items || []).forEach((c) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + c.id));
      tr.appendChild(el("td", "", c.name + " (" + c.location + ")"));
      tr.appendChild(el("td", "mono", c.openingHours));
      tr.appendChild(el("td", "", String(c.capacityPerSlot)));
      const stTd = el("td");
      stTd.appendChild(el("span", "pill " + (c.isActive ? "pill-ok" : "pill-muted"), c.isActive ? "OPEN" : "CLOSED"));
      tr.appendChild(stTd);
      const actTd = el("td");
      const b = el("button", "btn btn-outline", c.isActive ? "Close Counter" : "Open Counter");
      b.type = "button";
      b.addEventListener("click", async () => {
        await apiFetch("/api/v1/admin/counters/" + c.id, { method: "PATCH", body: { isActive: !c.isActive } });
        await loadSettingsModule();
      });
      actTd.appendChild(b);
      tr.appendChild(actTd);
      cFrag.appendChild(tr);
    });
    $("counters-tbody").replaceChildren(cFrag);
  }

  // ============================================================================
  // TAB 9: DB ADMIN, ENCRYPTED GFS BACKUPS, 4-DAY RETENTION, SECURITY & AUDIT
  // ============================================================================
  async function loadGovernanceModule() {
    const [bkp, ret, dbOv, sec, aud] = await Promise.all([
      apiFetch("/api/v1/admin/backups"),
      apiFetch("/api/v1/admin/retention/status"),
      apiFetch("/api/v1/admin/database/overview"),
      apiFetch("/api/v1/admin/security/overview"),
      apiFetch("/api/v1/admin/audit-logs?limit=80")
    ]);

    // Backups
    $("backup-health-pill").textContent = bkp.backupHealthy ? "Backup Healthy" : "Backup Needed";
    $("backup-health-pill").className = "pill " + (bkp.backupHealthy ? "pill-ok" : "pill-danger");
    const pol = bkp.policy || {};
    $("backup-policy-text").textContent =
      "GFS Retention: " + pol.keepDaily + " Daily / " + pol.keepWeekly + " Weekly / " + pol.keepMonthly +
      " Monthly · Fernet AES-128-CBC + HMAC-SHA256 · Secondary copy: " + (pol.independentSecondaryConfigured ? "Enabled" : "Local");

    const bFrag = document.createDocumentFragment();
    (bkp.recentRuns || []).forEach((r) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", fmtDateTime(r.createdAt)));
      tr.appendChild(el("td", "", String(r.tier).toUpperCase()));
      tr.appendChild(el("td", "mono", Math.round((r.sizeBytes || 0) / 1024) + " KB"));
      tr.appendChild(el("td", "", r.encrypted && r.verified ? "Encrypted + Verified" : r.status));
      tr.appendChild(el("td", "", r.restoreTested ? "Drill Passed" : "—"));
      tr.appendChild(el("td", "mono", String(r.sha256 || "").slice(0, 14) + "…"));
      bFrag.appendChild(tr);
    });
    $("backups-tbody").replaceChildren(bFrag);

    // 4-Day Retention
    $("ret-days").value = String(ret.tempRetentionDays || 4);
    const rFrag = document.createDocumentFragment();
    (ret.recentCleanupRuns || []).forEach((r) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", "#" + r.id));
      tr.appendChild(el("td", "", r.status));
      tr.appendChild(el("td", "", r.backupHealthy ? "Verified" : "Halted (Unhealthy Backup)"));
      tr.appendChild(el("td", "", String(r.archivedDaysCount || 0)));
      tr.appendChild(el("td", "mono", JSON.stringify(r.deletedCounts || {})));
      rFrag.appendChild(tr);
    });
    $("retention-tbody").replaceChildren(rFrag);

    // Database Overview
    $("db-meta-summary").textContent =
      "Engine: " + dbOv.dialect + " · Alembic Head: " + dbOv.alembicRevision +
      " · Tables: " + dbOv.totalTables + " · Live Rows: " + dbOv.totalRowsAcrossTables +
      " · Pool: " + (dbOv.pool && dbOv.pool.poolClass);
    const dbFrag = document.createDocumentFragment();
    (dbOv.tables || []).forEach((t) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", t.tableName));
      tr.appendChild(el("td", "mono", String(t.rowCount)));
      const clTd = el("td");
      const isPerm = t.retentionClass === "permanent_protected";
      clTd.appendChild(el("span", "pill " + (isPerm ? "pill-ok" : "pill-warn"), isPerm ? "PERMANENT PROTECTED" : "4-DAY EPHEMERAL ELIGIBLE"));
      tr.appendChild(clTd);
      dbFrag.appendChild(tr);
    });
    $("db-tables-tbody").replaceChildren(dbFrag);

    // Security Monitor
    const s24 = sec.summary24h || {};
    $("sec-summary-pill").textContent =
      "Failed Logins (24h): " + (s24.failedLogins || 0) + " · Rate Limits: " + (s24.rateLimitBlocks || 0) +
      " · Active Sessions: " + (s24.activeRefreshSessions || 0);
    const secFrag = document.createDocumentFragment();
    (sec.recentEvents || []).forEach((ev) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", fmtDateTime(ev.createdAt)));
      tr.appendChild(el("td", "mono", ev.eventType));
      tr.appendChild(el("td", "", ev.severity));
      tr.appendChild(el("td", "muted", [ev.identifier, ev.ipAddress].filter(Boolean).join(" · ")));
      secFrag.appendChild(tr);
    });
    $("security-tbody").replaceChildren(secFrag);

    // Audit Logs
    const audFrag = document.createDocumentFragment();
    (aud.items || []).forEach((a) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", fmtDateTime(a.createdAt)));
      tr.appendChild(el("td", "", a.actorEmail || ("User #" + (a.actorUserId || "sys"))));
      tr.appendChild(el("td", "mono", a.action));
      tr.appendChild(el("td", "", a.entityType + " #" + a.entityId));
      tr.appendChild(el("td", "mono", JSON.stringify(a.details || {})));
      audFrag.appendChild(tr);
    });
    $("audit-tbody").replaceChildren(audFrag);
  }

  // ============================================================================
  // Wire All Form & Button Event Listeners
  // ============================================================================
  function bindEvents() {
    $("admin-login-form").addEventListener("submit", onAdminLoginSubmit);
    $("btn-admin-logout").addEventListener("click", onAdminLogout);
    $("btn-admin-refresh").addEventListener("click", () => loadActiveTab());

    document.querySelectorAll(".nav-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // BI Filters & Direct Analytics Downloads
    $("bi-filter-form").addEventListener("submit", (e) => {
      e.preventDefault();
      loadBIDashboard().catch((err) => showAlert(err.message, "error"));
    });
    const btnBiPdf = $("btn-bi-download-pdf");
    if (btnBiPdf) btnBiPdf.addEventListener("click", () => triggerBiReportDownload("pdf"));
    const btnBiXlsx = $("btn-bi-download-xlsx");
    if (btnBiXlsx) btnBiXlsx.addEventListener("click", () => triggerBiReportDownload("xlsx"));
    const btnBiCsv = $("btn-bi-download-csv");
    if (btnBiCsv) btnBiCsv.addEventListener("click", () => triggerBiReportDownload("csv"));

    // Reports
    $("btn-rep-preview").addEventListener("click", () => previewSelectedReport().catch((err) => showAlert(err.message, "error")));
    $("btn-rep-pdf").addEventListener("click", () => triggerReportDownload("pdf"));
    $("btn-rep-xlsx").addEventListener("click", () => triggerReportDownload("xlsx"));
    $("btn-rep-csv").addEventListener("click", () => triggerReportDownload("csv"));

    $("rep-schedule-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/reports/schedule", {
          method: "PUT",
          body: { dailyReportEmails: $("rep-emails").value.trim() }
        });
        showAlert("Automated daily report email schedule saved.", "success");
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("btn-rep-generate-daily").addEventListener("click", async () => {
      try {
        const res = await apiFetch("/api/v1/admin/reports/generate-daily", { method: "POST", body: {} });
        showAlert("Generated " + res.generatedCount + " automated daily reports for " + res.date + ".", "success");
        await loadReportsStudio();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // AI BI
    $("aibi-days").addEventListener("change", () => loadAIBIDashboard().catch((err) => showAlert(err.message, "error")));
    $("btn-aibi-run-forecast").addEventListener("click", async () => {
      try {
        await apiFetch("/api/v1/ai/forecasts/run", { method: "POST", body: {} });
        showAlert("Recomputed today's AI slot demand forecasts.", "success");
        await loadAIBIDashboard();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Users & Permissions
    $("btn-usr-search").addEventListener("click", () => loadUsersModule());
    $("user-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const cid = Number($("usr-counter-id").value || 0);
        await apiFetch("/api/v1/admin/users", {
          method: "POST",
          body: {
            fullName: $("usr-name").value.trim(),
            email: $("usr-email").value.trim(),
            phone: $("usr-phone").value.trim() || null,
            temporaryPassword: $("usr-pass").value,
            role: $("usr-role").value,
            counterIds: cid > 0 ? [cid] : []
          }
        });
        e.target.reset();
        showAlert("Account provisioned.", "success");
        await loadUsersModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("perm-user-select").addEventListener("change", () => {
      const uid = Number($("perm-user-select").value || 0);
      const u = state.usersCache.find((x) => x.id === uid);
      if (u && u.staffPermissions) {
        $("perm-accept").checked = Boolean(u.staffPermissions.canAcceptOrders);
        $("perm-reject").checked = Boolean(u.staffPermissions.canRejectOrders);
        $("perm-prep").checked = Boolean(u.staffPermissions.canUpdatePrep);
        $("perm-pickup").checked = Boolean(u.staffPermissions.canVerifyPickup);
        $("perm-avail").checked = Boolean(u.staffPermissions.canManageAvailability);
        $("perm-inv").checked = Boolean(u.staffPermissions.canManageInventory);
      }
    });

    $("staff-perm-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const uid = Number($("perm-user-select").value || 0);
      if (!uid) {
        showAlert("Select a staff member first.", "error");
        return;
      }
      try {
        await apiFetch("/api/v1/admin/users/" + uid + "/permissions", {
          method: "PUT",
          body: {
            canAcceptOrders: $("perm-accept").checked,
            canRejectOrders: $("perm-reject").checked,
            canUpdatePrep: $("perm-prep").checked,
            canVerifyPickup: $("perm-pickup").checked,
            canManageAvailability: $("perm-avail").checked,
            canManageInventory: $("perm-inv").checked
          }
        });
        showAlert("Granular staff permissions saved.", "success");
        await loadUsersModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Menu, Categories & Discounts
    $("cat-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/categories", {
          method: "POST",
          body: { name: $("cat-name").value.trim(), sortOrder: Number($("cat-sort").value || 0), itemType: "food" }
        });
        e.target.reset();
        showAlert("Category created.", "success");
        await loadMenuModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("item-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const tags = $("itm-tags").value.split(",").map((s) => s.trim()).filter(Boolean);
        await apiFetch("/api/v1/admin/menu-items", {
          method: "POST",
          body: {
            categoryId: Number($("itm-cat").value),
            counterId: $("itm-counter").value ? Number($("itm-counter").value) : null,
            name: $("itm-name").value.trim(),
            description: $("itm-desc").value.trim() || null,
            pricePaise: Math.round(Number($("itm-price").value) * 100),
            costPaise: Math.round(Number($("itm-cost").value || 0) * 100),
            stockQuantity: Number($("itm-stock").value || 0),
            preparationMinutes: Number($("itm-prep").value || 10),
            dietaryTags: tags
          }
        });
        e.target.reset();
        showAlert("Menu item created.", "success");
        await loadMenuModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("disc-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/discounts", {
          method: "POST",
          body: {
            code: $("dsc-code").value.trim().toUpperCase(),
            discountPercent: Number($("dsc-pct").value || 0),
            discountPaise: Math.round(Number($("dsc-flat").value || 0) * 100),
            minOrderPaise: Math.round(Number($("dsc-min").value || 0) * 100),
            usageLimit: $("dsc-limit").value ? Number($("dsc-limit").value) : null,
            description: $("dsc-desc").value.trim() || null
          }
        });
        e.target.reset();
        showAlert("Promotional offer created.", "success");
        await loadMenuModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Procurement
    $("sup-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/suppliers", {
          method: "POST",
          body: {
            name: $("sup-name").value.trim(),
            contactName: $("sup-contact").value.trim() || null,
            phone: $("sup-phone").value.trim() || null,
            email: $("sup-email").value.trim() || null
          }
        });
        e.target.reset();
        showAlert("Supplier added.", "success");
        await loadProcurementModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("po-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const kind = $("po-kind").value;
        const line = {
          quantity: Number($("po-qty").value),
          unitCostPaise: Math.round(Number($("po-unit-cost").value || 0) * 100),
          expiryDate: $("po-expiry").value || null
        };
        if (kind === "menuItem") line.menuItemId = Number($("po-menuitem").value);
        else line.ingredientId = Number($("po-ingredient").value);

        await apiFetch("/api/v1/admin/purchase-orders", {
          method: "POST",
          body: {
            supplierId: Number($("po-supplier").value),
            lines: [line],
            notes: $("po-notes").value.trim() || null,
            markOrdered: true
          }
        });
        showAlert("Purchase order issued.", "success");
        await loadProcurementModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("ing-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/ingredients", {
          method: "POST",
          body: {
            name: $("ing-name").value.trim(),
            unit: $("ing-unit").value.trim(),
            currentQuantity: Number($("ing-qty").value || 0),
            reorderLevel: Number($("ing-reorder").value || 0),
            costPerUnit: $("ing-cost").value ? Number($("ing-cost").value) : null
          }
        });
        e.target.reset();
        showAlert("Raw ingredient added.", "success");
        await loadProcurementModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("exp-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/expenses", {
          method: "POST",
          body: {
            title: $("exp-title").value.trim(),
            category: $("exp-cat").value,
            amountPaise: Math.round(Number($("exp-amount").value) * 100),
            expenseDate: $("exp-date").value || null
          }
        });
        e.target.reset();
        showAlert("Expense recorded.", "success");
        await loadProcurementModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Operations & Refunds
    $("btn-ord-filter").addEventListener("click", () => loadOperationsModule());
    $("refund-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const payId = Number($("ref-payment-id").value);
        const amtVal = $("ref-amount").value ? Math.round(Number($("ref-amount").value) * 100) : null;
        await apiFetch("/api/v1/payments/" + payId + "/refunds", {
          method: "POST",
          body: { amountPaise: amtVal, reason: $("ref-reason").value.trim() }
        });
        e.target.reset();
        showAlert("Gateway refund initiated.", "success");
        await loadOperationsModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Settings & Counters
    $("global-settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/settings", {
          method: "PATCH",
          body: {
            canteenOpenTime: $("set-open-time").value.trim(),
            canteenCloseTime: $("set-close-time").value.trim(),
            taxRateBps: Number($("set-tax-bps").value || 0),
            auditRetentionDays: Number($("set-audit-days").value || 365),
            orderingPaused: $("set-paused").checked,
            pauseMessage: $("set-pause-msg").value.trim()
          }
        });
        showAlert("Operating hours and system settings updated.", "success");
        await loadSettingsModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("counter-create-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiFetch("/api/v1/admin/counters", {
          method: "POST",
          body: {
            name: $("cnt-name").value.trim(),
            location: $("cnt-loc").value.trim(),
            capacityPerSlot: Number($("cnt-cap").value || 25),
            openingHours: $("cnt-hours").value.trim()
          }
        });
        e.target.reset();
        showAlert("Pickup counter added.", "success");
        await loadSettingsModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    // Governance: Backups, Retention, DB Analyze, Session Revoke
    $("btn-backup-run").addEventListener("click", async () => {
      try {
        const r = await apiFetch("/api/v1/admin/backups/run", { method: "POST", body: {} });
        showAlert("Encrypted GFS backup created & verified (" + Math.round((r.sizeBytes || 0) / 1024) + " KB).", "success");
        await loadGovernanceModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("btn-backup-drill").addEventListener("click", async () => {
      try {
        const r = await apiFetch("/api/v1/admin/backups/restore-test", { method: "POST", body: {} });
        showAlert("Non-destructive restore drill PASSED (" + (r.verification && r.verification.engine) + ").", "success");
        await loadGovernanceModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("btn-ret-dryrun").addEventListener("click", async () => {
      try {
        const r = await apiFetch("/api/v1/admin/retention/run", {
          method: "POST",
          body: { retentionDays: Number($("ret-days").value || 4), dryRun: true }
        });
        $("ret-result-box").textContent = "Dry-Run Eligible Temporary Counts: " + JSON.stringify(r.eligibleCounts || {});
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("btn-ret-execute").addEventListener("click", async () => {
      try {
        const r = await apiFetch("/api/v1/admin/retention/run", {
          method: "POST",
          body: { retentionDays: Number($("ret-days").value || 4), dryRun: false }
        });
        if (r.status === "aborted_unhealthy_backup") {
          showAlert("Safety Gate Triggered: Cleanup halted because no verified recent encrypted backup was found. Create a GFS backup first.", "error");
        } else {
          showAlert("4-day temporary cleanup completed safely. Permanent business records preserved.", "success");
        }
        await loadGovernanceModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("btn-db-analyze").addEventListener("click", async () => {
      try {
        const r = await apiFetch("/api/v1/admin/database/analyze", { method: "POST", body: {} });
        showAlert("Database ANALYZE & integrity check completed: " + r.integrityStatus, "success");
        await loadGovernanceModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });

    $("sec-revoke-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const uid = Number($("sec-revoke-uid").value);
        const r = await apiFetch("/api/v1/admin/security/users/" + uid + "/revoke-sessions", { method: "POST", body: {} });
        showAlert("Revoked " + r.revokedRefreshSessions + " refresh session(s) for User #" + uid + ".", "success");
        await loadGovernanceModule();
      } catch (err) {
        showAlert(err.message, "error");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    verifySessionOnBoot();
  });
})();
