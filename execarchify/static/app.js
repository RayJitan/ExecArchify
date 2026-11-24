import {architecture, network} from "./graph.js";

const $ = selector => document.querySelector(selector);
const primary = $("#primary");
const inspector = $("#inspector");
const titles = {overview: "架构总览", search: "符号检索", trace: "关系追踪", impact: "变更影响"};
const labels = {
  file: "文件", function: "函数", method: "方法", class: "类", struct: "结构体", enum: "枚举",
  variable: "变量", constant: "常量", interface: "接口", type: "类型", module: "模块",
  call: "调用", import: "导入", include: "包含", reference: "引用", inherits: "继承",
  indexed: "已索引", parsed: "语法解析", heuristic: "启发式", inferred: "推断", declared: "显式声明",
  root: "主仓", shared: "附加仓", worktree: "工作区", pinned: "已锁定", commit: "提交",
  missing: "修订缺失", added: "新增", modified: "修改", deleted: "删除", renamed: "重命名",
  A: "新增", M: "修改", D: "删除", R: "重命名", base: "基线", head: "目标",
};
const icons = {
  layers: "m3 7 9-4.5L21 7l-9 4.5z M3 12l9 4.5 9-4.5 M3 17l9 4.5 9-4.5",
  search: "M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z M15.5 15.5l5 5",
  graph: "M6 8v8 M8 6h3a5 5 0 0 1 5 5v4 M8 18h6 M8 6a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z M8 18a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z M19 18a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z",
  diff: "M5 3h9l5 5v13H5z M14 3v5h5 M8 12h5 M10.5 9.5v5 M8 17h6",
  refresh: "M20 10a8 8 0 1 0-1.8 7.5 M20 4v6h-6",
  repo: "M5 5a2 2 0 0 1 2-2h12v15H7a2 2 0 0 0 0 4h12 M5 5v15 M9 7h6",
  folder: "M3 7V5h7l2 2h9v12H3z",
  file: "M5 3h9l5 5v13H5z M14 3v5h5 M8 13h8 M8 16h5",
  code: "m8 7-5 5 5 5 M16 7l5 5-5 5 M14 4l-4 16",
  arrow: "M4 12h15 M14 6l6 6-6 6",
  next: "m9 5 7 7-7 7", previous: "m15 5-7 7 7 7",
  close: "m6 6 12 12 M18 6 6 18",
  copy: "M9 9h11v12H9z M5 17H3V3h12v2",
  branch: "M7 7v10 M8 13c5 0 9-2 9-6 M9 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z M9 19a2 2 0 1 1-4 0 2 2 0 0 1 4 0z M19 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
  map: "M3 9h6v6H3z M15 3h6v6h-6z M15 15h6v6h-6z M9 12h3 M12 6h3 M12 6v12h3",
  list: "M9 6h12 M9 12h12 M9 18h12 M3 6h1 M3 12h1 M3 18h1",
  info: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M12 11v5 M12 7v1",
};

let ctx = null;
let result = {};
let active = null;
let loadedKey = null;
let controller = null;
let redraw = null;
let latestItem = null;
let nodes = new Map();
let edges = new Map();
let changes = new Map();
let groupEdges = new Map();
const expandedLists = new WeakMap();
let navigation = 0;

function el(tag, cls, value) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (value != null) element.textContent = value;
  return element;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", icons[name]);
  svg.append(path);
  return svg;
}

function link(text, href, cls, iconName) {
  const a = el("a", cls);
  a.href = href;
  if (iconName) a.append(icon(iconName));
  a.append(document.createTextNode(text));
  return a;
}

function button(text, action, cls, iconName) {
  const b = el("button", cls);
  b.type = "button";
  if (iconName) b.append(icon(iconName));
  b.append(document.createTextNode(text));
  b.addEventListener("click", action);
  return b;
}

function href(view, values = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(values)) {
    if (v != null && v !== "") params.set(k, String(v));
  }
  return "#" + view + (params.size ? "?" + params : "");
}

function readRoute() {
  const hash = location.hash.slice(1);
  const split = hash.indexOf("?");
  const page = split < 0 ? hash : hash.slice(0, split);
  const params = new URLSearchParams(split < 0 ? "" : hash.slice(split + 1));
  return {view: Object.hasOwn(titles, page) ? page : "overview", p: Object.fromEntries(params)};
}

function here(updates) {
  return href(active.view, {...active.p, ...updates});
}

const resetSelection = {node: null, edge: null, change: null, info: null, bucket: null, cross: null};
const selectNode = (id, bucket) => here({...resetSelection, node: id, bucket});
const label = value => Object.hasOwn(labels, value) ? labels[value] : String(value || "—");
const tail = path => String(path || "").split(/[\\/]/).filter(Boolean).at(-1) || path || "—";
const abbreviated = revision => /^[0-9a-f]{12,64}$/i.test(revision || "") ? revision.slice(0, 9) : revision || "—";
const number = value => typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
const entityName = node => node?.qualified || node?.name || node?.path || node?.id || "—";
const groupName = name => name === "." ? "仓库根目录" : String(name);
const rootRepo = () => ctx?.repositories?.find(r => r.role === "root" || r.id === "root") || ctx?.repositories?.[0];
const repoOf = id => ctx?.repositories?.find(r => r.id === id);
const repoName = id => tail(repoOf(id)?.path || id);
const refName = value => value === "WORKTREE" ? "工作区" : value === "EMPTY" ? "空树" : abbreviated(value);
const sourceLoc = item => String(item.path || "—") + (item.line ? ":" + item.line : "");
const itemLabel = item => entityName(item) + " · " + item.repo + " · " + sourceLoc(item);

function progress(text) {
  const row = el("div", "message loading");
  row.append(el("span", "spinner"), el("span", "", text));
  return row;
}

function empty(title, body, action) {
  const block = el("div", "empty");
  block.append(icon("info"), el("h3", "", title));
  if (body) block.append(el("p", "", body));
  if (action) block.append(action);
  return block;
}

