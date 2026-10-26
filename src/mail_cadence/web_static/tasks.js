// Tasks page: edit form, context variable editor, table search.
"use strict";

(() => {
  const { register, renderIcons, filterRows } = window.MailCadence;
  const byId = (id) => document.getElementById(id);

  function syncContextVarsJson() {
    const vars = [];
    for (const row of document.querySelectorAll(".ctx-var-row")) {
      const name = row.querySelector(".ctx-key").value.trim();
      if (name) vars.push({ name, value: row.querySelector(".ctx-val").value });
    }
    const field = byId("context_variables");
    if (field) field.value = JSON.stringify(vars);
  }

  function addContextVarRow(name = "", value = "", skipSync = false) {
    const row = document.createElement("div");
    row.className = "flex items-center gap-2 ctx-var-row";
    row.innerHTML = `
      <input type="text" placeholder="Key (e.g. company)" data-on-input="sync-context-vars"
             class="ctx-key w-1/3 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-100 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-indigo-500 font-mono">
      <input type="text" placeholder="Value (e.g. Acme Corp)" data-on-input="sync-context-vars"
             class="ctx-val w-2/3 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-100 placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-indigo-500 font-mono">
      <button type="button" data-on-click="remove-context-var" title="Remove variable"
              class="p-1 text-slate-500 hover:text-rose-400 transition-colors">
        <i data-lucide="x" class="w-4 h-4"></i>
      </button>`;
    // Values are set as properties, never interpolated into HTML.
    row.querySelector(".ctx-key").value = name;
    row.querySelector(".ctx-val").value = value;
    byId("ctx-vars-container").appendChild(row);
    renderIcons(row);
    if (!skipSync) syncContextVarsJson();
  }

  function renderContextVarsFromField() {
    const container = byId("ctx-vars-container");
    const field = byId("context_variables");
    if (!container) return;
    container.replaceChildren();
    if (!field || !field.value.trim()) return;
    try {
      const data = JSON.parse(field.value);
      if (!Array.isArray(data)) return;
      for (const item of data) {
        if (item && item.name) addContextVarRow(item.name, item.value || "", true);
      }
      syncContextVarsJson();
    } catch {
      // Not valid JSON: leave the visual editor empty; the raw field is still submitted.
    }
  }

  function setValue(id, value) {
    const el = byId(id);
    if (el) el.value = value;
  }

  async function loadTaskToEdit(taskId) {
    if (!taskId) return;
    const resp = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (!resp.ok) {
      alert(`Failed to fetch task data: ${resp.status} ${resp.statusText}`);
      return;
    }
    const task = await resp.json();
    const join = (value) => (Array.isArray(value) ? value.join(", ") : value || "");

    setValue("id", task.id || "");
    setValue("schedule_ref", task.schedule_ref || "");
    setValue("sender", task.sender || "");
    setValue("priority", task.priority || "normal");
    setValue("max_retries", task.max_retries ?? 3);
    setValue("subject", task.subject || "");
    setValue("body_format", task.body_template ? task.body_template.format : "html");
    setValue("body_template_path", task.body_template ? task.body_template.template_path : "");
    setValue("quick-template-select", "");
    setValue("to", join(task.to));
    setValue("cc", join(task.cc));
    setValue("bcc", join(task.bcc));
    setValue("context_variables", JSON.stringify(task.context_variables || []));
    renderContextVarsFromField();
    setValue("quick-load-task", taskId);

    const details = byId("task-form-details");
    if (details) {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function resetTaskForm() {
    for (const id of ["id", "subject", "body_template_path", "to", "cc", "bcc", "context_variables"]) {
      setValue(id, "");
    }
    setValue("priority", "normal");
    setValue("body_format", "html");
    setValue("max_retries", "3");
    setValue("quick-load-task", "");
    setValue("quick-template-select", "");
    const container = byId("ctx-vars-container");
    if (container) container.replaceChildren();
  }

  register({
    "load-task": (el) => loadTaskToEdit(el.dataset.taskId ?? el.value),
    "pick-template": (el) => {
      if (el.value) setValue("body_template_path", el.value);
    },
    "add-context-var": () => addContextVarRow(),
    "remove-context-var": (el) => {
      el.closest(".ctx-var-row").remove();
      syncContextVarsJson();
    },
    "sync-context-vars": syncContextVarsJson,
    "reset-task-form": resetTaskForm,
    "filter-tasks": (el) => filterRows(el, ".task-table-row"),
  });

  document.addEventListener("DOMContentLoaded", renderContextVarsFromField);
})();
