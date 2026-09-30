(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const inr = (x) => {
    const neg = x < 0; x = Math.abs(x);
    let [w, f] = x.toFixed(2).split(".");
    if (w.length > 3) { const t = w.slice(-3); let h = w.slice(0, -3); h = h.replace(/\B(?=(\d{2})+(?!\d))/g, ","); w = h + "," + t; }
    return (neg ? "-" : "") + w + "." + f;
  };

  // tabs
  $$(".tabs").forEach((bar) => {
    const panel = bar.parentElement;
    $$(".tab", bar).forEach((t) => t.addEventListener("click", () => {
      $$(".tab", bar).forEach((x) => x.classList.toggle("on", x === t));
      $$(".tabpane", panel).forEach((p) => p.classList.toggle("on", p.id === t.dataset.tab));
      try { sessionStorage.setItem("tab:" + location.pathname, t.dataset.tab); } catch (e) {}
    }));
    let saved = null;
    try { saved = sessionStorage.getItem("tab:" + location.pathname); } catch (e) {}
    if (saved) { const t = $(`.tab[data-tab="${saved}"]`, bar); if (t) t.click(); }
  });

  // exception severity chips
  $$('.chips[data-filter="flags"]').forEach((chips) => {
    $$(".chip", chips).forEach((c) => c.addEventListener("click", () => {
      $$(".chip", chips).forEach((x) => x.classList.toggle("on", x === c));
      $$("#flags .flag").forEach((f) => { f.style.display = c.dataset.sev === "all" || f.dataset.sev === c.dataset.sev ? "" : "none"; });
    }));
  });

  // table search + "only review" toggle
  function applyFilter(table) {
    const q = (table._q || "").toLowerCase().replace(/,/g, "");
    const reviewOnly = !!table._review;
    $$("tbody", table).forEach((tb) => {
      const hitReview = !reviewOnly || tb.dataset.conf === "review";
      if (tb.dataset.conf !== undefined) {
        const text = tb.textContent.toLowerCase().replace(/,/g, "");
        tb.style.display = hitReview && (!q || text.includes(q)) ? "" : "none";
      } else {
        $$("tr", tb).forEach((tr) => {
          const text = tr.textContent.toLowerCase().replace(/,/g, "");
          tr.style.display = !q || text.includes(q) ? "" : "none";
        });
      }
    });
  }
  $$("[data-search]").forEach((inp) => inp.addEventListener("input", () => {
    const t = $(inp.dataset.search); if (!t) return; t._q = inp.value; applyFilter(t);
  }));
  $$("[data-reviewonly]").forEach((cb) => cb.addEventListener("change", () => {
    const t = $(cb.dataset.reviewonly); if (!t) return; t._review = cb.checked; applyFilter(t);
  }));

  // manual match selection
  const bar = $("#selbar");
  if (bar) {
    const update = () => {
      const picks = $$(".pick:checked");
      let b = 0, k = 0; const dirs = new Set();
      picks.forEach((p) => { const a = parseFloat(p.dataset.amt); if (p.dataset.side === "bank") b += a; else k += a; dirs.add(p.dataset.dir); });
      $("#sel-bank").textContent = inr(b); $("#sel-book").textContent = inr(k);
      const d = Math.round((b - k) * 100) / 100;
      const diff = $("#sel-diff");
      diff.textContent = dirs.size > 1 ? "Mixed debit/credit — not allowed" : "Diff ₹" + inr(d);
      diff.className = "tot diff " + (d === 0 && dirs.size === 1 ? "zero" : "nz");
      $("#sel-ids").value = picks.map((p) => p.value).join(",");
      const hasBoth = picks.some((p) => p.dataset.side === "bank") && picks.some((p) => p.dataset.side === "book");
      $("#sel-go").disabled = !(hasBoth && d === 0 && dirs.size === 1);
      bar.classList.toggle("on", picks.length > 0);
    };
    $$(".pick").forEach((p) => p.addEventListener("change", update));
  }

  // notes save on Enter or blur-if-changed
  $$(".notef input[name=note]").forEach((inp) => {
    const orig = inp.value;
    inp.addEventListener("blur", () => { if (inp.value !== orig) inp.form.submit(); });
  });

  // upload drop zones
  $$(".drop").forEach((d) => {
    const inp = $("input[type=file]", d); const f = $(".f", d);
    inp.addEventListener("change", () => { const n = inp.files[0] ? inp.files[0].name : ""; f.textContent = n; d.classList.toggle("has", !!n); });
    ["dragenter", "dragover"].forEach((ev) => d.addEventListener(ev, () => d.classList.add("over")));
    ["dragleave", "drop"].forEach((ev) => d.addEventListener(ev, () => d.classList.remove("over")));
  });
  const form = $("#recoform");
  if (form) {
    form.addEventListener("submit", () => { $("#go").disabled = true; $("#busy").style.display = ""; });
    const sel = $("#acc"), hint = $("#nexthint");
    const view = $("#viewlast");
    const showNext = () => {
      const o = sel.selectedOptions[0]; const n = o && o.dataset.next;
      if (view) {
        const rid = o && o.dataset.run;
        view.style.display = rid ? "" : "none";
        if (rid) view.href = location.pathname.replace(/\/reconcile.*$/, "") + "/runs/" + rid;
      }
      if (!n) { hint.textContent = o && o.value ? "First reconciliation for this account." : ""; return; }
      const d = new Date(n + "T00:00:00"); d.setDate(d.getDate() + 1);
      hint.textContent = "Next date to reconcile: " + d.toLocaleDateString("en-GB").replace(/\//g, "-");
    };
    sel.addEventListener("change", showNext); showNext();
  }
})();