function panel(name, subtitle) {
  const box = el("section", "panel");
  const head = el("div", "panel-head");
  head.append(el("h2", "", name));
  if (subtitle) head.append(el("span", "count", subtitle));
  box.append(head);
  return {box, head};
}

function meta(pairs) {
  const dl = el("dl", "meta");
  for (const [name, value] of pairs) {
    if (value == null || value === "") continue;
    dl.append(el("dt", "", name), el("dd", "", value));
  }
  return dl;
}

function facts(items) {
  const wrapper = el("div", "stats");
  for (const [name, value, type] of items) {
    const cell = el("div", "stat");
    const desc = el("span", "stat-label");
    desc.append(icon(type), name);
    cell.append(desc, el("strong", "", number(value)));
    wrapper.append(cell);
  }
  return wrapper;
}

function gapBlock(payload) {
  const gaps = payload.gaps || [];
  if (!gaps.length && !payload.truncated) return;
  const details = el("details", "gaps");
  const summary = el("summary", "", (gaps.length ? "待核实的证据 · " + gaps.length : "查询范围") + (payload.truncated ? " · 已达返回上限" : ""));
  details.append(summary);
  if (payload.truncated) details.append(el("p", "", "当前查询已达到容量上限，可缩小范围查看其他结果。"));
  if (gaps.length) {
    const list = el("ul");
    for (const gap of gaps) list.append(el("li", "", typeof gap === "string" ? gap : gap.reason || JSON.stringify(gap)));
    details.append(list);
  }
  primary.append(details);
}

function rows(container, items, render, blockSize = 10) {
  let count = 0;
  function more(size = blockSize) {
    const end = Math.min(items.length, count + size);
    for (; count < end; count++) container.append(render(items[count], count));
    expandedLists.set(items, count);
    if (count < items.length) {
      const next = button("再显示 " + Math.min(items.length - count, blockSize) + " 项", () => {
        next.remove();
        more();
      }, "more");
      container.append(next);
    }
  }
  more(expandedLists.get(items) || blockSize);
}

function entityRow(item, compact = false, bucket) {
  const row = link("", selectNode(item.id, bucket), "result-row" + (compact ? " compact" : ""));
  if (active.p.node === item.id) row.classList.add("selected");
  row.title = itemLabel(item);
  const type = el("span", "type-box");
  type.append(icon(item.kind === "file" ? "file" : "code"));
  const body = el("span", "row-copy");
  body.append(el("strong", "", entityName(item)), el("small", "mono", (item.repo ? item.repo + " / " : "") + sourceLoc(item)));
  row.append(type, body, el("span", "kind", label(item.kind)));
  return row;
}

function edgeKey(edge) {
  return JSON.stringify([edge.source, edge.target, edge.kind, edge.repo, edge.path, edge.line, edge.pointer, edge.side]);
}

function evidenceRow(edge) {
  const a = link("", here({...resetSelection, edge: edgeKey(edge)}), "result-row edge-row");
  if (active.p.edge === edgeKey(edge)) a.classList.add("selected");
  const body = el("span", "row-copy");
  body.append(el("strong", "", (nodes.has(edge.source) ? entityName(nodes.get(edge.source)) : edge.source) + " → " + (nodes.has(edge.target) ? entityName(nodes.get(edge.target)) : edge.target)),
    el("small", "mono", (edge.repo ? edge.repo + " / " : "") + sourceLoc(edge)));
  a.append(icon("branch"), body, el("span", "kind", label(edge.kind)));
  return a;
}

function selector(text, options, value, action) {
  const field = el("label", "field");
  field.append(el("span", "", text));
  const select = el("select");
  for (const [key, name] of options) {
    const opt = el("option", "", name);
    opt.value = key;
    select.append(opt);
  }
  select.value = value;
  select.addEventListener("change", () => action(select.value));
  field.append(select);
  return field;
}

async function request(operation, args, signal) {
  const res = await fetch("/api/" + operation, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(args), signal,
  });
  const json = await res.json();
  if (!res.ok || json.error) throw new Error(json.error || String(res.status));
  return json;
}

function queryFor(route) {
  const {view, p} = route;
  if (view === "overview" && p.layout === "relations") return ["context", {architecture: true}];
  if (view === "overview" && p.group) return ["resolve", {query: "", group: p.group, limit: 10}];
  if (view === "search") return ["resolve", {query: p.q || "", group: p.group || "", repo: p.scope || "", limit: p.limit === "100" ? 100 : 30}];
  if (view === "trace" && p.id) return ["trace", {id: p.id, direction: p.direction || "both", depth: Number(p.depth || 1), limit: 30}];
  if (view === "impact") return ["impact", {base: p.base || null, head: p.head || null}];
  return null;
}

