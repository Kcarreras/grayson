/* Shared chart lightbox: ← → walk charts in their displayed order. The
   tile's SVG shows at once; the detail rendering (more, longer axis labels on
   a wider canvas) replaces it when it arrives, and is kept for the next look. */
(function () {
  var dlg = document.getElementById("chart-lightbox");
  if (!dlg || !dlg.showModal) return;
  var tiles = Array.prototype.slice.call(document.querySelectorAll(".chartbody[data-chart]"));
  if (!tiles.length) return;
  var at = -1;
  var detail = {};
  function loadDetail(id, body) {
    if (detail[id]) { body.innerHTML = detail[id]; return; }
    if (!window.fetch) return;
    fetch(tiles[at].dataset.svgUrl, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (svg) {
        detail[id] = svg;
        if (tiles[at] && tiles[at].dataset.chart === id) body.innerHTML = svg;
      })
      .catch(function () {});  /* the tile's own SVG stays up */
  }
  function show(i) {
    if (i < 0 || i >= tiles.length) return;
    at = i;
    var tile = tiles[i];
    var svg = tile.querySelector("svg");
    document.getElementById("lb-title").textContent = tile.dataset.title || "";
    document.getElementById("lb-caption").textContent = tile.dataset.caption || "";
    document.getElementById("lb-pos").textContent = (i + 1) + " / " + tiles.length;
    document.getElementById("lb-open").href = tile.dataset.pageUrl;
    var body = document.getElementById("lb-body");
    body.innerHTML = "";
    if (svg) body.appendChild(svg.cloneNode(true));
    loadDetail(tile.dataset.chart, body);
    document.getElementById("lb-prev").disabled = i === 0;
    document.getElementById("lb-next").disabled = i === tiles.length - 1;
    if (!dlg.open) dlg.showModal();
  }
  tiles.forEach(function (tile, i) {
    tile.tabIndex = 0;
    tile.setAttribute("role", "button");
    tile.setAttribute("aria-label", "Enlarge chart: " + tile.dataset.title);
    tile.addEventListener("click", function () { show(i); });
    tile.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(i); }
    });
  });
  document.getElementById("lb-prev").addEventListener("click", function () { show(at - 1); });
  document.getElementById("lb-next").addEventListener("click", function () { show(at + 1); });
  document.getElementById("lb-close").addEventListener("click", function () { dlg.close(); });
  dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") { show(at - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { show(at + 1); e.preventDefault(); }
  });
})();
