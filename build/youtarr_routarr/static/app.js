// Small conveniences. Selection and filing work without this file.
(() => {
  "use strict";
  document.documentElement.classList.add("js");
  const SCROLL = "routarr-scroll";
  const FOCUS = "routarr-focus";
  const form = document.getElementById("select");

  function remember() {
    sessionStorage.setItem(SCROLL, String(window.scrollY));
  }

  if (form) {
    // Ticking a box selects straight away. A box from another channel starts a new selection.
    form.addEventListener("change", (event) => {
      const box = event.target.closest('input[name="ids"]');
      if (!box) return;
      const boxes = Array.from(form.querySelectorAll('input[name="ids"]'));
      if (box.checked) {
        boxes.filter((b) => b.dataset.channel !== box.dataset.channel).forEach((b) => { b.checked = false; });
      }
      const ids = boxes.filter((b) => b.checked).map((b) => b.value);
      remember();
      sessionStorage.setItem(FOCUS, box.value);
      window.location.assign(ids.length ? `/?ids=${ids.map(encodeURIComponent).join(",")}` : "/");
    });
    // Keep the list where it was after choosing a row.
    form.addEventListener("click", (event) => {
      if (event.target.closest("a")) remember();
    });
    const saved = sessionStorage.getItem(SCROLL);
    sessionStorage.removeItem(SCROLL);
    const panel = document.getElementById("panel");
    // On narrow screens the panel sits below the list, so bring it into view.
    const stacked = panel && panel.offsetTop >= form.offsetTop + form.offsetHeight;
    if (stacked && /[?&](item|ids)=/.test(window.location.search)) panel.scrollIntoView();
    else if (saved !== null) window.scrollTo(0, Number(saved));
    // Ticking reloads the page, so put keyboard focus back on the box that was ticked.
    const ticked = sessionStorage.getItem(FOCUS);
    sessionStorage.removeItem(FOCUS);
    const box = ticked && form.querySelector(`input[name="ids"][value="${CSS.escape(ticked)}"]`);
    if (box) box.focus({ preventScroll: true });
  }

  const toast = document.querySelector(".toast");
  if (toast) {
    // No time limit: the message may offer Undo, so it stays until dismissed.
    toast.querySelector(".dismiss").addEventListener("click", () => { toast.hidden = true; });
    // Screen readers announce changes to a live region, not text that was there on load.
    const status = toast.querySelector('[role="status"]');
    const text = status.textContent;
    status.textContent = "";
    setTimeout(() => { status.textContent = text; }, 150);
  }
})();