function chrome() {
  $("#title").textContent = titles[active.view];
  document.title = titles[active.view] + " · ExecArchify";
  $("#root-line").replaceChildren();
  if (ctx) {
    const name = el("span", "workspace-name", tail(ctx.root));
    name.title = ctx.root;
    $("#root-line").append(icon("repo"), name);
    const rev = rootRepo()?.revision;
    if (rev) $("#root-line").append(el("span", "crumb-slash", "/"), el("code", "", abbreviated(rev)));
  }
  for (const a of document.querySelectorAll("[data-view]")) {
    if (a.dataset.view === active.view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
    if (a.dataset.view === "trace") {
      const id = active.view === "trace" ? active.p.id : latestItem?.id;
      a.href = href("trace", {id});
    }
  }
  if (active.view === "search" && document.activeElement !== $("#global-query")) $("#global-query").value = active.p.q || "";
  $("#generation").replaceChildren();
  $("#generation").append(el("span", "status-dot" + (ctx ? "" : " waiting")),
    el("span", "", ctx ? "索引版本 " + abbreviated(String(ctx.generation ?? "—")) : "连接工作区…"));
  const repos = $("#repo-list");
  repos.replaceChildren();
  for (const repo of ctx?.repositories || []) {
    const a = link("", href("overview", {info: repo.id}), "repo-row");
    a.title = repo.path;
    const body = el("span", "row-copy");
    body.append(el("strong", "", tail(repo.path)), el("small", "", label(repo.role) + " · " + abbreviated(repo.revision)));
    a.append(icon("repo"), body);
    repos.append(a);
  }
}

function canvas(title, keys) {
  const frame = el("div", "graph-frame");
  const legend = el("div", "legend");
  for (const [text, cls] of keys) {
    const key = el("span", "legend-key");
    key.append(el("i", "swatch " + cls), text);
    legend.append(key);
  }
  const host = el("div", "viewport");
  host.tabIndex = 0;
  host.setAttribute("role", "region");
  host.setAttribute("aria-label", title);
  frame.append(legend, host);
  return {frame, host};
}

function pager(foot, page, pages) {
  if (pages < 2) return;
  const nav = el("div", "pager");
  for (const [delta, name, glyph] of [[-1, "上一页", "previous"], [1, "下一页", "next"]]) {
    const next = page + delta;
    const b = button("", () => { location.hash = here({page: next}); }, "icon-btn", glyph);
    b.disabled = next < 1 || next > pages;
    b.setAttribute("aria-label", name);
    b.title = name;
    nav.append(b);
  }
  foot.append(nav);
}

function overview() {
  const groups = ctx.groups || [];
  const stats = ctx.stats || {};
  primary.append(facts([["索引文件", stats.files, "file"], ["代码符号", stats.symbols, "code"], ["引用记录", stats.references, "branch"]]));
  const layout = ["relations", "list"].includes(active.p.layout) ? active.p.layout : "map";
  const {box, head} = panel(layout === "relations" ? "跨组关系" : "架构分组");
  const tab = el("div", "segmented");
  for (const [text, iconName, choice] of [["分组地图", "map", "map"], ["分组关系", "graph", "relations"], ["列表", "list", "list"]]) {
    const a = link(text, href("overview", {layout: choice}), "", iconName);
    if (choice === layout) a.setAttribute("aria-current", "true");
    tab.append(a);
  }
  head.append(tab);
  let relations = [];
  if (!groups.length) {
    box.append(empty("当前索引没有分组", "工作区收录文件后，分组会出现在这里。"));
  } else if (layout === "relations") {
    relations = groupMap(box);
  } else if (layout === "list") {
    const list = el("div", "result-list");
    rows(list, groups, g => {
      const a = link("", here({...resetSelection, group: g.name}), "result-row");
      if (active.p.group === g.name) a.classList.add("selected");
      a.append(icon("folder"), el("strong", "row-copy", groupName(g.name)),
        el("span", "count", g.files + " 文件 · " + g.symbols + " 符号"));
      return a;
    }, 16);
    box.append(list);
  } else {
    const perPage = 5;
    const pages = Math.ceil(groups.length / perPage);
    const selectedAt = groups.findIndex(g => g.name === active.p.group);
    const page = Math.max(1, Math.min(Number.parseInt(active.p.page, 10) || Math.floor(selectedAt / perPage) + 1, pages));
    const from = (page - 1) * perPage;
    const visible = groups.slice(from, from + perPage);
    const {frame, host} = canvas("分组地图", [["工作区归属", "membership"]]);
    box.append(frame);
    redraw = () => architecture(host, {
      label: tail(ctx.root), detail: groups.length + " 个分组", description: ctx.root,
      href: here({...resetSelection, group: null, info: rootRepo()?.id}), glyph: "workspace", tone: "root-node",
    }, visible.map(group => ({
      label: groupName(group.name), detail: group.files + " 文件 · " + group.symbols + " 符号",
      description: "打开分组 " + groupName(group.name), glyph: "group",
      href: here({...resetSelection, group: group.name, page}), tone: group.name === active.p.group ? "picked" : "",
    })));
    const foot = el("div", "graph-footer");
    foot.append(el("span", "", pages > 1 ? (from + 1) + "–" + (from + visible.length) + " / " + groups.length + " 个分组" : groups.length + " 个分组"));
    pager(foot, page, pages);
    box.append(foot);
  }
  primary.append(box);
  gapBlock(ctx);
  if (layout === "relations") {
    if (relations.length) {
      const {box: evidence} = panel("跨组关系依据", relations.length + " 组");
      const list = el("div", "result-list");
      rows(list, relations, groupRelationRow);
      evidence.append(list);
      primary.append(evidence);
    }
    return;
  }
  const languages = Object.entries(ctx.languages || {}).sort((a, b) => b[1] - a[1]);
  if (languages.length) {
    const {box: coverage} = panel("索引语言", "文件数");
    const list = el("div", "lang-list");
    for (const [language, value] of languages) {
      const line = el("div", "lang");
      const meter = el("meter");
      meter.min = 0;
      meter.max = stats.files || 1;
      meter.value = value;
      meter.setAttribute("aria-label", language);
      line.append(el("span", "mono", language), meter, el("span", "mono", number(value)));
      list.append(line);
    }
    coverage.append(list);
    primary.append(coverage);
  }
}

const crossKey = edge => JSON.stringify([edge.source_group, edge.target_group, edge.kind, edge.confidence]);
const crossHref = edge => here({...resetSelection, cross: crossKey(edge)});

function groupRelationRow(edge) {
  const a = link("", crossHref(edge), "result-row");
  if (active.p.cross === crossKey(edge)) a.classList.add("selected");
  const text = el("span", "row-copy");
  text.append(el("strong", "", groupName(edge.source_group) + " → " + groupName(edge.target_group)),
    el("small", "mono", [label(edge.confidence), edge.sample?.repo, edge.sample ? sourceLoc(edge.sample) : ""].filter(Boolean).join(" · ")));
  a.append(icon("branch"), text, el("span", "kind", label(edge.kind) + " × " + number(edge.count)));
  return a;
}

function groupMap(box) {
  const p = active.p;
  const all = result.group_edges || [];
  const byName = new Map((ctx.groups || []).map(group => [group.name, group]));
  const kinds = [...new Set(all.map(edge => edge.kind))].sort();
  const filters = el("div", "filters");
  filters.append(selector("关联分组", [["", "全部分组"], ...[...byName.keys()].map(s => [s, groupName(s)])], p.focus || "",
    focus => { location.hash = here({...resetSelection, focus}); }),
  selector("关系类型", [["", "全部关系"], ...kinds.map(s => [s, label(s)])], p.kind || "",
    kind => { location.hash = here({...resetSelection, kind}); }));
  box.append(filters);
  const relations = all.filter(e => (!p.focus || e.source_group === p.focus || e.target_group === p.focus) && (!p.kind || e.kind === p.kind));
  const scanned = Number.isInteger(result.reference_scan_count) ? "已扫描 " + number(result.reference_scan_count) + " 条引用" : "";
  if (result.group_edges_truncated) {
    box.append(el("p", "scan-note", ["已达聚合查询上限，计数仅覆盖本次返回范围。", scanned].filter(Boolean).join(" ")));
  }
  if (!relations.length) {
    box.append(empty("当前返回范围没有匹配的跨组关系", scanned, link("查看分组地图", href("overview"), "text-link")));
    return relations;
  }
  const names = [...new Set(relations.flatMap(e => [e.source_group, e.target_group]))];
  const keys = [["有向关系", "relation"]];
  if (relations.some(e => ["inferred", "heuristic"].includes(e.confidence))) keys.push(["推断关系", "uncertain"]);
  const {frame, host} = canvas("分组间的有向关系", keys);
  const foot = el("div", "graph-footer");
  const focus = p.focus || relations[0].source_group;
  const graphNodes = names.map(name => {
    const group = byName.get(name);
    return {
      id: name, label: groupName(name), detail: group ? group.files + " 文件 · " + group.symbols + " 符号" : "架构分组",
      description: "打开分组 " + groupName(name), glyph: "group", tone: name === p.focus ? "root-node" : "",
      href: href("overview", {group: name}),
    };
  });
  const pairs = new Map();
  for (const e of relations) {
    const pair = JSON.stringify([e.source_group, e.target_group]);
    pairs.set(pair, (pairs.get(pair) || 0) + 1);
  }
  const links = relations.map(e => ({
    source: e.source_group, target: e.target_group, selected: p.cross === crossKey(e), href: crossHref(e),
    inferred: ["inferred", "heuristic"].includes(e.confidence),
    label: label(e.kind) + " × " + number(e.count),
    caption: relations.length <= 6 && pairs.get(JSON.stringify([e.source_group, e.target_group])) === 1,
    description: groupName(e.source_group) + " → " + groupName(e.target_group) + " · " + label(e.kind) + " × " + number(e.count),
  }));
  redraw = () => {
    const shown = network(host, graphNodes, links, focus, "分组间的有向关系");
    const details = [names.length + " 个分组 · " + relations.length + " 组关系"];
    if (shown.nodes < names.length || shown.edges < relations.length) details.push("图中显示 " + shown.nodes + " 个分组、" + shown.edges + " 组关系");
    if (scanned && !result.group_edges_truncated) details.push(scanned);
    foot.textContent = details.join(" · ");
  };
  box.append(frame, foot);
  return relations;
}

function search() {
  const p = active.p;
  const {box, head} = panel(p.q ? "“" + p.q + "”" : "符号与文件", (result.results || []).length + " 项");
  const filters = el("div", "filters");
  filters.append(selector("架构分组", [["", "全部分组"], ...(ctx.groups || []).map(g => [g.name, groupName(g.name)])],
    p.group || "", group => { location.hash = here({...resetSelection, group}); }));
  if ((ctx.repositories || []).length > 1) {
    filters.append(selector("仓库", [["", "全部仓库"], ...ctx.repositories.map(r => [r.id, tail(r.path) + " · " + r.id])],
      p.scope || "", scope => { location.hash = here({...resetSelection, scope}); }));
  }
  head.after(filters);
  const found = result.results || [];
  if (found.length) {
    const list = el("div", "result-list");
    rows(list, found, item => entityRow(item), 15);
    box.append(list);
    const cap = p.limit === "100" ? 100 : 30;
    if (found.length === cap) {
      const foot = el("div", "graph-footer");
      foot.append(el("span", "", "当前返回上限 " + cap + " 项"));
      if (cap < 100) foot.append(link("扩大到 100 项", here({...resetSelection, limit: 100}), "text-link"));
      box.append(foot);
    }
  } else {
    box.append(empty("没有匹配结果", "可调整关键词或分组，也可按文件路径查找。",
      link("浏览索引", href("search"), "text-link")));
  }
  primary.append(box);
  gapBlock(result);
}

function traceControls() {
  const p = active.p;
  const controls = el("div", "filters");
  controls.append(selector("关系方向", [["both", "双向"], ["out", "向外"], ["in", "向内"]], p.direction || "both",
    direction => { location.hash = here({...resetSelection, direction}); }),
  selector("展开深度", [["1", "1 跳"], ["2", "2 跳"], ["3", "3 跳"], ["4", "4 跳"], ["5", "5 跳"]], p.depth || "1",
    depth => { location.hash = here({...resetSelection, depth}); }));
  return controls;
}

function graphPanel(name, items, relations, origin, legend, colors, columns) {
  const {box} = panel(name, items.length + " 个节点 · " + relations.length + " 条关系");
  const keys = [...legend];
  if (relations.some(edge => ["inferred", "heuristic"].includes(edge.confidence))) keys.push(["推断关系", "uncertain"]);
  const {frame, host} = canvas(name, keys);
  const footer = el("div", "graph-footer");
  const graphNodes = items.map(item => ({
    id: item.id, label: item.kind === "file" ? tail(item.path) : item.name || item.qualified,
    detail: label(item.kind) + (item.line ? " · L" + item.line : ""),
    description: itemLabel(item), glyph: item.kind === "file" ? "file" : "symbol",
    href: selectNode(item.id), column: columns?.get(item.id),
    tone: (colors.get(item.id) || "") + (active.p.node === item.id ? " picked" : ""),
  }));
  const graphEdges = relations.map(edge => ({
    ...edge, href: here({...resetSelection, edge: edgeKey(edge)}), selected: active.p.edge === edgeKey(edge),
    label: label(edge.kind), inferred: ["inferred", "heuristic"].includes(edge.confidence),
    description: label(edge.kind) + " · " + (nodes.get(edge.source)?.name || edge.source) + " → " +
      (nodes.get(edge.target)?.name || edge.target) + " · " + sourceLoc(edge),
  }));
  redraw = () => {
    const shown = network(host, graphNodes, graphEdges, origin, name);
    let text = relations.length ? "箭头从来源指向目标" : "当前范围内没有已定位的关系";
    if (shown.nodes < items.length || shown.edges < relations.length) {
      text += " · 图中显示 " + shown.nodes + " 个节点、" + shown.edges + " 条关系";
    }
    footer.textContent = text;
  };
  box.append(frame, footer);
  return box;
}

function edgeList(relations) {
  if (!relations.length) return;
  const {box} = panel("关系证据", relations.length + " 条");
  const list = el("div", "result-list");
  rows(list, relations, evidenceRow);
  box.append(list);
  primary.append(box);
}

function trace() {
  if (!active.p.id) {
    primary.append(empty("选择追踪起点", "在检索结果中选择一个符号或文件，然后打开关系追踪。",
      link("浏览符号与文件", href("search"), "action", "search")));
    return;
  }
  primary.append(traceControls());
  const items = result.nodes || [];
  const relations = result.edges || [];
  const origin = nodes.has(active.p.id) ? active.p.id : items[0]?.id;
  if (items.length) {
    primary.append(graphPanel("符号邻域", items, relations, origin, [["追踪起点", "focus"], ["代码关系", "relation"]], new Map([[origin, "root-node"]])));
    const list = el("details", "node-list");
    list.append(el("summary", "", "节点列表 · " + items.length));
    rows(list, items, item => entityRow(item));
    primary.append(list);
  } else primary.append(empty("起点在当前索引中不可用", "请重新检索该符号。", link("符号检索", href("search"), "text-link")));
  gapBlock(result);
  edgeList(relations);
}

function compareForm() {
  const form = el("form", "compare filters");
  const fields = [];
  for (const [name, key, placeholder] of [["基线修订", "base", "HEAD"], ["目标修订", "head", "工作区"]]) {
    const tag = el("label", "field");
    const input = el("input");
    input.name = key;
    input.type = "text";
    input.placeholder = placeholder;
    input.value = active.p[key] || "";
    input.autocomplete = "off";
    tag.append(el("span", "", name), input);
    fields.push(input);
    form.append(tag);
  }
  const submit = el("button", "action", "比较");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", event => {
    event.preventDefault();
    const [base, head] = fields.map(input => input.value.trim());
    const next = href("impact", {base, head});
    if (location.hash === next) load(true);
    else location.hash = next;
  });
  return form;
}

