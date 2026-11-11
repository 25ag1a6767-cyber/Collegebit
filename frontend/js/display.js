(function () {
  "use strict";

  let soundEnabled = true;
  let lastKnownReadySet = new Set();
  let audioCtx = null;

  function playChime() {
    if (!soundEnabled) return;
    try {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === "suspended") {
        audioCtx.resume();
      }
      const now = audioCtx.currentTime;
      const osc1 = audioCtx.createOscillator();
      const gain1 = audioCtx.createGain();
      osc1.type = "sine";
      osc1.frequency.setValueAtTime(587.33, now); // D5
      osc1.frequency.exponentialRampToValueAtTime(880, now + 0.15); // A5
      gain1.gain.setValueAtTime(0.25, now);
      gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.6);
      osc1.connect(gain1);
      gain1.connect(audioCtx.destination);
      osc1.start(now);
      osc1.stop(now + 0.6);
    } catch (_) {}
  }

  function updateClock() {
    const d = new Date();
    const clockEl = document.getElementById("clock");
    if (clockEl) {
      clockEl.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
  }
  setInterval(updateClock, 1000);
  updateClock();

  const chimeBtn = document.getElementById("chime-toggle");
  if (chimeBtn) {
    chimeBtn.addEventListener("click", () => {
      soundEnabled = !soundEnabled;
      chimeBtn.textContent = soundEnabled ? "🔔 Sound: ON" : "🔕 Sound: OFF";
      chimeBtn.className = soundEnabled ? "btn-active" : "";
      if (soundEnabled && !audioCtx) {
        try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) {}
      }
    });
  }

  const fsBtn = document.getElementById("fullscreen-btn");
  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    });
  }

  async function fetchDisplayData() {
    const sel = document.getElementById("counter-select");
    const counterVal = sel ? sel.value : "";
    const q = counterVal ? "?counterId=" + encodeURIComponent(counterVal) : "";
    try {
      const res = await fetch("/api/v1/queue/display-board" + q, { cache: "no-store" });
      if (!res.ok) return;
      const body = await res.json();
      const data = body.data || body;
      renderBoard(data);
    } catch (_) {}
  }

  function renderBoard(data) {
    const sel = document.getElementById("counter-select");
    if (sel && sel.options.length <= 1 && Array.isArray(data.counters)) {
      data.counters.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = String(c.id);
        opt.textContent = c.name + (c.location ? " (" + c.location + ")" : "");
        sel.appendChild(opt);
      });
    }

    const readyTokens = data.readyTokens || [];
    const prepTokens = data.preparingTokens || [];
    const collectedTokens = data.recentlyCollected || [];

    const currentReadyIds = new Set(readyTokens.map((t) => t.orderId || t.tokenNumber));
    let hasNewReady = false;
    if (lastKnownReadySet.size > 0) {
      for (const id of currentReadyIds) {
        if (!lastKnownReadySet.has(id)) {
          hasNewReady = true;
          break;
        }
      }
    }
    lastKnownReadySet = currentReadyIds;
    if (hasNewReady) {
      playChime();
    }

    const readyCountEl = document.getElementById("ready-count");
    if (readyCountEl) {
      readyCountEl.textContent = `${readyTokens.length} Order${readyTokens.length === 1 ? "" : "s"}`;
    }
    const readyGrid = document.getElementById("ready-tokens-grid");
    if (readyGrid) {
      if (!readyTokens.length) {
        readyGrid.textContent = "";
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "No orders ready for collection right now.";
        readyGrid.appendChild(empty);
      } else {
        readyGrid.textContent = "";
        readyTokens.forEach((t) => {
          const card = document.createElement("div");
          card.className = "token-card ready flash";

          const numEl = document.createElement("div");
          numEl.className = "token-number";
          numEl.textContent = t.tokenDisplay || ("#" + t.tokenNumber);

          const ctrEl = document.createElement("div");
          ctrEl.className = "token-counter";
          ctrEl.textContent = t.counterName || "Main Counter";

          const timeEl = document.createElement("div");
          timeEl.className = "token-time";
          timeEl.textContent = "Ready " + (t.readyMinsAgo <= 0 ? "just now" : t.readyMinsAgo + "m ago");

          card.appendChild(numEl);
          card.appendChild(ctrEl);
          card.appendChild(timeEl);
          readyGrid.appendChild(card);
        });
      }
    }

    const prepCountEl = document.getElementById("prep-count");
    if (prepCountEl) {
      prepCountEl.textContent = `${prepTokens.length} Order${prepTokens.length === 1 ? "" : "s"}`;
    }
    const prepGrid = document.getElementById("prep-tokens-grid");
    if (prepGrid) {
      if (!prepTokens.length) {
        prepGrid.textContent = "";
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "Kitchen queue is clear.";
        prepGrid.appendChild(empty);
      } else {
        prepGrid.textContent = "";
        prepTokens.forEach((t) => {
          const card = document.createElement("div");
          card.className = "token-card prep";

          const numEl = document.createElement("div");
          numEl.className = "token-number";
          numEl.textContent = t.tokenDisplay || ("#" + t.tokenNumber);

          const ctrEl = document.createElement("div");
          ctrEl.className = "token-counter";
          ctrEl.textContent = t.counterName || "Main Counter";

          const timeEl = document.createElement("div");
          timeEl.className = "token-time";
          timeEl.textContent = "In Preparation";

          card.appendChild(numEl);
          card.appendChild(ctrEl);
          card.appendChild(timeEl);
          prepGrid.appendChild(card);
        });
      }
    }

    const colList = document.getElementById("collected-tokens-list");
    if (colList) {
      if (!collectedTokens.length) {
        colList.textContent = "";
        const notice = document.createElement("span");
        notice.className = "privacy-notice";
        notice.textContent = "None yet today";
        colList.appendChild(notice);
      } else {
        colList.textContent = "";
        collectedTokens.slice(0, 10).forEach((t) => {
          const badge = document.createElement("span");
          badge.className = "collected-badge";
          badge.textContent = t.tokenDisplay || ("#" + t.tokenNumber);
          badge.title = `${t.counterName || "Counter"} - Handed Over`;
          colList.appendChild(badge);
        });
      }
    }
  }

  const selEl = document.getElementById("counter-select");
  if (selEl) {
    selEl.addEventListener("change", fetchDisplayData);
  }

  function connectWs() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/api/v1/ws`;
    let ws;
    try {
      ws = new WebSocket(wsUrl);
      ws.onmessage = function (ev) {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.topic === "queue.public" || msg.topic === "order.ready" || msg.topic === "order.collected") {
            fetchDisplayData();
          }
        } catch (_) {}
      };
      ws.onclose = function () {
        setTimeout(connectWs, 4000);
      };
    } catch (_) {
      setTimeout(connectWs, 5000);
    }
  }

  connectWs();
  fetchDisplayData();
  setInterval(fetchDisplayData, 5000);
})();
