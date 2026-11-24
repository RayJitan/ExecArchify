// All links and labels are supplied by the current query.
const SVG = "http://www.w3.org/2000/svg";
const measure = document.createElement("canvas").getContext("2d");

function shape(tag, attributes, content) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value);
  if (content != null) el.textContent = content;
  return el;
}

function caption(value, width, font) {
  const valueText = String(value || "");
  if (!measure) return valueText;
  measure.font = font;
  if (measure.measureText(valueText).width <= width) return valueText;
  let text = "";
  for (const char of valueText) {
    if (measure.measureText(text + char + "…").width > width) break;
    text += char;
  }
  return text + "…";
}

function sheet(host, width, height, title) {
  const svg = shape("svg", {width, height, viewBox: "0 0 " + width + " " + height, role: "group", "aria-label": title});
  host.replaceChildren(svg);
  return svg;
}

function glyph(type, x, y) {
  const geometries = {
    group: "M1 4h5l2 2h7v8H1z",
    workspace: "m1 6 7-3.5L15 6 8 9.5z M1 9.5 8 13l7-3.5",
    file: "M3 1.5h6l4 4v9H3z M9 2v4h4 M5.5 9h5 M5.5 11.5h3.5",
    symbol: "m5.5 4-4 4 4 4 M10.5 4l4 4-4 4",
  };
  return shape("path", {d: geometries[type] || geometries.symbol, class: "node-icon", transform: "translate(" + x + " " + y + ")"});
}

function card(svg, node, x, y, width, height) {
  const link = shape("a", {href: node.href, "aria-label": node.description || node.label, tabindex: 0, class: "graph-node " + (node.tone || "")});
  link.append(shape("title", {}, node.description || node.label));
  link.append(shape("rect", {x, y, width, height, rx: 7, class: "node-body"}));
  const compact = width < 150;
  const start = x + (compact ? 12 : 39);
  if (!compact) link.append(glyph(node.glyph, x + 12, y + height / 2 - 8));
  const available = width - (start - x) - 11;
  link.append(shape("text", {x: start, y: y + height / 2 - 2, class: "node-label"},
    caption(node.label, available, "600 13px Segoe UI, Microsoft YaHei UI, sans-serif")));
  link.append(shape("text", {x: start, y: y + height / 2 + 15, class: "node-detail"},
    caption(node.detail, available, "11px Consolas, Microsoft YaHei UI, monospace")));
  svg.append(link);
}

export function architecture(host, root, groups) {
  const width = Math.max(270, Math.min(1080, host.clientWidth));
  const height = Math.max(192, groups.length * 60 + 32);
  const svg = sheet(host, width, height, "工作区与架构分组");
  const gutter = width < 450 ? 12 : 26;
  const rootWidth = Math.min(210, Math.floor(width * 0.37));
  const groupWidth = Math.min(240, Math.floor(width * 0.43));
  const groupX = width - gutter - groupWidth;
  const end = gutter + rootWidth;
  const fork = Math.round((end + groupX) / 2);
  const middle = height / 2;
  const positions = groups.map((_, i) => middle + (i - (groups.length - 1) / 2) * 60);
  if (positions.length) {
    svg.append(shape("path", {d: "M" + end + " " + middle + "H" + fork + " M" + fork + " " + positions[0] + "V" + positions.at(-1), class: "group-wire"}));
  }
  groups.forEach((node, i) => {
    svg.append(shape("path", {d: "M" + fork + " " + positions[i] + "H" + groupX, class: "group-wire " + (node.tone || "")}));
    card(svg, node, groupX, positions[i] - 23, groupWidth, 46);
  });
  card(svg, root, gutter, middle - 29, rootWidth, 58);
  if (groups.length) svg.append(shape("circle", {cx: end, cy: middle, r: 3, class: "root-port"}));
}

// Shortest observed distances place upstream nodes left of the selected symbol
// and downstream nodes right. A cycle can legitimately draw a backward edge.
function distances(origin, edges, reverse) {
  const links = new Map();
  for (const edge of edges) {
    const from = reverse ? edge.target : edge.source;
    if (!links.has(from)) links.set(from, []);
    links.get(from).push(reverse ? edge.source : edge.target);
  }
  const result = new Map([[origin, 0]]);
  const queue = [origin];
  for (let i = 0; i < queue.length; i++) {
    const here = queue[i];
    for (const next of links.get(here) || []) {
      if (result.has(next)) continue;
      result.set(next, result.get(here) + 1);
      queue.push(next);
    }
  }
  return result;
}

