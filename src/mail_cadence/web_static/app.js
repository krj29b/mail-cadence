// Shared panel behaviour. No inline JavaScript is allowed (Content-Security-Policy), so
// elements declare `data-on-<event>="action-name"` and pages register the actions here.
"use strict";

window.MailCadence = (() => {
  const actions = Object.create(null);
  const EVENTS = ["click", "change", "input", "submit"];

  const THEME_KEY = "mail-cadence-theme";

  function register(map) {
    Object.assign(actions, map);
  }

  function dispatch(event) {
    const attr = `data-on-${event.type}`;
    const el = event.target instanceof Element ? event.target.closest(`[${attr}]`) : null;
    if (!el) return;
    const action = actions[el.getAttribute(attr)];
    if (action) action(el, event);
  }

  for (const type of EVENTS) document.addEventListener(type, dispatch);

  function renderIcons(root) {
    if (window.MailCadenceIcons) window.MailCadenceIcons.render(root);
  }

  // Server data for a page lives in <script type="application/json" id="..."> blocks.
  function readJson(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    try {
      return JSON.parse(el.textContent);
    } catch {
      return fallback;
    }
  }

  // Show only table rows whose text contains the search box value.
  function filterRows(input, rowSelector) {
    const filter = input.value.toLowerCase().trim();
    for (const row of document.querySelectorAll(rowSelector)) {
      row.classList.toggle("hidden", !row.textContent.toLowerCase().includes(filter));
    }
  }

  function getStoredTheme() {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") return saved;
    } catch {
      // Ignore localStorage read errors
    }
    return null;
  }

  function setStoredTheme(value) {
    try {
      if (value) {
        localStorage.setItem(THEME_KEY, value);
      } else {
        localStorage.removeItem(THEME_KEY);
      }
    } catch {
      // Ignore localStorage write errors
    }
  }

  function getSystemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  }

  function getEffectiveTheme() {
    return getStoredTheme() || getSystemTheme();
  }

  function updateThemeUI() {
    const stored = getStoredTheme();
    const effective = getEffectiveTheme();

    if (stored) {
      document.documentElement.dataset.theme = stored;
    } else {
      delete document.documentElement.dataset.theme;
    }

    const btn = document.getElementById("theme-toggle");
    if (!btn) return;

    btn.setAttribute("aria-pressed", effective === "light" ? "true" : "false");

    const sunIcon = btn.querySelector(".theme-icon-sun");
    const moonIcon = btn.querySelector(".theme-icon-moon");
    const label = document.getElementById("theme-toggle-label");
    const state = document.getElementById("theme-toggle-state");

    if (effective === "light") {
      if (sunIcon) sunIcon.classList.remove("hidden");
      if (moonIcon) moonIcon.classList.add("hidden");
      if (label) label.textContent = stored ? "Theme: Light" : "Theme: System (Light)";
    } else {
      if (sunIcon) sunIcon.classList.add("hidden");
      if (moonIcon) moonIcon.classList.remove("hidden");
      if (label) label.textContent = stored ? "Theme: Dark" : "Theme: System (Dark)";
    }

    if (state) {
      state.textContent = stored ? stored : "Auto";
    }
  }

  function toggleTheme() {
    const current = getEffectiveTheme();
    const next = current === "dark" ? "light" : "dark";
    setStoredTheme(next);
    updateThemeUI();
  }

  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (!getStoredTheme()) {
        updateThemeUI();
      }
    });
  }

  register({
    "reset-form": (el) => {
      const form = document.getElementById(el.dataset.form);
      if (form) form.reset();
    },
    "toggle-theme": () => {
      toggleTheme();
    },
  });

  document.addEventListener("DOMContentLoaded", () => {
    updateThemeUI();
    renderIcons();
  });
  document.addEventListener("htmx:afterSwap", (event) => renderIcons(event.target));

  return { register, renderIcons, readJson, filterRows, toggleTheme, updateThemeUI };
})();
