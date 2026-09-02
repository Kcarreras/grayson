/* Relationship canvas: Cytoscape.js drawing a model the server built, laid out
 * by ELK's layered algorithm (the cytoscape-elk extension).
 *
 * Layout is entirely ELK's: it assigns tables to layers by their relationships
 * and orders within a layer to minimise crossings, so nodes never land on top
 * of each other and the picture stays readable as the schema grows. Nothing
 * here positions anything by hand.
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

  function build(root) {
    var payload = document.getElementById(root.dataset.model);
    if (!payload) throw new Error("no graph model");
    var model = JSON.parse(payload.textContent);
    var canvas = root.querySelector(".relgraph-canvas");
    var t = {
      panel: token("--panel"),
      panel2: token("--panel2"),
      border: token("--border"),
      ink2: token("--ink-2"),
      muted: token("--muted"),
      faint: token("--faint"),
      act: token("--act")
    };

    var elements = model.nodes
      .map(function (n) {
        return { group: "nodes", data: n };
      })
      .concat(
        model.edges.map(function (e) {
          return { group: "edges", data: e };
        })
      );

    var style = [
      {
        selector: "node",
        style: {
          shape: "round-rectangle",
          // Sizing from the label means a long table name widens its box
          // instead of overflowing it; the old fixed-width boxes clipped names.
          width: "label",
          height: "label",
          padding: "10px",
          "background-color": t.panel2,
          "border-width": 1,
          "border-color": t.border,
          label: "data(label)",
          color: t.ink2,
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
          width: 1.4,
          "line-color": t.border,
          "target-arrow-color": t.border,
          "target-arrow-shape": "triangle",
          "arrow-scale": 0.8,
          "font-family": MONO,
          "font-size": 10,
          color: t.muted,
          // An opaque chip behind the label is what stops join keys from
          // dissolving into the lines they sit on.
          "text-background-color": t.panel,
          "text-background-opacity": 1,
          "text-background-padding": 3,
          "text-background-shape": "roundrectangle",
          "text-border-color": t.border,
          "text-border-width": 1,
          "text-border-opacity": 1,
          "text-rotation": "none"
        }
      },
      { selector: "edge.labelled", style: { label: "data(on)" } },
      {
        // Declared by one side only - usually an incomplete profile rather than
        // a different kind of relationship.
        selector: "edge[!mutual]",
        style: { "line-style": "dashed" }
      },
      { selector: ".dim", style: { opacity: 0.18 } },
      {
        selector: "edge.hot",
        style: { "line-color": t.act, "target-arrow-color": t.act, width: 2.2, color: t.act }
      },
      { selector: "node.hot", style: { "border-color": t.act, color: t.act } }
    ];

    // Node placement is the expensive step and it scales badly. Measured on a
    // synthetic warehouse schema: NETWORK_SIMPLEX takes 43 ms at 40 nodes,
    // 267 ms at 60, 729 ms at 80 and 2.5 s at 129 - past ~60 the wait is worse
    // than the payoff. BRANDES_KOEPF does 129 nodes in 263 ms; it spends about
    // 70% more vertical space, which on a big map you are panning anyway.
    var COMPACT_PLACEMENT_MAX_NODES = 60;

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
          "elk.layered.spacing.nodeNodeBetweenLayers": 96,
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
      style: style,
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

    // -- hover detail ----------------------------------------------------
    var tip = root.querySelector(".relgraph-tip");

    // Table names, join keys and notes are written by agents into the knowledge
    // library, so the tooltip is assembled from nodes rather than interpolated
    // into innerHTML - none of it is trusted markup.
    function line(text, tag) {
      var el = document.createElement(tag || "div");
      el.textContent = text;
      return el;
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
      out.appendChild(
        line(d.source.split(".").pop() + " → " + d.target.split(".").pop(), "b")
      );
      if (d.on) out.appendChild(line("on " + d.on));
      if (d.cardinality) out.appendChild(line(d.cardinality));
      if (d.note) out.appendChild(line(d.note));
      if (!d.mutual) out.appendChild(line("declared by one side only", "i"));
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