const buckets = [
  ["changed_symbols", "变更范围内的符号", "edit-node"],
  ["direct", "直接影响", "direct-node"],
  ["indirect", "间接影响", "indirect-node"],
  ["possible", "可能影响", "possible-node"],
  ["tests", "关联测试", "test-node"],
];
const changeKey = change => JSON.stringify([change.repo, change.path]);

function impact() {
  primary.append(compareForm());
  const files = result.changes || [];
  const direct = result.direct || [];
  const tests = result.tests || [];
  primary.append(facts([["变更文件", files.length, "diff"], ["直接影响", direct.length, "branch"], ["关联测试", tests.length, "code"]]));
  gapBlock(result);
  if (!files.length && !nodes.size) {
    primary.append(empty("所选范围没有已索引的文件差异", refName(result.base) + " → " + refName(result.head)));
    return;
  }
  const relations = result.edges || [];
  if (nodes.size) {
    const colors = new Map();
    const columns = new Map();
    for (const key of ["possible", "indirect", "direct", "tests", "changed_symbols"]) {
      const tone = buckets.find(b => b[0] === key)[2];
      for (const item of result[key] || []) {
        colors.set(item.id, tone);
        columns.set(item.id, key === "changed_symbols" ? 1 : 1 - (Number(item.depth) || 1));
      }
    }
    const visible = new Set(colors.values());
    const legend = buckets.filter(([, , tone]) => visible.has(tone)).map(([k, name, tone]) => [k === "changed_symbols" ? "变更符号" : name, tone]);
    primary.append(graphPanel("变更关系图", [...nodes.values()], relations, "", legend, colors, columns));
  }
  const {box: fileBox} = panel("变更文件", files.length + " 项");
  const fileList = el("div", "result-list");
  rows(fileList, files, change => {
    const a = link("", here({...resetSelection, change: changeKey(change)}), "result-row");
    if (active.p.change === changeKey(change)) a.classList.add("selected");
    a.append(icon("file"), el("span", "row-copy mono", change.path), el("span", "kind", label(change.status)));
    return a;
  });
  fileBox.append(fileList);
  primary.append(fileBox);
  for (const [key, name] of buckets) {
    const items = result[key] || [];
    if (!items.length) continue;
    const {box} = panel(name, items.length + " 项");
    const list = el("div", "result-list");
    rows(list, items, item => entityRow(item, false, key));
    box.append(list);
    primary.append(box);
  }
  edgeList(relations);
}

