
(function () {
  "use strict";

  const PALETTE = [
    "#0f766e",
    "#2563eb",
    "#d97706",
    "#dc2626",
    "#7c3aed",
    "#0891b2",
    "#16a34a",
    "#db2777",
    "#4f46e5",
    "#ea580c"
  ];

  const SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach((k) => {
        if (attrs[k] !== undefined && attrs[k] !== null) {
          el.setAttribute(k, String(attrs[k]));
        }
      });
    }
    return el;
  }

  function htmlEl(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function defaultFmt(v) {
    const n = Number(v || 0);
    if (Math.abs(n) >= 100000) return (n / 1000).toFixed(1) + "k";
    if (Number.isInteger(n)) return n.toLocaleString("en-IN");
    return n.toFixed(2);
  }

  function renderEmpty(container, message) {
    container.replaceChildren(htmlEl("p", "cb-chart-empty", message || "No records in selected filter window."));
  }

  function buildLegend(seriesList) {
    const wrap = htmlEl("div", "cb-chart-legend");
    seriesList.forEach((s, idx) => {
      const item = htmlEl("span", "cb-legend-item");
      const dotSvg = svgEl("svg", { width: "10", height: "10", viewBox: "0 0 10 10", "aria-hidden": "true" });
      dotSvg.appendChild(svgEl("circle", { cx: "5", cy: "5", r: "4.5", fill: s.color || PALETTE[idx % PALETTE.length] }));
      item.appendChild(dotSvg);
      item.appendChild(htmlEl("span", "cb-legend-text", s.name));
      wrap.appendChild(item);
    });
    return wrap;
  }

  function renderLineChart(container, opts) {
    if (!container) return;
    const labels = (opts && opts.labels) || [];
    const series = (opts && opts.series) || [];
    const fmt = (opts && opts.formatValue) || defaultFmt;
    if (!labels.length || !series.length) {
      renderEmpty(container, opts && opts.emptyMessage);
      return;
    }

    let minVal = 0;
    let maxVal = 0;
    series.forEach((s) => {
      (s.values || []).forEach((v) => {
        const num = Number(v || 0);
        if (num < minVal) minVal = num;
        if (num > maxVal) maxVal = num;
      });
    });
    if (maxVal === minVal) {
      maxVal = minVal + 10;
    }

    const W = 640;
    const H = 235;
    const padL = 62;
    const padR = 18;
    const padT = 18;
    const padB = 42;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`,
      class: "cb-svg-chart",
      role: "img",
      "aria-label": (opts && opts.title) || "Line chart"
    });

    const yFor = (val) => padT + plotH - ((Number(val || 0) - minVal) / (maxVal - minVal)) * plotH;
    const xFor = (idx) => (labels.length === 1 ? padL + plotW / 2 : padL + (idx / (labels.length - 1)) * plotW);

    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const v = minVal + ((maxVal - minVal) * i) / steps;
      const y = yFor(v);
      svg.appendChild(svgEl("line", { x1: padL, y1: y.toFixed(1), x2: W - padR, y2: y.toFixed(1), stroke: "#e2e8f0", "stroke-width": "1" }));
      const lbl = svgEl("text", { x: padL - 6, y: (y + 3.5).toFixed(1), "text-anchor": "end", fill: "#64748b", "font-size": "10.5" });
      lbl.textContent = fmt(v);
      svg.appendChild(lbl);
    }

    if (minVal < 0 && maxVal > 0) {
      const y0 = yFor(0);
      svg.appendChild(svgEl("line", { x1: padL, y1: y0.toFixed(1), x2: W - padR, y2: y0.toFixed(1), stroke: "#94a3b8", "stroke-dasharray": "4 3", "stroke-width": "1.2" }));
    }

    const stride = Math.max(1, Math.ceil(labels.length / 7));
    labels.forEach((lblText, idx) => {
      if (idx % stride !== 0 && idx !== labels.length - 1) return;
      const x = xFor(idx);
      const t = svgEl("text", { x: x.toFixed(1), y: H - 14, "text-anchor": "middle", fill: "#64748b", "font-size": "10" });
      t.textContent = String(lblText).slice(-10);
      svg.appendChild(t);
    });

    series.forEach((s, sIdx) => {
      const color = s.color || PALETTE[sIdx % PALETTE.length];
      const vals = s.values || [];
      const pts = labels.map((_, idx) => `${xFor(idx).toFixed(1)},${yFor(vals[idx] || 0).toFixed(1)}`);

      if (series.length === 1 && minVal >= 0 && pts.length >= 2) {
        const baseY = yFor(0).toFixed(1);
        const areaPts = `${xFor(0).toFixed(1)},${baseY} ${pts.join(" ")} ${xFor(labels.length - 1).toFixed(1)},${baseY}`;
        svg.appendChild(svgEl("polygon", { points: areaPts, fill: color, opacity: "0.10" }));
      }

      if (pts.length >= 2) {
        svg.appendChild(
          svgEl("polyline", {
            points: pts.join(" "),
            fill: "none",
            stroke: color,
            "stroke-width": "2.4",
            "stroke-linecap": "round",
            "stroke-linejoin": "round"
          })
        );
      }

      const showDots = labels.length <= 45;
      labels.forEach((lblText, idx) => {
        if (!showDots && idx % stride !== 0 && idx !== labels.length - 1) return;
        const cx = xFor(idx);
        const cy = yFor(vals[idx] || 0);
        const dot = svgEl("circle", { cx: cx.toFixed(1), cy: cy.toFixed(1), r: "3.6", fill: color, stroke: "#ffffff", "stroke-width": "1.2" });
        const title = svgEl("title");
        title.textContent = `${lblText} — ${s.name}: ${fmt(vals[idx] || 0)}`;
        dot.appendChild(title);
        svg.appendChild(dot);
      });
    });

    const frag = document.createDocumentFragment();
    if (series.length > 1) frag.appendChild(buildLegend(series));
    frag.appendChild(svg);
    container.replaceChildren(frag);
  }

  function renderBarChart(container, opts) {
    if (!container) return;
    const labels = (opts && opts.labels) || [];
    const series = (opts && opts.series) || [];
    const fmt = (opts && opts.formatValue) || defaultFmt;
    const horizontal = Boolean(opts && opts.horizontal);

    if (!labels.length || !series.length) {
      renderEmpty(container, opts && opts.emptyMessage);
      return;
    }

    let maxVal = 0;
    let minVal = 0;
    series.forEach((s) => {
      (s.values || []).forEach((v) => {
        const n = Number(v || 0);
        if (n > maxVal) maxVal = n;
        if (n < minVal) minVal = n;
      });
    });
    if (maxVal === 0 && minVal === 0) maxVal = 10;

    if (horizontal) {
      const rowH = 26;
      const padL = 148;
      const padR = 64;
      const padT = 12;
      const padB = 14;
      const W = 640;
      const H = Math.max(150, padT + padB + labels.length * rowH);
      const plotW = W - padL - padR;
      const vals = series[0].values || [];
      const barColor = series[0].color || PALETTE[0];
      const effMax = Math.max(1, maxVal);

      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "cb-svg-chart", role: "img" });
      labels.forEach((lblText, idx) => {
        const y = padT + idx * rowH;
        const v = Number(vals[idx] || 0);
        const bw = Math.max(2, (Math.max(0, v) / effMax) * plotW);

        const nameTxt = svgEl("text", { x: padL - 8, y: y + 15, "text-anchor": "end", fill: "#1e293b", "font-size": "11" });
        nameTxt.textContent = String(lblText).length > 21 ? String(lblText).slice(0, 20) + "…" : String(lblText);
        svg.appendChild(nameTxt);

        svg.appendChild(svgEl("rect", { x: padL, y: y + 4, width: plotW, height: 15, rx: "4", fill: "#f1f5f9" }));
        const bar = svgEl("rect", { x: padL, y: y + 4, width: bw.toFixed(1), height: 15, rx: "4", fill: barColor });
        const tt = svgEl("title");
        tt.textContent = `${lblText}: ${fmt(v)}`;
        bar.appendChild(tt);
        svg.appendChild(bar);

        const valTxt = svgEl("text", { x: (padL + bw + 6).toFixed(1), y: y + 15, fill: "#334155", "font-size": "10.5", "font-weight": "600" });
        valTxt.textContent = fmt(v);
        svg.appendChild(valTxt);
      });
      container.replaceChildren(svg);
      return;
    }

    const W = 640;
    const H = 235;
    const padL = 58;
    const padR = 16;
    const padT = 18;
    const padB = 46;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const effMax = Math.max(1, maxVal);

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "cb-svg-chart", role: "img" });
    for (let i = 0; i <= 4; i++) {
      const v = (effMax * i) / 4;
      const y = padT + plotH - (i / 4) * plotH;
      svg.appendChild(svgEl("line", { x1: padL, y1: y.toFixed(1), x2: W - padR, y2: y.toFixed(1), stroke: "#e2e8f0", "stroke-width": "1" }));
      const yt = svgEl("text", { x: padL - 6, y: (y + 3.5).toFixed(1), "text-anchor": "end", fill: "#64748b", "font-size": "10.5" });
      yt.textContent = fmt(v);
      svg.appendChild(yt);
    }

    const groupW = plotW / labels.length;
    const innerW = groupW * 0.72;
    const barW = Math.max(3, innerW / series.length);
    const stride = Math.max(1, Math.ceil(labels.length / 8));

    labels.forEach((lblText, gIdx) => {
      const gx = padL + gIdx * groupW + (groupW - innerW) / 2;
      series.forEach((s, sIdx) => {
        const v = Math.max(0, Number((s.values || [])[gIdx] || 0));
        const bh = Math.max(1.5, (v / effMax) * plotH);
        const bx = gx + sIdx * barW;
        const by = padT + plotH - bh;
        const color = s.color || PALETTE[sIdx % PALETTE.length];
        const rect = svgEl("rect", {
          x: bx.toFixed(1),
          y: by.toFixed(1),
          width: Math.max(2, barW - 1.5).toFixed(1),
          height: bh.toFixed(1),
          rx: "3",
          fill: color
        });
        const tt = svgEl("title");
        tt.textContent = `${lblText} — ${s.name}: ${fmt(v)}`;
        rect.appendChild(tt);
        svg.appendChild(rect);
      });

      if (gIdx % stride === 0 || gIdx === labels.length - 1) {
        const lx = padL + gIdx * groupW + groupW / 2;
        const xt = svgEl("text", { x: lx.toFixed(1), y: H - 16, "text-anchor": "middle", fill: "#64748b", "font-size": "10" });
        xt.textContent = String(lblText).length > 11 ? String(lblText).slice(0, 10) + "…" : String(lblText);
        svg.appendChild(xt);
      }
    });

    const frag = document.createDocumentFragment();
    if (series.length > 1) frag.appendChild(buildLegend(series));
    frag.appendChild(svg);
    container.replaceChildren(frag);
  }

  function renderDonutChart(container, opts) {
    if (!container) return;
    const items = ((opts && opts.items) || []).filter((x) => Number(x.value || 0) > 0);
    const fmt = (opts && opts.formatValue) || defaultFmt;
    if (!items.length) {
      renderEmpty(container, (opts && opts.emptyMessage) || "No category sales in selected period.");
      return;
    }

    const total = items.reduce((acc, it) => acc + Number(it.value || 0), 0);
    const cx = 105;
    const cy = 105;
    const rOuter = 82;
    const rInner = 48;

    const svg = svgEl("svg", { viewBox: "0 0 210 210", class: "cb-donut-svg", role: "img" });
    let startAngle = -Math.PI / 2;

    if (items.length === 1) {
      const c = items[0].color || PALETTE[0];
      svg.appendChild(svgEl("circle", { cx, cy, r: rOuter, fill: c }));
      svg.appendChild(svgEl("circle", { cx, cy, r: rInner, fill: "#ffffff" }));
    } else {
      items.forEach((it, idx) => {
        const val = Number(it.value || 0);
        const sliceAngle = (val / total) * Math.PI * 2;
        const endAngle = startAngle + sliceAngle;
        const largeArc = sliceAngle > Math.PI ? 1 : 0;

        const x1 = cx + rOuter * Math.cos(startAngle);
        const y1 = cy + rOuter * Math.sin(startAngle);
        const x2 = cx + rOuter * Math.cos(endAngle);
        const y2 = cy + rOuter * Math.sin(endAngle);
        const x3 = cx + rInner * Math.cos(endAngle);
        const y3 = cy + rInner * Math.sin(endAngle);
        const x4 = cx + rInner * Math.cos(startAngle);
        const y4 = cy + rInner * Math.sin(startAngle);

        const d = [
          `M ${x1.toFixed(2)} ${y1.toFixed(2)}`,
          `A ${rOuter} ${rOuter} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`,
          `L ${x3.toFixed(2)} ${y3.toFixed(2)}`,
          `A ${rInner} ${rInner} 0 ${largeArc} 0 ${x4.toFixed(2)} ${y4.toFixed(2)}`,
          "Z"
        ].join(" ");

        const path = svgEl("path", { d, fill: it.color || PALETTE[idx % PALETTE.length], stroke: "#ffffff", "stroke-width": "1.5" });
        const tt = svgEl("title");
        tt.textContent = `${it.label}: ${fmt(val)} (${((val * 100) / total).toFixed(1)}%)`;
        path.appendChild(tt);
        svg.appendChild(path);
        startAngle = endAngle;
      });
    }

    const centerTop = svgEl("text", { x: cx, y: cy - 4, "text-anchor": "middle", fill: "#64748b", "font-size": "10" });
    centerTop.textContent = "TOTAL";
    const centerVal = svgEl("text", { x: cx, y: cy + 13, "text-anchor": "middle", fill: "#0f172a", "font-size": "12.5", "font-weight": "700" });
    centerVal.textContent = fmt(total);
    svg.appendChild(centerTop);
    svg.appendChild(centerVal);

    const layout = htmlEl("div", "cb-donut-layout");
    layout.appendChild(svg);

    const list = htmlEl("div", "cb-donut-legend");
    items.slice(0, 8).forEach((it, idx) => {
      const row = htmlEl("div", "cb-donut-legend-row");
      const left = htmlEl("span", "cb-legend-item");
      const dotSvg = svgEl("svg", { width: "10", height: "10", viewBox: "0 0 10 10" });
      dotSvg.appendChild(svgEl("circle", { cx: "5", cy: "5", r: "4.5", fill: it.color || PALETTE[idx % PALETTE.length] }));
      left.appendChild(dotSvg);
      left.appendChild(htmlEl("span", "cb-legend-text", it.label));
      const pct = ((Number(it.value || 0) * 100) / total).toFixed(1) + "%";
      const right = htmlEl("strong", "cb-donut-val", `${fmt(it.value)} (${pct})`);
      row.appendChild(left);
      row.appendChild(right);
      list.appendChild(row);
    });
    layout.appendChild(list);
    container.replaceChildren(layout);
  }

  window.CampusBiteCharts = {
    PALETTE,
    renderLineChart,
    renderBarChart,
    renderDonutChart
  };
})();
