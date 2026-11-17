"use strict";
const CACHE_NAME = "ace-canteen-shell-v7";
const OFFLINE_URL = "/offline.html";
const APP_SHELL = [
  "/customer/",
  OFFLINE_URL,
  "/manifest.webmanifest",
  "/css/style.css",
  "/css/v1-platform.css",
  "/js/app.js",
  "/js/v1-platform.js",
  "/assets/ace-logo.png",
  "/assets/canteen-hero.png",
  "/assets/icon-192.png",
  "/assets/icon-512.png",
  "/assets/icon-maskable-512.png",
  "/assets/apple-touch-icon.png"
];

const PRIVATE_OR_API_PATH = url =>
  url.pathname === "/api" ||
  url.pathname.startsWith("/api/") ||
  url.pathname === "/ws" ||
  url.pathname.startsWith("/ws/") ||
  url.pathname === "/docs" ||
  url.pathname.startsWith("/docs/") ||
  url.pathname === "/openapi.json" ||
  url.pathname.startsWith("/auth/") ||
  url.pathname === "/pickup" ||
  url.pathname.startsWith("/pickup/") ||
  url.pathname === "/qr" ||
  url.pathname.startsWith("/qr/");

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(APP_SHELL);
  })());
});

self.addEventListener("message", event => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter(key => key.startsWith("ace-canteen-shell-") && key !== CACHE_NAME)
        .map(key => caches.delete(key))
    );
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET" || request.headers.get("authorization")) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || PRIVATE_OR_API_PATH(url)) return;

  if (request.mode === "navigate") {
    if (url.pathname !== "/customer" && url.pathname !== "/customer/") return;
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put("/customer/", response.clone());
        }
        return response;
      } catch {
        return (await caches.match(OFFLINE_URL)) ||
          (await caches.match("/customer/")) ||
          new Response(
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>CollegeBits offline</title><main><h1>CollegeBits is offline</h1><p>Reconnect to load live catalogue, account, order, QR pickup and payment information.</p></main></html>",
            { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }, status: 503 }
          );
      }
    })());
    return;
  }

  if (!APP_SHELL.includes(url.pathname) || url.search) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok && response.type === "basic") {
      await cache.put(request, response.clone());
    }
    return response;
  })());
});

self.addEventListener("push", event => {
  let payload = {
    title: "CollegeBits Canteen",
    body: "You have an order update.",
    url: "/customer/#my-orders-section",
    tag: "canteen-order-update"
  };
  if (event.data) {
    try {
      const parsed = event.data.json();
      payload = {
        ...payload,
        ...parsed,
        url: parsed.url || (parsed.data && parsed.data.url) || payload.url,
        tag: parsed.tag || (parsed.data && parsed.data.tag) || payload.tag
      };
    } catch {
      payload.body = event.data.text() || payload.body;
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: payload.tag || "canteen-order-update",
      icon: "/assets/icon-192.png",
      badge: "/assets/icon-192.png",
      data: { url: payload.url || "/customer/#my-orders-section" }
    })
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/customer/#my-orders-section";
  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of allClients) {
      if (client.url.startsWith(`${self.location.origin}/customer`) && "focus" in client) {
        await client.focus();
        try {
          client.postMessage({ type: "NAVIGATE_ORDER", url: targetUrl });
        } catch (_) {}
        return;
      }
    }
    if (self.clients.openWindow) {
      await self.clients.openWindow(targetUrl);
    }
  })());
});