function side(title, closeTo) {
  inspector.replaceChildren();
  const head = el("div", "panel-head");
  head.append(el("h2", "", title));
  if (closeTo) {
    const close = link("", closeTo, "icon-btn", "close");
    close.title = "关闭详情";
    close.setAttribute("aria-label", close.title);
    head.append(close);
  }
  const body = el("div", "inspector-body");
  inspector.append(head, body);
  return body;
}

function feature(body, glyph, title, subtitle) {
  const mark = el("span", "feature-mark");
  mark.append(icon(glyph));
  body.append(mark, el("h3", "feature-title", title));
  if (subtitle) body.append(el("p", "feature-subtitle", subtitle));
}

function terms(body) {
  if (!ctx.terms?.length) return;
  const section = el("section", "side-section");
  section.append(el("h4", "", "工作区术语"));
  const tags = el("div", "tags");
  for (const term of ctx.terms) tags.append(link(term, href("search", {q: term}), "tag"));
  section.append(tags);
  body.append(section);
}

function workspaceInfo(repo, explicit) {
  const body = side(explicit ? "仓库详情" : "工作区详情", explicit ? here({...resetSelection}) : null);
  if (!repo) {
    body.append(empty("仓库未收录", "请刷新工作区确认当前索引范围。"));
    return;
  }
  feature(body, "repo", tail(repo.path), repo.path);
  body.append(meta([["仓库 ID", repo.id], ["角色", label(repo.role)], ["Git 修订", repo.revision || "尚无提交"],
    ["版本绑定", repo.binding === "reference" ? "参考版本" : label(repo.binding)]]));
  body.append(link("浏览仓库内容", href("search", {scope: repo.id}), "action secondary", "folder"));
  if ((ctx.repositories || []).length > 1) {
    const section = el("section", "side-section");
    section.append(el("h4", "", "本工作区仓库"));
    for (const r of ctx.repositories) {
      section.append(link(tail(r.path) + " · " + r.id, href("overview", {info: r.id}), "side-link", "repo"));
    }
    body.append(section);
  }
  terms(body);
}

