/* Relationship canvas: Cytoscape.js drawing a model the server built, laid out
 * by ELK's layered algorithm (the cytoscape-elk extension).
 *
 * Layout is entirely ELK's: it assigns tables to layers by their relationships
 * and orders within a layer to minimise crossings, so nodes never land on top
 * of each other and the picture stays readable as the schema grows. Nothing
 * here positions anything by hand.
 *
 * What an edge says, and how it says it:
 *   - the label is the join key as column pairs: "ORDER_ID" when both sides
 *     share the name, "ORDERS.PROMO_CODE = PROMOS.CODE" when they differ (the
 *     line does not say which end declared it, so the label must), one pair
 *     per line for a composite key. A join the server could not read as
 *     column pairs is drawn in italics, as written.
 *   - the line ends carry cardinality in crow's-foot spirit: a bar for "one",
 *     a fork for "many". No cardinality recorded, no end markers.
 *   - dashed: declared by one side only. A dashed node outline: a table the
 *     library references but has no doc for.
 * The tooltip spells all of it out in words, with the table each column
 * belongs to, so nothing depends on decoding the glyphs.
 *
 * Colours come from the page's CSS tokens and are re-read when the theme
 * changes (the nav toggle or the OS), so the canvas never keeps the previous
 * theme's palette.
 *
 * Everything is vendored under static/vendor - the console makes no network
 * requests and works offline.
 */
