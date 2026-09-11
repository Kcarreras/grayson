/* Capture server markup before enhancements, to refresh only when it changes. */
var graysonInitialContent = document.getElementById("main-content").innerHTML;

/* Copy the exact server text, preserving whitespace independently of highlighting. */
document.addEventListener("click", async function (event) {
  var button = event.target.closest("[data-copy-sql]");
  if (!button || button.disabled) return;
  var status = button.closest(".sql-transfer").querySelector("[data-copy-status]");
  button.disabled = true;
  status.textContent = "Copying…";
  try {
    var response = await fetch(button.dataset.copySql, {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) {
      status.textContent = response.status === 409
        ? "This proposal changed. Reload before copying."
        : "Could not load SQL. Reload and try again.";
      return;
    }
    await navigator.clipboard.writeText(await response.text());
    status.textContent = "SQL copied";
  } catch (error) {
    status.textContent = "Copy unavailable. Select the SQL below or use Download .sql.";
  } finally {
    button.disabled = false;
  }
});

(function () {
  var button = document.getElementById("mobile-menu");
  var nav = document.getElementById("main-navigation");
  document.querySelector(".sidebar").classList.add("nav-ready");
  button.addEventListener("click", function () {
    button.setAttribute("aria-expanded", String(nav.classList.toggle("open")));
  });
  nav.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && nav.classList.contains("open")) {
      nav.classList.remove("open"); button.setAttribute("aria-expanded", "false"); button.focus();
    }
  });
})();

/* Refresh protection is based on values, not focus: a note stays protected
   after tabbing away. No form values are copied to browser storage. */
var graysonForms = new Map();
var graysonSubmitting = false;
function graysonFormState(form) {
  return JSON.stringify(Array.from(new FormData(form).entries()).map(function (entry) {
    return [entry[0], typeof entry[1] === "string" ? entry[1] : entry[1].name];
  }));
}
document.querySelectorAll('form[method="post"]').forEach(function (form) {
  graysonForms.set(form, graysonFormState(form));
});
function graysonDirty() {
  return Array.from(graysonForms).some(function (entry) {
    return entry[0].isConnected && graysonFormState(entry[0]) !== entry[1];
  });
}
document.addEventListener("submit", function (event) {
  if (event.defaultPrevented || event.target.method !== "post") return;
  if (graysonSubmitting) { event.preventDefault(); return; }
  graysonSubmitting = true;
  event.target.setAttribute("aria-busy", "true");
  var button = event.submitter;
  if (button) { button.setAttribute("aria-disabled", "true"); button.setAttribute("aria-busy", "true"); }
});
window.addEventListener("beforeunload", function (event) {
  if (!graysonSubmitting && graysonDirty()) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("pageshow", function (event) {
  if (!event.persisted) return;
  graysonSubmitting = false;
  document.querySelectorAll('[aria-busy="true"]').forEach(function (el) {
    el.removeAttribute("aria-busy"); el.removeAttribute("aria-disabled");
  });
});

function graysonToast(message) {
  var toast = document.getElementById("ui-toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(graysonToast.timer);
  graysonToast.timer = setTimeout(function () { toast.hidden = true; }, 3500);
}

document.querySelectorAll(".help").forEach(function (help) {
  ["mouseenter", "focusin"].forEach(function (name) {
    help.addEventListener(name, function () { help.classList.remove("help-dismissed"); });
  });
});
document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") document.querySelectorAll(".help").forEach(function (help) {
    help.classList.add("help-dismissed");
  });
});

/* Copy-to-clipboard on every SQL block. Text is captured before the button is
   appended so the button's own label never ends up in the copied text. */