function codeExcerpt(body, item) {
  const section = el("section", "side-section");
  section.append(el("h4", "", "源码片段"));
  if (typeof item.snippet !== "string" || item.snippet === "") {
    section.append(el("p", "muted", "该位置没有可显示的片段。"));
  } else {
    const pre = el("pre", "excerpt");
    pre.tabIndex = 0;
    pre.setAttribute("aria-label", "源码片段");
    const start = Number(item.snippet_line) || Number(item.line) || 1;
    const end = Number(item.end_line) || Number(item.line);
    for (const [i, text] of item.snippet.split(/\r?\n/).entries()) {
      const n = start + i;
      const line = el("span", "source-line" + (n >= item.line && n <= end ? " highlighted" : ""));
      const gutter = el("span", "line-no", n);
      gutter.setAttribute("aria-hidden", "true");
      line.append(gutter, el("code", "", text || " "));
      pre.append(line);
    }
    section.append(pre);
  }
  body.append(section);
}

function copyButton(text) {
  const b = button("复制位置", async () => {
    try {
      await navigator.clipboard.writeText(text);
      b.replaceChildren(icon("copy"), "已复制");
      $("#notice").textContent = "已复制源码位置";
    } catch {
      const fallback = el("input", "copy-fallback");
      fallback.value = text;
      fallback.readOnly = true;
      fallback.setAttribute("aria-label", "可复制的源码位置");
      b.replaceWith(fallback);
      fallback.focus();
      fallback.select();
    }
  }, "action secondary", "copy");
  return b;
}

function itemFacts(item) {
  const compare = active.view === "impact";
  const sideRef = item.side === "base" ? result.base : result.head;
  const live = !compare || (item.side === "head" && result.head === "WORKTREE");
  const binding = item.binding || (live ? repoOf(item.repo)?.binding : null);
  const revision = item.revision || (compare
    ? (item.repo === rootRepo()?.id ? sideRef : null)
    : repoOf(item.repo)?.revision);
  return [
    ["文件", item.path], ["行号", item.line ? item.line + (item.end_line > item.line ? "–" + item.end_line : "") : null],
    ["仓库", item.repo && repoName(item.repo) + " · " + item.repo],
    ["比较侧", compare && item.side ? label(item.side) + (sideRef ? " · " + refName(sideRef) : "") : null],
    ["Git 修订", revision ? (revision === "WORKTREE" ? "工作区" : revision) : null],
    ["版本绑定", binding === "reference" ? "参考版本" : binding ? label(binding) : null],
    ["来源类型", item.confidence ? label(item.confidence) : null], ["规则位置", item.pointer],
    ["传播深度", item.depth > 1 ? item.depth + " 跳" : null],
  ];
}

function reasonText(reason) {
  const known = {"exact ID": "实体 ID 完全匹配", "source text": "命中源码正文", "similar spelling": "符号拼写相近", "changed source range": "定义与变更行段相交"};
  if (Object.hasOwn(known, reason)) return known[reason];
  const incoming = /^incoming (.+) at (.+)$/u.exec(reason);
  if (incoming) return "入向" + label(incoming[1]) + " · " + incoming[2];
  const route = /^(\d+) hop\(s\) via (.+) at (.+)$/u.exec(reason);
  if (route) return route[1] + " 跳 · " + label(route[2]) + " · " + route[3];
  const match = /^(query|token|term .+) · (exact symbol|symbol|path)$/u.exec(reason);
  if (match) {
    const origin = match[1] === "query" ? "查询词" : match[1] === "token" ? "分词" : "术语 “" + match[1].slice(5) + "”";
    return origin + " · " + {"exact symbol": "符号完全匹配", symbol: "命中符号名称", path: "命中文件路径"}[match[2]];
  }
  return reason;
}

