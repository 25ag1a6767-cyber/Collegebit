"use strict";

(() => {
  const hash = String(window.location.hash || "");
  if (hash.startsWith("#reset=") || hash.startsWith("#verify-email=")) {
    window.location.replace(`/customer/${hash}`);
  }
})();