(function () {
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement("textarea");   /* http://127.0.0.1 fallback */
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    var copied = false;
    try { copied = document.execCommand("copy"); } finally { document.body.removeChild(ta); }
    return copied ? Promise.resolve() : Promise.reject(new Error("Copy unavailable"));
  }
  document.querySelectorAll("pre.sql:not(.sql-proposal-code)").forEach(function (pre) {
    var text = pre.textContent;
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "copybtn"; btn.textContent = "Copy";
    btn.title = "Copy SQL to clipboard";
    btn.addEventListener("click", function () {
      copy(text).then(function () {
        btn.textContent = "Copied"; btn.classList.add("done");
        graysonToast("SQL copied to clipboard");
        setTimeout(function () {
          btn.textContent = "Copy"; btn.classList.remove("done");
        }, 1400);
      }).catch(function () { graysonToast("Could not copy. Select the SQL and copy it manually."); });
    });
    pre.appendChild(btn);
  });
})();

/* Chart and checkpoint tooltips. A shortened axis label carries its full text in data-full
   and a mark carries its value in a <title>; both show in an HTML tip at once,
   where the native SVG tooltip is slow, hover-only, and absent on touch.
   Delegated from the document, so the lightbox's cloned or fetched SVG needs
   no rebinding; mark titles move into data-tip once so the native tooltip does
   not double up (exports render fresh and keep theirs). */
