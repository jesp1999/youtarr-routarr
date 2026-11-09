// Type-to-filter dropdown for <input data-combo="template-id">.
// The template holds <option> elements. The first row is always exactly what is typed,
// so a new show or folder is one Enter away. Picking a row fires "change" for htmx.
(() => {
  "use strict";
  const MAX_ROWS = 200;
  let list = null;    // the floating <ul>
  let input = null;   // the input it belongs to
  let rows = [];      // [{value, isNew}]
  let active = -1;

  function options(el) {
    const tpl = document.getElementById(el.dataset.combo);
    if (!tpl) return [];
    return Array.from(tpl.content.querySelectorAll("option"), (o) => o.textContent);
  }

  function matches(all, text) {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return all.slice();
    const first = words[0];
    const hits = all.filter((o) => {
      const low = o.toLowerCase();
      return words.every((w) => low.includes(w));
    });
    // Names that start with the first word come first; otherwise keep alphabetical order.
    return hits.filter((o) => o.toLowerCase().startsWith(first))
      .concat(hits.filter((o) => !o.toLowerCase().startsWith(first)));
  }

  function ensureList() {
    if (list) return list;
    list = document.createElement("ul");
    list.className = "combo-list";
    list.setAttribute("role", "listbox");
    list.id = "combo-list";
    list.hidden = true;
    // mousedown, not click, so the input keeps focus.
    list.addEventListener("mousedown", (e) => {
      const li = e.target.closest("li[data-index]");
      if (!li) return;
      e.preventDefault();
      choose(Number(li.dataset.index));
    });
    document.body.appendChild(list);
    return list;
  }

  function place() {
    if (!list || list.hidden || !input) return;
    const r = input.getBoundingClientRect();
    list.style.left = `${r.left}px`;
    list.style.top = `${r.bottom + 2}px`;
    list.style.width = `${r.width}px`;
    list.style.maxHeight = `${Math.max(120, window.innerHeight - r.bottom - 16)}px`;
  }

  function render(showAll) {
    const text = input.value.trim();
    const all = options(input);
    const found = showAll ? all : matches(all, text);
    const exact = all.some((o) => o === text);
    // "Salad Fingers" typed for an existing "Salad Fingers (2004)" is that show, not a new one:
    // drop the year-less row so only the entry with the year is offered (Enter picks it).
    const key = text.toLowerCase();
    const hasYearForm = !!text && all.some((o) => o.toLowerCase().replace(/\s*\(\d{4}\)$/, "") === key && o.toLowerCase() !== key);
    rows = [];
    if (text && !exact && !hasYearForm) rows.push({ value: text, isNew: true });
    if (text && exact) rows.push({ value: text, isNew: false });
    for (const o of found) if (o !== text) rows.push({ value: o, isNew: false });
    rows = rows.slice(0, MAX_ROWS);

    const ul = ensureList();
    ul.replaceChildren();
    rows.forEach((row, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = `combo-opt-${i}`;
      li.dataset.index = String(i);
      li.textContent = row.value;
      if (row.isNew) {
        const tag = document.createElement("span");
        tag.className = "combo-new";
        tag.textContent = input.dataset.newLabel || "new";
        li.append(" ", tag);
      }
      ul.appendChild(li);
    });
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "combo-empty";
      li.textContent = "No existing matches";
      ul.appendChild(li);
    }
    active = -1;
    ul.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", ul.id);
    place();
  }

  function highlight(i) {
    if (!rows.length) return;
    active = (i + rows.length) % rows.length;
    list.querySelectorAll("li[aria-selected]").forEach((li) => li.removeAttribute("aria-selected"));
    const li = list.querySelector(`#combo-opt-${active}`);
    li.setAttribute("aria-selected", "true");
    li.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", li.id);
  }

  function close() {
    if (list) list.hidden = true;
    if (input) {
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    active = -1;
  }

  function choose(i) {
    const row = rows[i];
    if (!row || !input) return;
    input.value = row.value;
    close();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("focusin", (e) => {
    const el = e.target.closest("input[data-combo]");
    if (!el) return;
    input = el;
    el.setAttribute("role", "combobox");
    el.setAttribute("aria-autocomplete", "list");
    render(true);
  });

  document.addEventListener("focusout", (e) => {
    if (e.target === input) close();
  });

  document.addEventListener("input", (e) => {
    if (e.target === input) render(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.target !== input) return;
    const open = list && !list.hidden;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) render(false);
      highlight(active + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (open) highlight(active - 1);
    } else if (e.key === "Enter" && open) {
      e.preventDefault(); // never submit the form from the dropdown
      choose(active >= 0 ? active : 0);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      close();
    }
  });

  // A typed value that was never picked still fires the browser's own change on blur,
  // so htmx refreshes either way. An occasional duplicate change only re-fetches a suggestion.
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);
})();
