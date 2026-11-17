"use strict";
(() => {
  const configured =
    typeof window.__COLLEGEBITS_CONFIG__ === "object" && window.__COLLEGEBITS_CONFIG__
      ? window.__COLLEGEBITS_CONFIG__.apiBaseUrl
      : "";
  const rawBase = String(configured || "").trim().replace(/\/+$/, "");

  window.__COLLEGEBITS_CONFIG__ = Object.freeze({ apiBaseUrl: rawBase });

  window.collegebitsApiUrl = path => {
    if (!path || !rawBase || /^https?:\/\//i.test(path)) return path;
    return rawBase + (path.startsWith("/") ? path : "/" + path);
  };

  window.collegebitsWsUrl = path => {
    const normalizedPath = path.startsWith("/") ? path : "/" + path;
    if (rawBase) {
      return rawBase.replace(/^http/i, "ws") + normalizedPath;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${normalizedPath}`;
  };

  if (rawBase && typeof window.fetch === "function") {
    const origFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (typeof input === "string" && (input === "/api" || input.startsWith("/api/"))) {
        return origFetch(window.collegebitsApiUrl(input), init);
      }
      return origFetch(input, init);
    };
  }
})();