(function () {
  var SCOPE = ".chartbody svg, .lb-body svg, .chart-large svg";
  function adopt(root) {
    if (!root.querySelectorAll) return;
    root.querySelectorAll(".checkpoint-label[title]").forEach(function (el) {
      el.dataset.tip = el.title;
      el.removeAttribute("title");
    });
    root.querySelectorAll(SCOPE).forEach(function (svg) {
      svg.querySelectorAll("path > title, circle > title").forEach(function (t) {
        t.parentNode.dataset.tip = t.textContent; t.remove();
      });
      svg.querySelectorAll("text[data-full] > title").forEach(function (t) { t.remove(); });
    });
  }
  adopt(document);
  new MutationObserver(function (records) {
    records.forEach(function (r) { r.addedNodes.forEach(adopt); });
  }).observe(document.body, { childList: true, subtree: true });

  var tip = document.createElement("div");
  tip.className = "viz-tip"; tip.hidden = true;
  tip.id = "console-tooltip"; tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  var pinned = null;
  var active = null;
  function target(e) {
    var el = e.target && e.target.closest && e.target.closest("[data-full], [data-tip]");
    return el && (el.matches(".checkpoint-label") || el.closest(SCOPE)) ? el : null;
  }
  function place(x, y) {
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(x + 12, window.innerWidth - w - 8);
    var top = y - h - 10;
    if (top < 8) top = y + 16;
    tip.style.left = Math.max(8, left) + "px"; tip.style.top = top + "px";
  }
  function show(el, x, y) {
    if (active && active !== el) active.removeAttribute("aria-describedby");
    active = el;
    el.setAttribute("aria-describedby", tip.id);
    tip.textContent = el.dataset.full || el.dataset.tip || "";
    tip.classList.toggle("mono", !!el.dataset.full);
    tip.hidden = false;
    place(x, y);
  }
  function hide() {
    if (pinned) return;
    tip.hidden = true;
    if (active) active.removeAttribute("aria-describedby");
    active = null;
  }
  document.addEventListener("pointerover", function (e) {
    var el = target(e);
    if (el && e.pointerType !== "touch") show(el, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", function (e) {
    if (!tip.hidden && !pinned && e.pointerType !== "touch") place(e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", function (e) { if (target(e)) hide(); });
  document.addEventListener("pointerdown", function (e) {
    var el = target(e);
    if (e.pointerType === "touch" && el) { pinned = el; show(el, e.clientX, e.clientY); }
    else { pinned = null; hide(); }
  });
  document.addEventListener("focusin", function (e) {
    var el = target(e);
    if (!el) return;
    var r = el.getBoundingClientRect();
    show(el, r.left + r.width / 2, r.top);
  });
  document.addEventListener("focusout", function (e) { if (target(e)) hide(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { pinned = null; hide(); }
  });
})();

/* Folds ease open only on a user toggle, never on page load: the live views
   reload every few seconds and must not flicker. */
document.addEventListener("click", function (e) {
  var summary = e.target.closest("summary");
  var d = summary && summary.parentElement;
  if (!d || d.tagName !== "DETAILS" || d.open) return;
  d.classList.add("opening");
  setTimeout(function () { d.classList.remove("opening"); }, 260);
});

/* Stage timeline: when the stage has advanced since this browser last saw
   the session, the newly completed track fills in and the nodes pop. */
(function () {
  var tl = document.querySelector(".timeline[data-session]");
  if (!tl) return;
  var key = "grayson_stage_" + tl.dataset.session;
  var now = parseInt(tl.dataset.stage, 10);
  var seen = null;
  try { seen = localStorage.getItem(key); localStorage.setItem(key, String(now)); } catch (e) {}
  if (seen === null) return;
  seen = parseInt(seen, 10);
  if (!(now > seen)) return;
  var steps = tl.querySelectorAll(".tl-step");
  for (var i = Math.max(seen, 0); i <= now && i < steps.length; i++) {
    steps[i].style.setProperty("--k", String(i - seen));
    steps[i].classList.add("tl-advance");
    if (i === seen) steps[i].classList.add("from");
  }
})();

/* Collapsible tiles (charts, findings, sections): the server opens the newest
   few; a manual toggle is remembered per tile so the 10s live refresh doesn't
   fight the user. */
(function () {
  var cards = document.querySelectorAll("details[data-fold]");
  if (!cards.length) return;
  var key = "grayson_fold_" + location.pathname;
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) {}
  cards.forEach(function (d) {
    var id = d.dataset.fold;
    if (id in saved) d.open = !!saved[id];
    d.addEventListener("toggle", function () {
      saved[id] = d.open ? 1 : 0;
      try { localStorage.setItem(key, JSON.stringify(saved)); } catch (e) {}
    });
  });
})();
/* In-page jumps (the stat tiles, "superseded by" links): the target unfolds
   first if it or any ancestor is collapsed, then the page scrolls to it and
   lights it for a moment. The hash is not kept, so the live refresh does not
   jump back; a hash arriving from outside is honoured once, then dropped. */
(function () {
  function unfold(el) {
    for (var d = el.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) {
      if (!d.open) d.open = true;
    }
  }
  function spot(el) {
    el.classList.remove("spot"); void el.offsetWidth; el.classList.add("spot");
    setTimeout(function () { el.classList.remove("spot"); }, 1700);
  }
  function jump(id) {
    var el = id && document.getElementById(id);
    if (!el) return false;
    el.dispatchEvent(new CustomEvent('grayson:reveal', {bubbles: true}));
    unfold(el);
    el.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    el.focus({ preventScroll: true });
    spot(el);
    return true;
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (!a || a.getAttribute("href").length < 2) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
    var id;
    try { id = decodeURIComponent(a.getAttribute("href").slice(1)); } catch (err) { return; }
    if (jump(id)) e.preventDefault();
  });
  function followHash() {
    if (location.hash.length < 2) return;
    var id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (err) { return; }
    var el = document.getElementById(id);
    if (el) unfold(el);
    // Keep the hash until live-page scroll restoration has seen it. An explicit
    // evidence link takes priority over a position saved by an earlier refresh.
    requestAnimationFrame(function () {
      if (jump(id) && history.replaceState) history.replaceState(history.state, "", location.pathname + location.search);
    });
  }
  window.addEventListener('hashchange', followHash);
  followHash();
})();

/* Lists: a [data-list] container's [data-item] children sort by their
   data-s-<key> values (numeric when every value parses as a number), filter
   by data-tags (OR within a category, AND between categories) and a text search over the
   item, and fold together. Sortable table headers (th[data-sortkey]) drive
   the same order. State is remembered per list per page. */
(function () {
  document.querySelectorAll("[data-list]").forEach(function (list) {
    var id = list.dataset.list;
    var items = Array.prototype.slice.call(list.querySelectorAll("[data-item]"));
    var tools = document.querySelector('[data-list-tools="' + id + '"]');
    if (!items.length || !tools) return;
    var sel = tools.querySelector("select");
    var q = tools.querySelector(".lt-q");
    var chips = Array.prototype.slice.call(tools.querySelectorAll(".fchip"));
    var count = tools.querySelector(".lt-count");
    var reset = tools.querySelector(".lt-reset");
    var empty = document.createElement("div");
    empty.className = "list-empty"; empty.hidden = true;
    empty.textContent = "No matches. Try another search or clear the filters above.";
    var container = list.closest(".listwrap") || list.closest(".scroll") || list.closest("table") || list;
    container.insertAdjacentElement("afterend", empty);
    var searchText = new Map(items.map(function (it) {
      // Descriptions can live in editable inputs, outside textContent. Index
      // their saved values once so filtering never hides a row mid-edit.
      var values = Array.from(it.querySelectorAll('input:not([type="hidden"]), textarea'))
        .map(function (input) { return input.defaultValue; }).join(' ');
      return [it, (it.textContent + ' ' + values).toLowerCase()];
    }));
    var table = list.closest("table");
    var heads = table ? Array.prototype.slice.call(table.querySelectorAll("th[data-sortkey]")) : [];
    var noun = list.dataset.noun || "item";
    var key = "grayson_list_" + location.pathname + "_" + id;
    var st = { sort: sel ? sel.value : "", tags: [], q: "" };
    try { st = Object.assign(st, JSON.parse(localStorage.getItem(key) || "{}")); } catch (e) {}
    if (!Array.isArray(st.tags)) st.tags = [];
    st.tags = st.tags.filter(function (tag) { return chips.some(function (chip) { return chip.dataset.tag === tag; }); });
    if (typeof st.q !== "string") st.q = "";
    if (typeof st.sort !== "string") st.sort = sel ? sel.value : "";
    if (sel && !Array.from(sel.options).some(function (option) { return option.value === st.sort; }) &&
        !heads.some(function (head) {
          return st.sort === head.dataset.sortkey + ':asc' || st.sort === head.dataset.sortkey + ':desc';
        })) st.sort = sel.value;
    if (sel && !Array.from(sel.options).some(function (option) { return option.value === st.sort; })) {
      var extra = document.createElement("option");   /* set from a column header */
      extra.value = st.sort; extra.textContent = st.sort.replace(":", " "); extra.hidden = true;
      sel.appendChild(extra);
    }
    function tagsOf(it) { return (it.dataset.tags || "").split(/\s+/); }
    function val(it, k) { return it.getAttribute("data-s-" + k) || ""; }
    function sort() {
      var parts = st.sort.split(":"), k = parts[0], dir = parts[1] === "desc" ? -1 : 1;
      if (!k) return;
      var numeric = items.every(function (it) { return val(it, k) === "" || /^-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(val(it, k)); });
      var keyed = items.map(function (it, i) {
        var v = val(it, k);
        return { it: it, i: i, v: numeric ? (v === "" ? -Infinity : parseFloat(v)) : v };
      });
      keyed.sort(function (a, b) {
        var c = numeric ? (a.v < b.v ? -1 : a.v > b.v ? 1 : 0)
          : a.v.localeCompare(b.v, undefined, { numeric: true, sensitivity: "base" });
        return c * dir || a.i - b.i;
      });
      items = keyed.map(function (x) { return x.it; });
      items.forEach(function (it) { it.parentNode.appendChild(it); });
      heads.forEach(function (h) {
        var on = h.dataset.sortkey === k;
        h.classList.toggle("sorted", on);
        h.classList.toggle("asc", on && dir === 1);
        h.classList.toggle("desc", on && dir === -1);
        h.setAttribute("aria-sort", on ? (dir === 1 ? "ascending" : "descending") : "none");
      });
    }
    function filter() {
      var needle = st.q.trim().toLowerCase(), shown = 0;
      var groups = new Map();
      chips.forEach(function (chip) {
        if (st.tags.indexOf(chip.dataset.tag) < 0) return;
        var group = chip.dataset.filterGroup || chip.dataset.tag;
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(chip.dataset.tag);
      });
      items.forEach(function (it) {
        var tg = tagsOf(it);
        var ok = Array.from(groups.values()).every(function (tags) {
          return tags.some(function (t) { return tg.indexOf(t) >= 0; });
        }) &&
          (!needle || searchText.get(it).indexOf(needle) >= 0);
        it.hidden = !ok;
        if (ok) shown++;
      });
      empty.hidden = shown !== 0;
      if (reset) reset.hidden = !st.q && !st.tags.length;
      if (count) {
        var plural = noun === "query" ? "queries" : noun + (/(?:s|x|z|ch|sh)$/.test(noun) ? "es" : "s");
        var all = items.length + " " + (items.length === 1 ? noun : plural);
        count.textContent = shown === items.length ? all : shown + " of " + all;
      }
      chips.forEach(function (c) {
        var on = st.tags.indexOf(c.dataset.tag) >= 0;
        c.classList.toggle("on", on);
        c.setAttribute("aria-pressed", String(on));
        var n = c.querySelector(".lt-n");
        if (n) n.textContent = items.filter(function (it) { return tagsOf(it).indexOf(c.dataset.tag) >= 0; }).length;
      });
    }
    function apply(reorder) {
      if (reorder) sort();
      filter();
      try { localStorage.setItem(key, JSON.stringify(st)); } catch (e) {}
    }
    if (sel) {
      sel.value = st.sort;
      sel.addEventListener("change", function () { st.sort = sel.value; apply(true); });
    }
    if (q) {
      q.value = st.q;
      q.addEventListener("input", function () { st.q = q.value; apply(); });
    }
    chips.forEach(function (c) {
      c.addEventListener("click", function () {
        var i = st.tags.indexOf(c.dataset.tag);
        if (i >= 0) st.tags.splice(i, 1); else st.tags.push(c.dataset.tag);
        apply();
      });
    });
    heads.forEach(function (h) {
      h.tabIndex = 0;
      h.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); h.click(); }
      });
      h.addEventListener("click", function () {
        var parts = st.sort.split(":");
        var dir = parts[0] === h.dataset.sortkey && parts[1] === "asc" ? "desc" : "asc";
        st.sort = h.dataset.sortkey + ":" + dir;
        if (sel && !Array.from(sel.options).some(function (option) { return option.value === st.sort; })) {
          var o = document.createElement("option");
          o.value = st.sort; o.textContent = h.textContent.trim() + (dir === "asc" ? " ↑" : " ↓"); o.hidden = true;
          sel.appendChild(o);
        }
        if (sel) sel.value = st.sort;
        apply(true);
      });
    });
    function foldAll(open) {
      items.forEach(function (it) { if (!it.hidden && it.tagName === "DETAILS") it.open = open; });
    }
    var ex = tools.querySelector(".lt-expand"), co = tools.querySelector(".lt-collapse");
    if (ex) ex.addEventListener("click", function () { foldAll(true); });
    if (co) co.addEventListener("click", function () { foldAll(false); });
    if (reset) reset.addEventListener("click", function () {
      st.q = ""; st.tags = []; if (q) { q.value = ""; q.focus(); } apply();
    });
    list.addEventListener('grayson:reveal', function (event) {
      var item = event.target.closest('[data-item]');
      if (!item || !item.hidden) return;
      st.q = ''; st.tags = []; if (q) q.value = ''; apply();
      graysonToast('Filters cleared to show the linked item.');
    });
    apply(true);
  });
})();