function itemInfo(item) {
  const body = side("源码依据", active.p.node ? here({...resetSelection}) : null);
  feature(body, item.kind === "file" ? "file" : "code", entityName(item), label(item.kind) + (item.group ? " · " + groupName(item.group) : ""));
  body.append(meta(itemFacts(item)));
  if (item.reason) body.append(el("p", "reason", reasonText(item.reason)));
  codeExcerpt(body, item);
  const actions = el("div", "actions");
  const live = active.view !== "impact" || (item.side === "head" && result.head === "WORKTREE");
  if (live) {
    latestItem = item;
    if (active.view !== "trace" || active.p.id !== item.id) actions.append(link("追踪关系", href("trace", {id: item.id}), "action", "graph"));
  }
  const version = item.binding === "worktree" ? "WORKTREE" : item.revision;
  actions.append(copyButton((item.repo || "") + (version ? "@" + version : "") + " " + sourceLoc(item)));
  body.append(actions);
  if (item.evidence_path?.length > 1) {
    const section = el("section", "side-section");
    section.append(el("h4", "", "影响路径"));
    const list = el("ol", "evidence-steps");
    for (const step of item.evidence_path) {
      const li = el("li");
      const details = el("details");
      details.append(el("summary", "", label(step.kind) + " · " + sourceLoc(step)));
      details.append(meta(itemFacts(step)));
      if (step.snippet) codeExcerpt(details, step);
      li.append(details);
      list.append(li);
    }
    section.append(list);
    body.append(section);
  } else if (item.evidence) {
    const section = el("section", "side-section");
    section.append(el("h4", "", "影响依据"), el("p", "mono", sourceLoc(item.evidence)));
    if (item.evidence.snippet) codeExcerpt(section, item.evidence);
    body.append(section);
  }
  const identifier = el("details", "identifier");
  identifier.append(el("summary", "", "实体 ID"), el("code", "", item.id));
  body.append(identifier);
}

function edgeInfo(edge) {
  const body = side("关系依据", here({...resetSelection}));
  feature(body, "branch", label(edge.kind), [edge.repo, sourceLoc(edge)].filter(Boolean).join(" / "));
  for (const [name, id] of [["来源", edge.source], ["目标", edge.target]]) {
    const row = el("div", "endpoint");
    const node = nodes.get(id);
    row.append(el("small", "", name), node ? link(entityName(node), selectNode(id), "text-link") : el("code", "", id));
    body.append(row);
  }
  const witness = !edge.revision && (result.direct || []).concat(result.indirect || [], result.possible || [], result.tests || [])
    .filter(n => !edge.side || n.side === edge.side)
    .map(n => n.evidence).find(e => e && e.source === edge.source && e.target === edge.target && e.repo === edge.repo &&
      e.path === edge.path && e.line === edge.line && e.kind === edge.kind);
  const item = {...edge, revision: edge.revision || witness?.revision};
  body.append(meta(itemFacts(item)));
  codeExcerpt(body, item);
  body.append(copyButton([edge.repo, sourceLoc(edge), edge.pointer].filter(Boolean).join(" ")));
}

function groupEdgeInfo(edge) {
  const body = side("跨组关系依据", here({...resetSelection}));
  feature(body, "branch", label(edge.kind) + " × " + number(edge.count),
    groupName(edge.source_group) + " → " + groupName(edge.target_group));
  if (!edge.sample) {
    body.append(el("p", "muted", "这组关系没有返回具体位置。"));
    return;
  }
  const sample = edge.sample;
  const section = el("section", "side-section");
  section.append(el("h4", "", "证据样本"), meta(itemFacts(sample)));
  codeExcerpt(section, sample);
  body.append(section);
  const actions = el("div", "actions");
  if (sample.source) actions.append(link("追踪来源", href("trace", {id: sample.source, direction: "out"}), "action", "graph"));
  if (sample.target) actions.append(link("追踪目标", href("trace", {id: sample.target, direction: "in"}), "action secondary", "branch"));
  body.append(actions);
}

function rangeText(ranges) {
  if (!Array.isArray(ranges) || !ranges.length) return "—";
  return ranges.map(([a, b]) => a === b ? "L" + a : "L" + a + "–" + b).join("、");
}

function changeInfo(change) {
  const body = side("文件变更", here({...resetSelection}));
  feature(body, "diff", tail(change.path), change.path);
  body.append(meta([["仓库", change.repo || rootRepo()?.id], ["状态", label(change.status)],
    ["基线", refName(result.base)], ["原行段", rangeText(change.old_lines)],
    ["目标", refName(result.head)], ["新行段", rangeText(change.new_lines)]]));
  const members = (result.changed_symbols || []).filter(n => n.path === change.path && n.repo === change.repo);
  if (members.length) {
    const section = el("section", "side-section");
    section.append(el("h4", "", "变更范围内的符号"));
    rows(section, members, item => entityRow(item, true, "changed_symbols"), 5);
    body.append(section);
  }
}

function groupInfo() {
  const group = ctx.groups.find(g => g.name === active.p.group);
  const body = side("分组内容", here({...resetSelection, group: null}));
  if (!group) {
    body.append(empty("该分组已不可用", "打开其他分组或刷新工作区。"));
    return;
  }
  feature(body, "folder", groupName(group.name), group.files + " 个文件 · " + group.symbols + " 个符号");
  const list = el("div", "side-results");
  rows(list, result.results || [], item => entityRow(item, true), 5);
  body.append(list, link("在符号检索中打开", href("search", {group: group.name}), "action secondary", "search"));
}

function defaultInfo() {
  if (active.view === "overview" && active.p.layout === "relations") {
    const body = side("跨组关系依据");
    body.append(empty("选择一条跨组关系", "点击连线或证据列表，可核对一个具体的来源位置并继续追踪。"));
    return;
  }
  if (active.view === "overview") {
    workspaceInfo(rootRepo(), false);
    return;
  }
  if (active.view === "impact") {
    const body = side("比较范围");
    feature(body, "diff", rootRepo() ? tail(rootRepo().path) : "当前主仓");
    body.append(meta([["基线", result.base || active.p.base || "HEAD"], ["目标", result.head === "WORKTREE" ? "工作区" : result.head || active.p.head || "工作区"]]));
    if (result.external_consumers === "out_of_scope") body.append(el("p", "scope-note", "范围限于本工作区已索引的仓库。"));
    return;
  }
  const body = side("源码依据");
  body.append(empty("选择一个符号或文件", "打开结果可查看仓库、源码行号与片段。"));
  terms(body);
}