function edgePath(a, b, size, variation) {
  if (a === b) {
    const start = a.x + size * 0.57;
    return "M" + start + " " + a.y + "c0 -19 31 -19 31 0";
  }
  const ay = a.y + 24 + variation;
  const by = b.y + 24 + variation;
  if (a.x === b.x) {
    const edge = a.x + size;
    const bend = edge + 20 + Math.abs(variation);
    return "M" + edge + " " + ay + "C" + bend + " " + ay + " " + bend + " " + by + " " + edge + " " + by;
  }
  const dir = a.x < b.x ? 1 : -1;
  const x1 = a.x + (dir > 0 ? size : 0);
  const x2 = b.x + (dir > 0 ? 0 : size);
  const bend = Math.max(16, Math.abs(x2 - x1) / 2);
  return "M" + x1 + " " + ay + "C" + (x1 + dir * bend) + " " + ay + " " + (x2 - dir * bend) + " " + by + " " + x2 + " " + by;
}

export function network(host, nodes, edges, origin, title) {
  const shown = nodes.slice(0, 30);
  const incoming = distances(origin, edges, true);
  const outgoing = distances(origin, edges, false);
  const columns = new Map();
  for (const node of shown) {
    const before = incoming.get(node.id);
    const after = outgoing.get(node.id);
    const travel = before != null && (after == null || before < after) ? -before : after || 0;
    const level = node.column ?? travel;
    if (!columns.has(level)) columns.set(level, []);
    columns.get(level).push(node);
  }
  const levels = [...columns.keys()].sort((a, b) => a - b);
  const cardWidth = 164;
  const spacing = 56;
  const neededWidth = levels.length * (cardWidth + spacing) - spacing + 56;
  const width = Math.max(neededWidth, host.clientWidth, 275);
  const rows = Math.max(1, ...[...columns.values()].map(column => column.length));
  const height = Math.max(205, rows * 72 + 36);
  const positions = new Map();
  levels.forEach((level, col) => {
    const members = columns.get(level);
    members.forEach((node, row) => {
      positions.set(node.id, {
        x: (width - neededWidth) / 2 + 28 + col * (cardWidth + spacing),
        y: (height - (members.length - 1) * 72 - 48) / 2 + row * 72,
      });
    });
  });
  const svg = sheet(host, width, height, title);
  const defs = shape("defs", {});
  for (const name of ["line", "selected"]) {
    const marker = shape("marker", {id: "tip-" + name, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto", markerUnits: "userSpaceOnUse"});
    marker.append(shape("path", {d: "M0,0 L8,4 L0,8 z", class: "tip-" + name}));
    defs.append(marker);
  }
  svg.append(defs);
  const pairs = new Map();
  const drawn = edges.filter(edge => positions.has(edge.source) && positions.has(edge.target)).slice(0, 60);
  for (const edge of drawn) {
    const pair = JSON.stringify([edge.source, edge.target]);
    const count = pairs.get(pair) || 0;
    pairs.set(pair, count + 1);
    const offset = count ? (count % 2 ? 1 : -1) * Math.min(16, 4 * Math.ceil(count / 2)) : 0;
    const d = edgePath(positions.get(edge.source), positions.get(edge.target), cardWidth, offset);
    const link = shape("a", {href: edge.href, tabindex: 0, class: "graph-edge " + (edge.selected ? "selected" : ""), "aria-label": edge.description});
    link.append(shape("title", {}, edge.description));
    const props = {d, class: "wire" + (edge.inferred ? " inferred" : ""), "marker-end": "url(#tip-" + (edge.selected ? "selected" : "line") + ")"};
    const wire = shape("path", props);
    link.append(shape("path", {d, class: "wire-hit"}), wire);
    svg.append(link);
    if (edge.selected || edge.caption) {
      const middle = wire.getPointAtLength(wire.getTotalLength() / 2);
      link.append(shape("text", {x: middle.x, y: middle.y - 6, class: "edge-caption"}, edge.label));
    }
  }
  for (const node of shown) {
    const point = positions.get(node.id);
    card(svg, node, point.x, point.y, cardWidth, 48);
  }
  return {nodes: shown.length, edges: drawn.length};
}