/* Session action bar: the picked tab shows its panel; the pick is remembered
   per page so the live refresh brings the same panel back. */
(function () {
  var bar = document.querySelector(".card.actions");
  if (!bar) return;
  var tabs = Array.from(bar.querySelectorAll("[data-act-select]"));
  var cancel = bar.querySelector(".seg-cancel");
  var hint = bar.querySelector(".seg-hint");
  var key = "grayson_act_" + location.pathname;
  var on = null;
  try { on = localStorage.getItem(key); } catch (e) {}
  if (!tabs.some(function (tab) { return tab.dataset.actSelect === on; })) on = null;
  function render() {
    tabs.forEach(function (tab, i) {
      var active = tab.dataset.actSelect === on;
      tab.classList.toggle("on", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active || (!on && i === 0) ? 0 : -1;
      tab.setAttribute("aria-controls", "action-panel-" + tab.dataset.actSelect);
    });
    bar.querySelectorAll(".actpanel").forEach(function (p) {
      p.id = "action-panel-" + p.dataset.act;
      p.setAttribute("aria-labelledby", "sact-" + p.dataset.act);
      p.hidden = p.dataset.act !== on;
    });
    if (cancel) cancel.hidden = !on;
    if (hint) hint.hidden = !!on;
    try { if (on) localStorage.setItem(key, on); else localStorage.removeItem(key); } catch (e) {}
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { on = tab.dataset.actSelect; render(); });
    tab.addEventListener("keydown", function (e) {
      var next;
      if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
      else if (e.key === "ArrowLeft") next = (i + tabs.length - 1) % tabs.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = tabs.length - 1;
      else return;
      e.preventDefault(); tabs[next].click(); tabs[next].focus();
    });
  });
  if (cancel) cancel.addEventListener("click", function () {
    var previous = tabs.find(function (tab) { return tab.dataset.actSelect === on; });
    on = null; render(); if (previous) previous.focus();
  });
  render();
})();