function unavailable() {
  side("详情", here({...resetSelection})).append(empty("当前结果中没有这个位置", "可以返回列表选择其他证据。"));
}

function renderInfo(fetched) {
  const {p, view} = active;
  if (p.info) return workspaceInfo(repoOf(p.info), true);
  if (p.change) return changes.has(p.change) ? changeInfo(changes.get(p.change)) : unavailable();
  if (p.cross) return groupEdges.has(p.cross) ? groupEdgeInfo(groupEdges.get(p.cross)) : unavailable();
  if (p.edge) return edges.has(p.edge) ? edgeInfo(edges.get(p.edge)) : unavailable();
  if (p.node) {
    const record = view === "impact" && buckets.some(([key]) => key === p.bucket)
      ? (result[p.bucket] || []).find(item => item.id === p.node) : nodes.get(p.node) || fetched;
    return record ? itemInfo(record) : unavailable();
  }
  if (view === "overview" && p.group) return groupInfo();
  if (view === "trace" && nodes.size) return itemInfo(nodes.get(p.id) || nodes.values().next().value);
  defaultInfo();
}

function indexResult() {
  const items = active.view === "impact" ? buckets.flatMap(([key]) => result[key] || []) : result.nodes || result.results || [];
  nodes = new Map();
  for (const item of items) if (item.id && !nodes.has(item.id)) nodes.set(item.id, item);
  edges = new Map((result.edges || []).map(edge => [edgeKey(edge), edge]));
  changes = new Map((result.changes || []).map(change => [changeKey(change), change]));
  groupEdges = new Map((result.group_edges || []).map(edge => [crossKey(edge), edge]));
}

const sameSnapshot = (left, right) => left?.root === right?.root && left?.generation === right?.generation;

function acceptContext(next) {
  if (!sameSnapshot(ctx, next)) latestItem = null;
  ctx = next;
}

async function load(force = false) {
  const token = ++navigation;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const previous = active;
  active = readRoute();
  const operation = queryFor(active);
  const key = JSON.stringify([active.view, operation]);
  redraw = null;
  chrome();
  try {
    if (force || key !== loadedKey) {
      loadedKey = null;
      primary.setAttribute("aria-busy", "true");
      primary.replaceChildren(progress("正在读取本地索引…"));
      inspector.replaceChildren();
      if (operation?.[0] !== "context" && (!ctx || !operation || force)) acceptContext(await request("context", {}, signal));
      let next = operation ? await request(...operation, signal) : {};
      if (operation?.[0] === "context") acceptContext(next);
      if (operation && !sameSnapshot(next, ctx)) {
        acceptContext(await request("context", {}, signal));
        if (!sameSnapshot(next, ctx)) next = await request(...operation, signal);
        if (!sameSnapshot(next, ctx)) throw new Error("读取期间索引发生变化，请刷新当前视图。");
      }
      if (token !== navigation) return;
      result = next;
      loadedKey = key;
    }
    indexResult();
    let fetched;
    if (active.p.node && !nodes.has(active.p.node) && active.view !== "impact") {
      try {
        const extra = await request("context", {id: active.p.node}, signal);
        if (sameSnapshot(extra, ctx)) fetched = extra.item;
      } catch (error) {
        if (error.name === "AbortError") throw error;
      }
    }
    if (token !== navigation) return;
    const selected = active.p.node || active.p.edge || active.p.cross || active.p.change || active.p.info || (active.view === "overview" && active.p.group);
    $(".workspace-grid").classList.toggle("inspecting", Boolean(selected));
    primary.replaceChildren();
    ({overview, search, trace, impact})[active.view]();
    renderInfo(fetched);
    chrome();
    redraw?.();
    if (selected && previous && location.hash !== href(previous.view, previous.p)) {
      inspector.focus({preventScroll: true});
      if (matchMedia("(max-width: 760px)").matches) inspector.scrollIntoView({block: "start"});
    } else if (previous && previous.view !== active.view) {
      $("#main-content").focus({preventScroll: true});
      window.scrollTo(0, 0);
    }
    $("#notice").textContent = titles[active.view] + "已更新";
  } catch (error) {
    if (token !== navigation || error.name === "AbortError") return;
    result = {};
    loadedKey = null;
    chrome();
    primary.replaceChildren();
    if (active.view === "impact") primary.append(compareForm());
    primary.append(empty("读取失败", error.message, button("重新读取", () => load(true), "action", "refresh")));
    inspector.replaceChildren();
    $("#notice").textContent = "读取失败，" + error.message;
  } finally {
    if (token === navigation) primary.setAttribute("aria-busy", "false");
  }
}

for (const holder of document.querySelectorAll("[data-icon]")) holder.replaceWith(icon(holder.dataset.icon));
$("#top-search").addEventListener("submit", event => {
  event.preventDefault();
  location.hash = href("search", {q: $("#global-query").value.trim()});
  $("#global-query").blur();
});
$("#reload").addEventListener("click", () => load(true));
$(".skip").addEventListener("click", event => { event.preventDefault(); $("#main-content").focus(); });
document.addEventListener("keydown", event => {
  const typing = event.target.matches("input,textarea,select,[contenteditable='true']");
  if (event.key === "/" && !typing && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    $("#global-query").focus();
    $("#global-query").select();
  }
  if (event.key === "Escape") {
    if (typing) event.target.blur();
    else if (active.p.node || active.p.edge || active.p.cross || active.p.change || active.p.info) location.hash = here({...resetSelection});
    else if (active.view === "overview" && active.p.group) location.hash = here({group: null});
  }
});
let lastWidth = 0;
new ResizeObserver(entries => {
  const width = entries[0].contentRect.width;
  if (width !== lastWidth) {
    lastWidth = width;
    redraw?.();
  }
}).observe(primary);
window.addEventListener("hashchange", () => load());
load();
