// Schedules page: visual hour-window editor synced to the raw JSON field, table search.
"use strict";

(() => {
  const { register, renderIcons, readJson, filterRows } = window.MailCadence;
  const byId = (id) => document.getElementById(id);
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const DEFAULT_WINDOW = { start: "08:00", end: "18:00", days_of_week: "*", max_concurrency: 2, rate_limit_per_min: 60 };

  // Pill styles (kept as literal class lists so the Tailwind build finds them).
  const EVERY_DAY_ON = "bg-indigo-600 text-white border-indigo-500";
  const DAY_ON = "bg-indigo-600/30 text-indigo-300 border-indigo-500/50";
  const PILL_OFF = "bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200";
  const PILL_BASE = "px-2 py-0.5 rounded text-[11px] font-mono border transition-colors";

  function setPill(button, selected, onClasses) {
    button.dataset.selected = String(selected);
    // Keep the marker class (pill-day / pill-everyday): rows look buttons up by it.
    const marker = button.dataset.day ? "pill-day uppercase" : "pill-everyday";
    button.className = `${marker} ${PILL_BASE} ${selected ? onClasses : PILL_OFF}`;
  }

  function setDays(row, days) {
    const everyDay = days === "*" || days.size === 0 || days.size === DAYS.length;
    setPill(row.querySelector(".pill-everyday"), everyDay, EVERY_DAY_ON);
    for (const button of row.querySelectorAll(".pill-day")) {
      setPill(button, !everyDay && days.has(button.dataset.day), DAY_ON);
    }
  }

  function selectedDays(row) {
    if (row.querySelector(".pill-everyday").dataset.selected === "true") return "*";
    const days = [...row.querySelectorAll('.pill-day[data-selected="true"]')].map((b) => b.dataset.day);
    return days.length ? days.join(",") : "*";
  }

  function syncVisualToRawJson() {
    const field = byId("allowed_hours_json");
    const container = byId("hour-windows-container");
    if (!field || !container) return;
    const toInt = (input, fallback) => {
      const value = Number.parseInt(input.value, 10);
      return Number.isNaN(value) ? fallback : value;
    };
    const windows = [...container.querySelectorAll(".hw-builder-row")].map((row) => ({
      start: row.querySelector(".hw-input-start").value || "08:00",
      end: row.querySelector(".hw-input-end").value || "17:00",
      days_of_week: selectedDays(row),
      max_concurrency: toInt(row.querySelector(".hw-input-max-conc"), 1),
      rate_limit_per_min: toInt(row.querySelector(".hw-input-rate-limit"), 60),
    }));
    field.value = JSON.stringify(windows);
  }

  const INPUT = "w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-50 font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500";

  function createRow(data) {
    const row = document.createElement("div");
    row.className = "hw-builder-row bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2";
    const dayButtons = DAYS.map(
      (day) => `<button type="button" class="pill-day" data-day="${day}" data-on-click="toggle-day">${day}</button>`,
    ).join("");
    row.innerHTML = `
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 items-center">
        <div>
          <label class="block text-[11px] font-medium text-slate-400 mb-1">Start Time (HH:MM)</label>
          <input type="time" data-on-change="sync-windows" class="hw-input-start ${INPUT}">
        </div>
        <div>
          <label class="block text-[11px] font-medium text-slate-400 mb-1">End Time (HH:MM, 24:00 = end of day)</label>
          <input type="text" pattern="([01][0-9]|2[0-3]):[0-5][0-9]|24:00" placeholder="17:00"
                 title="HH:MM, or 24:00 for the end of the day" data-on-change="sync-windows" class="hw-input-end ${INPUT}">
        </div>
        <div>
          <label class="block text-[11px] font-medium text-slate-400 mb-1">Max Concurrency</label>
          <input type="number" min="1" data-on-change="sync-windows" class="hw-input-max-conc ${INPUT}">
        </div>
        <div>
          <label class="block text-[11px] font-medium text-slate-400 mb-1">Rate Limit / min</label>
          <div class="flex items-center gap-2">
            <input type="number" min="1" data-on-change="sync-windows" class="hw-input-rate-limit ${INPUT}">
            <button type="button" data-on-click="remove-window" title="Remove Window"
                    class="bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 p-1.5 rounded-lg transition-colors shrink-0">
              <i data-lucide="trash-2" class="w-4 h-4"></i>
            </button>
          </div>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-1.5 pt-1 border-t border-slate-900">
        <span class="text-[11px] font-medium text-slate-400 mr-1">Days of Week:</span>
        <button type="button" class="pill-everyday" data-on-click="toggle-every-day">Every Day (*)</button>
        ${dayButtons}
      </div>`;
    // Values are set as properties, never interpolated into HTML.
    row.querySelector(".hw-input-start").value = data.start || "08:00";
    row.querySelector(".hw-input-end").value = data.end || "17:00";
    row.querySelector(".hw-input-max-conc").value = data.max_concurrency ?? 1;
    row.querySelector(".hw-input-rate-limit").value = data.rate_limit_per_min ?? 60;
    const rawDays = String(data.days_of_week || "*").toLowerCase().trim();
    setDays(row, rawDays === "*" ? "*" : new Set(rawDays.split(",").map((d) => d.trim()).filter(Boolean)));
    return row;
  }

  function addWindow(data = DEFAULT_WINDOW) {
    const container = byId("hour-windows-container");
    if (!container) return;
    const row = createRow(data);
    container.appendChild(row);
    renderIcons(row);
    syncVisualToRawJson();
  }

  function setWindows(windows) {
    const container = byId("hour-windows-container");
    if (!container) return;
    container.replaceChildren();
    if (windows && windows.length) {
      for (const window of windows) addWindow(window);
    } else {
      addWindow();
    }
  }

  function openForm() {
    const details = byId("schedule-form-details");
    if (details) details.open = true;
  }

  async function loadScheduleToEdit(scheduleId) {
    if (!scheduleId) return;
    openForm();
    const resp = await fetch(`/api/schedules/${encodeURIComponent(scheduleId)}`);
    if (!resp.ok) {
      alert(`Failed to fetch schedule: ${resp.status} ${resp.statusText}`);
      return;
    }
    const schedule = await resp.json();
    byId("id").value = schedule.id || "";
    byId("description").value = schedule.description || "";
    const select = byId("load-schedule-select");
    if (select) select.value = schedule.id || "";
    setWindows(schedule.allowed_hours);
  }

  function resetScheduleForm() {
    openForm();
    byId("id").value = "";
    byId("description").value = "";
    const select = byId("load-schedule-select");
    if (select) select.value = "";
    setWindows([DEFAULT_WINDOW]);
  }

  register({
    "load-schedule": (el) => loadScheduleToEdit(el.dataset.scheduleId ?? el.value),
    "reset-schedule-form": resetScheduleForm,
    "toggle-raw-json": () => byId("raw-json-wrapper")?.classList.toggle("hidden"),
    "add-window": () => addWindow(),
    "remove-window": (el) => {
      el.closest(".hw-builder-row").remove();
      syncVisualToRawJson();
    },
    "toggle-every-day": (el) => {
      setDays(el.closest(".hw-builder-row"), "*");
      syncVisualToRawJson();
    },
    "toggle-day": (el) => {
      const row = el.closest(".hw-builder-row");
      const days = new Set(selectedDays(row) === "*" ? [] : selectedDays(row).split(","));
      if (el.dataset.selected === "true") days.delete(el.dataset.day);
      else days.add(el.dataset.day);
      setDays(row, days);
      syncVisualToRawJson();
    },
    "sync-windows": syncVisualToRawJson,
    "filter-schedules": (el) => filterRows(el, ".schedule-table-row"),
  });

  document.addEventListener("DOMContentLoaded", () => {
    setWindows(readJson("edit-schedule-windows", null) || [DEFAULT_WINDOW]);
  });
})();