/* Accessible quick navigation: pages and the current page's named sections. */
(function () {
  var dialog = document.getElementById('quick-dialog');
  var trigger = document.getElementById('quick-open');
  if (!dialog.showModal) { trigger.hidden = true; return; }
  var input = document.getElementById('quick-query');
  var results = document.getElementById('quick-results');
  var entries = [];
  document.querySelectorAll('.nav a').forEach(function (a) {
    entries.push({ label: a.textContent.trim(), href: a.href, group: 'Workspace' });
  });
  document.querySelectorAll('main h2').forEach(function (h, i) {
    if (!h.id) h.id = 'page-section-' + i;
    var title = h.cloneNode(true);
    title.querySelectorAll('.help, .h-count, .badge').forEach(function (el) { el.remove(); });
    entries.push({ label: title.textContent.trim(), href: '#' + h.id, group: 'On this page' });
  });
  var links = [], selected = 0;
  function select(index) {
    selected = Math.max(0, Math.min(index, links.length - 1));
    links.forEach(function (a, i) { a.classList.toggle('selected', i === selected); });
    if (links[selected]) links[selected].scrollIntoView({ block: 'nearest' });
  }
  function render() {
    results.replaceChildren();
    links = entries.filter(function (entry) {
      return entry.label.toLowerCase().includes(input.value.trim().toLowerCase());
    }).map(function (entry) {
      var a = document.createElement('a');
      a.href = entry.href; a.className = 'quick-result';
      var label = document.createElement('span'); label.textContent = entry.label;
      var group = document.createElement('small'); group.textContent = entry.group;
      a.append(label, group); results.appendChild(a);
      a.addEventListener('click', function () { dialog.close(); });
      a.addEventListener('focus', function () { select(links.indexOf(a)); });
      return a;
    });
    document.getElementById('quick-empty').hidden = links.length > 0;
    select(0);
  }
  function open() { input.value = ''; render(); dialog.showModal(); input.focus(); }
  trigger.addEventListener('click', open);
  document.getElementById('quick-close').addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
  input.addEventListener('input', render);
  dialog.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); select(selected + (e.key === 'ArrowDown' ? 1 : -1));
      if (e.target !== input && links[selected]) links[selected].focus();
    } else if (e.key === 'Enter' && e.target === input && links[selected]) {
      e.preventDefault(); links[selected].click();
    }
  });
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault(); if (dialog.open) dialog.close(); else open();
    }
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !dialog.open &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && !e.target.isContentEditable) {
      var q = Array.from(document.querySelectorAll('main input[type="search"]')).find(function (el) { return el.getClientRects().length; });
      if (q) { e.preventDefault(); q.focus(); }
    }
  });
})();