(function () {
  "use strict";

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var MONO = "ui-monospace, Consolas, monospace";

  function palette() {
    return {
      panel: token("--panel"),
      node: token("--rg-node") || token("--panel2"),
      nodeBorder: token("--rg-node-border") || token("--border"),
      edge: token("--rg-edge") || token("--border"),
      edgeInk: token("--rg-edge-ink") || token("--ink-2"),
      ink: token("--ink"),
      ink2: token("--ink-2"),
      muted: token("--muted"),
      faint: token("--faint"),
      act: token("--act"),
      attn: token("--attn")
    };
  }

  // Cytoscape has no crow's foot; "tee" is the standard "one" bar and "vee"
  // is the closest thing to a fork. The legend under the canvas shows both.
  var END_SHAPE = { one: "tee", many: "vee", "": "none" };

  function styleFor(t) {
    return [
      {
        selector: "node",
        style: {
          shape: "round-rectangle",
          // Sizing from the label means a long table name widens its box
          // instead of overflowing it; the old fixed-width boxes clipped names.
          width: "label",
          height: "label",
          padding: "10px",
          "background-color": t.node,
          "border-width": 1.2,
          "border-color": t.nodeBorder,
          label: "data(label)",
          color: t.ink,
          "font-family": MONO,
          "font-size": 12,
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "none"
        }
      },
      {
        selector: "node[?focus]",
        style: {
          "background-color": t.panel,
          "border-color": t.act,
          "border-width": 2,
          color: t.act,
          "font-weight": 600
        }
      },
      {
        // Referenced by a relationship but never described: a knowledge gap,
        // drawn as one rather than passed off as a documented table.
        selector: "node[!known]",
        style: { "border-style": "dashed", "border-color": t.faint, color: t.muted }
      },
      {
        selector: "edge",
        style: {
          "curve-style": "taxi",
          "taxi-direction": "auto",
          "taxi-turn": "50%",
          "taxi-turn-min-distance": 12,
          width: 1.6,
          "line-color": t.edge,
          "source-arrow-color": t.edge,
          "target-arrow-color": t.edge,
          "source-arrow-shape": "none",
          "target-arrow-shape": "none",
          "arrow-scale": 0.9,
          "font-family": MONO,
          "font-size": 11,
          color: t.edgeInk,
          "text-wrap": "wrap",
          "text-max-width": "260px",
          // An opaque chip behind the label is what stops join keys from
          // dissolving into the lines they sit on.
          "text-background-color": t.panel,
          "text-background-opacity": 1,
          "text-background-padding": 3,
          "text-background-shape": "roundrectangle",
          "text-border-color": t.edge,
          "text-border-width": 1,
          "text-border-opacity": 1,
          "text-rotation": "none"
        }
      },
      { selector: "edge.labelled", style: { label: "data(label)" } },
      // A join that is not column pairs: shown as written, but visibly so.
      { selector: "edge[!parsed]", style: { "font-style": "italic", color: t.muted } },
      { selector: 'edge[source_end = "one"]', style: { "source-arrow-shape": END_SHAPE.one } },
      { selector: 'edge[source_end = "many"]', style: { "source-arrow-shape": END_SHAPE.many } },
      { selector: 'edge[target_end = "one"]', style: { "target-arrow-shape": END_SHAPE.one } },
      { selector: 'edge[target_end = "many"]', style: { "target-arrow-shape": END_SHAPE.many } },
      {
        // Declared by one side only - usually an incomplete profile rather than
        // a different kind of relationship.
        selector: "edge[!mutual]",
        style: { "line-style": "dashed", "line-dash-pattern": [6, 4] }
      },
      {
        // The two sides disagree (on cardinality, or on which columns join):
        // a fact worth a human's eye, coloured as such.
        selector: "edge[conflict != ''], edge[parallel > 1]",
        style: {
          "line-color": t.attn,
          "source-arrow-color": t.attn,
          "target-arrow-color": t.attn,
          "text-border-color": t.attn
        }
      },
      { selector: ".dim", style: { opacity: 0.22 } },
      {
        selector: "edge.hot",
        style: {
          "line-color": t.act,
          "source-arrow-color": t.act,
          "target-arrow-color": t.act,
          width: 2.4,
          color: t.ink,
          "text-border-color": t.act
        }
      },
      { selector: "node.hot", style: { "border-color": t.act, color: t.act } }
    ];
  }

  function build(root) {
    var payload = document.getElementById(root.dataset.model);
    if (!payload) throw new Error("no graph model");
    var model = JSON.parse(payload.textContent);
    var canvas = root.querySelector(".relgraph-canvas");

    var elements = model.nodes
      .map(function (n) {
        return { group: "nodes", data: n };
      })
      .concat(
        model.edges.map(function (e) {
          return { group: "edges", data: e };
        })
      );

    // Node placement is the expensive step and it scales badly. Measured on a
    // synthetic warehouse schema: NETWORK_SIMPLEX takes 43 ms at 40 nodes,
    // 267 ms at 60, 729 ms at 80 and 2.5 s at 129 - past ~60 the wait is worse
    // than the payoff. BRANDES_KOEPF does 129 nodes in 263 ms; it spends about
    // 70% more vertical space, which on a big map you are panning anyway.
    var COMPACT_PLACEMENT_MAX_NODES = 60;

    // The join-key chip sits mid-edge and edges draw under nodes, so a chip
    // wider than the gap between layers is hidden behind the boxes it joins.
    // Size the gap from the longest label line (11px mono, ~6.8px a glyph),
    // capped where the label wraps anyway (text-max-width above).
    var longestLabel = model.edges.reduce(function (n, e) {
      return Math.max(n, String(e.label || "").split("\n").reduce(function (m, l) {
        return Math.max(m, l.length);
      }, 0));
    }, 0);
    var layerGap = model.edge_labels
      ? Math.min(300, Math.max(112, Math.round(longestLabel * 6.8) + 56))
      : 112;

    function layout(direction) {
      return {
        name: "elk",
        // Reserve room for the rendered label box, not a nominal node size.
        nodeDimensionsIncludeLabels: true,
        fit: true,
        padding: 24,
        elk: {
          algorithm: "layered",
          "elk.direction": direction,
          // No edgeRouting option: cytoscape-elk applies ELK's node positions
          // but not its bend points, so asking for routed edges would cost
          // layout time and change nothing. The taxi curve-style draws them.
          "elk.layered.nodePlacement.strategy":
            model.nodes.length <= COMPACT_PLACEMENT_MAX_NODES
              ? "NETWORK_SIMPLEX"
              : "BRANDES_KOEPF",
          "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
          "elk.layered.cycleBreaking.strategy": "GREEDY",
          "elk.spacing.nodeNode": 42,
          "elk.spacing.edgeNode": 26,
          "elk.spacing.edgeEdge": 16,
          "elk.layered.spacing.nodeNodeBetweenLayers": layerGap,
          "elk.layered.spacing.edgeNodeBetweenLayers": 26,
          "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES"
        }
      };
    }

    cytoscape.use(cytoscapeElk);
    var direction = "RIGHT";
    var cy = cytoscape({
      container: canvas,
      elements: elements,
      style: styleFor(palette()),
      layout: layout(direction),
      wheelSensitivity: 0.2,
      maxZoom: 2.5,
      minZoom: 0.15
    });
    // ELK runs on the main thread and the canvas is empty until it finishes,
    // so the "laying out" note stays up until the first layout settles.
    cy.one("layoutstop", function () {
      root.classList.add("ready");
    });

    if (model.edge_labels) cy.edges().addClass("labelled");

    // -- theme ------------------------------------------------------------
    // Tokens were read once at build; re-read them whenever the theme moves.
    function restyle() {
      cy.style(styleFor(palette()));
    }
    if (window.MutationObserver) {
      new MutationObserver(restyle).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"]
      });
    }
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: light)");
      if (mq.addEventListener) mq.addEventListener("change", restyle);
      else if (mq.addListener) mq.addListener(restyle);
    }

    // -- hover detail ----------------------------------------------------
    var tip = root.querySelector(".relgraph-tip");

    // Table names, join keys and notes are written by agents into the knowledge
    // library, so the tooltip is assembled from nodes rather than interpolated
    // into innerHTML - none of it is trusted markup.
    function line(text, tag, cls) {
      var el = document.createElement(tag || "div");
      el.textContent = text;
      if (cls) el.className = cls;
      return el;
    }

    function leaf(fqn) {
      return fqn.split(".").pop();
    }

    function describe(el) {
      var d = el.data();
      var out = document.createDocumentFragment();
      if (el.isNode()) {
        out.appendChild(line(d.qualifier + "." + d.label, "b"));
        if (!d.known) out.appendChild(line("not described in the library yet", "i"));
        out.appendChild(line(el.connectedEdges().length + " relationship(s)"));
        return out;
      }
      var s = leaf(d.source), t = leaf(d.target);
      var head = s + " → " + t;
      if (d.cardinality) head += "  (" + d.cardinality + ")";
      out.appendChild(line(head, "b"));
      if (d.source_end) {
        out.appendChild(
          line(d.source_end + " " + s + (d.source_end === "many" ? " rows" : " row") +
            " per " + d.target_end + " " + t + (d.target_end === "many" ? " rows" : " row"),
            "div", "tip-side")
        );
      }
      if (d.keys && d.keys.length) {
        d.keys.forEach(function (k) {
          out.appendChild(line(s + "." + k.from + " = " + t + "." + k.to, "div", "tip-mono"));
        });
      } else if (d.on) {
        out.appendChild(line("join as written: " + d.on, "i"));
      } else {
        out.appendChild(line("no join key recorded", "i", "tip-warn"));
      }
      if (d.note) out.appendChild(line(d.note));
      if (d.conflict) out.appendChild(line("sides disagree: " + d.conflict, "div", "tip-warn"));
      if (d.parallel > 1) {
        out.appendChild(
          line(d.parallel + " different join keys are recorded between these tables", "div", "tip-warn")
        );
      }
      out.appendChild(
        line(d.mutual ? "declared by both tables" : "declared by " + leaf(d.declared_by[0]) + " only",
          "i", "tip-side")
      );
      return out;
    }

    cy.on("mouseover", "node, edge", function (evt) {
      var el = evt.target;
      tip.replaceChildren(describe(el));
      tip.hidden = false;
      var pos = evt.renderedPosition || { x: 0, y: 0 };
      tip.style.left = Math.round(pos.x + 14) + "px";
      tip.style.top = Math.round(pos.y + 14) + "px";
      var near = el.isNode() ? el.closedNeighborhood() : el.connectedNodes().union(el);
      cy.elements().not(near).addClass("dim");
      near.addClass("hot");
      // Dense maps hide join keys by default; reveal them for what is hovered.
      if (!model.edge_labels) near.edges().addClass("labelled");
    });
    cy.on("mouseout", "node, edge", function () {
      tip.hidden = true;
      cy.elements().removeClass("dim hot");
      if (!model.edge_labels) cy.edges().removeClass("labelled");
    });
    cy.on("tap", "node", function (evt) {
      var d = evt.target.data();
      if (d.known && !d.focus) {
        location.href = root.dataset.href.replace("__FQN__", encodeURIComponent(d.id));
      }
    });

    // Inside a fold, the canvas may have been built at zero size (the fold
    // remembered closed); refit each time the fold opens.
    for (var fold = root.closest("details"); fold; fold = fold.parentElement && fold.parentElement.closest("details")) {
      fold.addEventListener("toggle", function (evt) {
        if (evt.target.open) { cy.resize(); cy.fit(undefined, 24); }
      });
    }

    // -- controls -------------------------------------------------------
    function on(sel, fn) {
      var b = root.querySelector(sel);
      if (b) b.addEventListener("click", fn);
    }
    on(".rg-fit", function () {
      cy.fit(undefined, 24);
    });
    on(".rg-flip", function (evt) {
      direction = direction === "RIGHT" ? "DOWN" : "RIGHT";
      evt.currentTarget.textContent =
        direction === "RIGHT" ? "Left to right" : "Top to bottom";
      cy.layout(layout(direction)).run();
    });
    on(".rg-labels", function (evt) {
      var showing = cy.edges(".labelled").length > 0;
      cy.edges()[showing ? "removeClass" : "addClass"]("labelled");
      model.edge_labels = !showing;
      evt.currentTarget.setAttribute("aria-pressed", String(!showing));
      evt.currentTarget.textContent = showing ? "Show join keys" : "Hide join keys";
    });
  }

  function init() {
    // A failed render must not leave a blank panel where a diagram belongs. The
    // server-rendered relationship table below it is the fallback either way,
    // so say what went wrong and get out of the reader's way.
    document.querySelectorAll(".relgraph").forEach(function (root) {
      try {
        build(root);
      } catch (err) {
        root.classList.add("failed");
        var note = root.querySelector(".relgraph-note");
        if (note) {
          note.textContent =
            "Diagram unavailable (" + err.message + ") — the relationships are listed below.";
        }
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
