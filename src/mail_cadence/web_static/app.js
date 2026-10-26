// Shared panel behaviour. No inline JavaScript is allowed (Content-Security-Policy), so
// elements declare `data-on-<event>="action-name"` and pages register the actions here.
"use strict";

window.MailCadence = (() => {
  const actions = Object.create(null);
  const EVENTS = ["click", "change", "input", "submit"];

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

  register({
    "reset-form": (el) => {
      const form = document.getElementById(el.dataset.form);
      if (form) form.reset();
    },
  });

  document.addEventListener("DOMContentLoaded", () => renderIcons());
  document.addEventListener("htmx:afterSwap", (event) => renderIcons(event.target));

  return { register, renderIcons, readJson, filterRows };
})();