/* Fill gaps in legacy compact forms using their visible field/table labels. */
(function () {
  document.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(function (input) {
    if (input.labels.length || input.hasAttribute('aria-label') || input.hasAttribute('aria-labelledby')) return;
    var dd = input.closest('dd'), cell = input.closest('td'), label = '';
    if (dd && dd.previousElementSibling && dd.previousElementSibling.tagName === 'DT') label = dd.previousElementSibling.textContent;
    if (!label && cell) {
      var table = cell.closest('table');
      var head = table && table.querySelector('tr');
      if (head && head.cells[cell.cellIndex]) label = head.cells[cell.cellIndex].textContent;
      if (cell.parentElement.cells[0] !== cell) label = cell.parentElement.cells[0].textContent.trim() + ': ' + label;
    }
    if (!label) label = input.placeholder || input.name.replace(/_/g, ' ');
    if (label) input.setAttribute('aria-label', label.trim());
  });
  document.querySelectorAll('.scroll').forEach(function (region) {
    if (region.scrollWidth > region.clientWidth) {
      region.tabIndex = 0; region.setAttribute('role', 'region');
      region.setAttribute('aria-label', 'Scrollable table');
    }
  });
})();

/* Live pages only reload for changed content. Pause on unsaved edits, dialogs,
   text selections, hidden tabs, errors, or an explicit user pause. */
