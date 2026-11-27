// Small conveniences. Selection and filing work without this file.
(() => {
  "use strict";
  document.documentElement.classList.add("js");
  const SCROLL = "routarr-scroll";
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
  }

  const toast = document.querySelector(".toast");
  if (toast) {
    setTimeout(() => { toast.hidden = true; }, 5000);
  }
})();
