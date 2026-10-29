// Theme initialization before rendering to prevent flash of wrong theme.
"use strict";
(() => {
  const KEY = "mail-cadence-theme";
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
    }
  } catch {
    // Ignore localStorage access errors (e.g. disabled in browser settings)
  }
})();
