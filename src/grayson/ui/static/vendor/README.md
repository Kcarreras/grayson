# Vendored graph libraries

The console has no build step and makes no network requests, so the relationship
canvas ships its dependencies as committed files rather than as an npm build or
a CDN link. Served from loopback by `grayson.ui.server.static_asset`.

| File | Package | Version | License | Source |
| --- | --- | --- | --- | --- |
| `cytoscape.min.js` | [cytoscape](https://js.cytoscape.org/) | 3.31.1 | MIT | `https://unpkg.com/cytoscape@3.31.1/dist/cytoscape.min.js` |
| `elk.bundled.js` | [elkjs](https://github.com/kieler/elkjs) | 0.9.3 | EPL-2.0 | `https://unpkg.com/elkjs@0.9.3/lib/elk.bundled.js` |
| `cytoscape-elk.js` | [cytoscape-elk](https://github.com/cytoscape/cytoscape.js-elk) | 2.2.0 | MIT | `https://unpkg.com/cytoscape-elk@2.2.0/dist/cytoscape-elk.js` |

`elk.bundled.js` is the bulk of it (1.5 MB): elkjs is the Eclipse Layout Kernel
cross-compiled from Java, and the bundled build inlines its web worker so no
second file is needed. Only pages that draw a canvas load any of this — see
`templates/_graph_assets.html`.

## Upgrading

Replace the file, update the version here, and re-check the layout timings
recorded in `../graph.js` (`COMPACT_PLACEMENT_MAX_NODES`) — they are what decide
when the compact-but-slow node placement gives way to the fast one.

```bash
curl -sLo cytoscape.min.js https://unpkg.com/cytoscape@<version>/dist/cytoscape.min.js
```

Browsers cache these for a year, keyed on the `?v=<grayson version>` the
templates append, so an upgrade must also bump `grayson.__version__` to be picked
up by anyone who already loaded the old bundle.