(function () {
  var control = document.querySelector('[data-live]');
  if (!control) return;
  var label = control.querySelector('[data-live-label]');
  var button = control.querySelector('button');
  var key = 'grayson_live_' + location.pathname;
  var scrollKey = 'grayson_scroll_' + location.pathname;
  var paused = false, failed = false;
  try {
    paused = sessionStorage.getItem(key) === 'paused';
    var position = sessionStorage.getItem(scrollKey);
    if (position !== null) {
      sessionStorage.removeItem(scrollKey);
      if (!location.hash) requestAnimationFrame(function () { window.scrollTo(0, Number(position)); });
    }
  } catch (e) {}
  function reason() {
    if (paused) return 'Updates paused';
    if (graysonDirty()) return 'Paused · unsaved changes';
    if (document.hidden) return 'Paused · tab inactive';
    if (document.querySelector('dialog[open]')) return 'Paused while reviewing';
    var el = document.activeElement;
    if (el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)) return 'Paused while editing';
    if (el && el.closest('main') && el.matches(':focus-visible') &&
        el.matches('button, a, summary, [tabindex="0"]')) return 'Paused while reviewing';
    if (window.getSelection().toString()) return 'Paused while selecting';
    if (graysonSubmitting) return 'Saving changes…';
    return '';
  }
  function render() {
    var why = reason();
    label.textContent = why || (failed ? 'Connection interrupted · retrying' : 'Live · checks every 10s');
    control.toggleAttribute('data-paused', !!why || failed);
    button.textContent = paused ? 'Resume' : 'Pause';
    button.setAttribute('aria-label', paused ? 'Resume live updates' : 'Pause live updates');
  }
  button.addEventListener('click', function () {
    paused = !paused;
    try { sessionStorage.setItem(key, paused ? 'paused' : 'live'); } catch (e) {}
    render();
  });
  ['input', 'change', 'focusin', 'focusout', 'visibilitychange'].forEach(function (name) {
    document.addEventListener(name, function () { setTimeout(render, 0); });
  });
  async function tick() {
    render();
    if (!reason()) {
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, 8000);
      try {
        var response = await fetch(location.href, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error('Refresh failed');
        var html = await response.text();
        var next = new DOMParser().parseFromString(html, 'text/html').getElementById('main-content');
        if (!next) throw new Error('Refresh unavailable');
        failed = false;
        if (next.innerHTML !== graysonInitialContent && !reason()) {
          try { sessionStorage.setItem(scrollKey, String(window.scrollY)); } catch (e) {}
          location.reload(); return;
        }
      } catch (e) { failed = true; }
      finally { clearTimeout(timeout); }
    }
    render(); setTimeout(tick, 10000);
  }
  render(); setTimeout(tick, 10000);
})();
