/* NooNoo front end. Plain JS, no build step. */
(() => {
"use strict";

// ---------------------------------------------------------------- utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
const SHELL = params.get("shell") || "browser";      // app (NooNoo.app window) | browser
const AUTHOR = "Alex Stuart";
const FRAME = params.get("frame") || "xp";           // xp | native

async function api(path, body) {
  const opts = { headers: { "X-Token": window.TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const fmt = {
  size(b) {
    if (b == null || isNaN(b)) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i < 2 ? Math.round(b) : b.toFixed(b < 10 ? 2 : 1)) + " " + u[i];
  },
  dur(s) {
    if (s == null || isNaN(s)) return "";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  },
  speed(b) { return b ? fmt.size(b) + "/s" : ""; },
  views(n) {
    if (n == null) return "";
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
    return String(n);
  },
  date(d) {  // yyyymmdd -> yyyy-mm-dd
    return d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : "";
  },
  time(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  },
};

const store = {
  get(k, d) { try { const v = localStorage.getItem("nn." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("nn." + k, JSON.stringify(v)); } catch {} },
};

function copyText(t) {
  const ta = document.createElement("textarea");
  ta.value = t; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); } catch {}
  ta.remove();
  navigator.clipboard?.writeText(t).catch(() => {});
}

// ---------------------------------------------------------------- global state
const S = {
  settings: null,
  defaults: null,
  page: "search",
  searches: [],
  active: null,
  jobs: [],
  jobVersion: -1,
  jobSel: new Set(),
  lib: { files: [], root: "", folder: "*", filter: "" },
  libSel: new Set(),
  libDirty: true,
  flash: null,
};

// ---------------------------------------------------------------- ListView (XP list control)
const colPrefs = () => { try { return JSON.parse(S.settings?.columns || "{}") || {}; } catch { return {}; } };

class ListView {
  constructor(wrap, name, cols, o = {}) {
    Object.assign(this, { wrap, name, allCols: cols, o });
    this.items = [];
    this.sel = o.sel || new Set();
    this.anchor = null;
    this.sort = store.get(`sort.${name}`, o.sort || null);
    this.widths = store.get(`widths.${name}`, {});
    this.empty = o.empty || "";
    wrap.tabIndex = 0;
    wrap.addEventListener("mousedown", (e) => this._down(e));
    wrap.addEventListener("dblclick", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (tr && o.onDbl) o.onDbl(this.byId(tr.dataset.id));
    });
    wrap.addEventListener("contextmenu", (e) => {
      if (e.target.closest("thead")) { e.preventDefault(); return this._colMenu(e); }
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      e.preventDefault();
      if (!this.sel.has(tr.dataset.id)) this.select([tr.dataset.id]);
      o.onContext && o.onContext(e, this.selected());
    });
    wrap.addEventListener("keydown", (e) => this._key(e));
    wrap.addEventListener("focus", () => wrap.classList.add("focused"));
    if (o.tooltip) {
      wrap.addEventListener("mouseover", (e) => {
        const tr = e.target.closest("tr[data-id]");
        if (tr) Tip.schedule(e, () => o.tooltip(this.byId(tr.dataset.id)));
      });
      wrap.addEventListener("mouseleave", () => Tip.hide());
      wrap.addEventListener("mousemove", (e) => Tip.move(e));
    }
  }
  // visible columns, in the user's order (column chooser + header drag), minus hidden ones
  get cols() {
    const p = colPrefs()[this.name] || {};
    const hidden = new Set(p.hidden || this.allCols.filter((c) => c.hidden).map((c) => c.key));
    const byKey = Object.fromEntries(this.allCols.map((c) => [c.key, c]));
    const order = (p.order || []).filter((k) => byKey[k]);
    const keys = [...order];
    this.allCols.forEach((c, i) => {
      if (keys.includes(c.key)) return;
      const prev = this.allCols.slice(0, i).reverse().find((x) => keys.includes(x.key));
      keys.splice(prev ? keys.indexOf(prev.key) + 1 : 0, 0, c.key);
    });
    return keys.map((k) => byKey[k]).filter((c) => c.fixed || (c.visible ? c.visible() : !hidden.has(c.key)));
  }
  savePrefs(patch) {
    const all = colPrefs();
    all[this.name] = { ...(all[this.name] || {}), ...patch };
    if (patch === null) delete all[this.name];
    return saveSettings({ columns: JSON.stringify(all) }).then(() => this.render());
  }
  _colMenu(e) {
    const shown = new Set(this.cols.map((c) => c.key));
    const p = colPrefs()[this.name] || {};
    const items = this.allCols.filter((c) => !c.fixed).map((c) => ({
      label: c.title || c.label || c.key, checked: shown.has(c.key),
      run: () => c.toggle ? c.toggle(!shown.has(c.key)) : this.savePrefs({
        hidden: shown.has(c.key) ? [...this.allCols.filter((x) => !shown.has(x.key)).map((x) => x.key), c.key]
                                 : this.allCols.filter((x) => !shown.has(x.key) && x.key !== c.key).map((x) => x.key) }),
    }));
    Menu.show(e.clientX, e.clientY, [...items, ...(this.o.colMenuExtra ? ["-", ...this.o.colMenuExtra()] : []), "-",
      { label: "Reset Columns", run: () => { this.widths = {}; store.set(`widths.${this.name}`, {}); this.savePrefs(null); }, disabled: !p.order && !p.hidden && !Object.keys(this.widths).length }]);
  }
  byId(id) { return this.items.find((i) => this.o.id(i) === id); }
  selected() { return this.view.filter((i) => this.sel.has(this.o.id(i))); }
  setItems(items, sel) {
    this.items = items;
    if (sel) this.sel = sel;
    const ids = new Set(items.map(this.o.id));
    for (const id of [...this.sel]) if (!ids.has(id)) this.sel.delete(id);
    this.render();
  }
  select(ids, add = false) {
    if (!add) this.sel.clear();
    ids.forEach((id) => this.sel.add(id));
    this._paintSel();
    this.o.onSel && this.o.onSel(this.selected());
  }
  _sorted() {
    const v = this.items.slice();
    if (this.sort) {
      const col = this.cols.find((c) => c.key === this.sort.key);
      if (col) {
        const get = col.sortVal || ((i) => i[col.key]);
        const dir = this.sort.desc ? -1 : 1;
        v.sort((a, b) => {
          let x = get(a), y = get(b);
          if (x == null || x === "") return 1;
          if (y == null || y === "") return -1;
          if (typeof x === "string") return x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" }) * dir;
          return (x - y) * dir;
        });
      }
    }
    return v;
  }
  render() {
    const top = this.wrap.scrollTop;
    this.view = this._sorted();
    const cols = this.cols;
    const head = cols.map((c) => {
      const sorted = this.sort && this.sort.key === c.key;
      return `<th data-key="${c.key}" class="${c.cls || ""} ${sorted ? "sorted" + (this.sort.desc ? " desc" : "") : ""}" title="${esc(c.title || c.label)}">${c.label}<span class="rz"></span></th>`;
    }).join("");
    const colgroup = cols.map((c) => {
      const w = this.widths[c.key] ?? c.w;
      return `<col style="${w ? `width:${w}px` : ""}">`;
    }).join("");
    let body = "";
    if (this.o.before) body += this.o.before() || "";
    for (const it of this.view) {
      const id = this.o.id(it);
      const cls = (this.sel.has(id) ? "sel " : "") + (this.o.rowClass ? this.o.rowClass(it) : "");
      body += `<tr data-id="${esc(id)}" class="${cls}">` +
        cols.map((c) => `<td class="${c.cls || ""}">${c.render ? c.render(it) : esc(it[c.key])}</td>`).join("") + "</tr>";
    }
    this.wrap.querySelector("table")?.remove();
    const thumbCls = cols.some((c) => c.thumb) ? "thumbs-" + (S.settings?.thumbs === "large" ? "large" : "small") : "";
    const emptyEl = this.wrap.querySelector(".empty-state");
    if (emptyEl) emptyEl.classList.toggle("hidden", !!this.view.length || !!(this.o.before && this.o.before()));
    this.wrap.insertAdjacentHTML("beforeend",
      `<table class="lv ${thumbCls}"><colgroup>${colgroup}</colgroup><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
    if (!this.view.length && !emptyEl && this.empty && !(this.o.before && this.o.before())) {
      this.wrap.querySelector("tbody").innerHTML = `<tr class="row-msg"><td colspan="${cols.length}">${this.empty}</td></tr>`;
    }
    this.wrap.scrollTop = top;
  }
  _paintSel() {
    $$("tbody tr[data-id]", this.wrap).forEach((tr) => tr.classList.toggle("sel", this.sel.has(tr.dataset.id)));
  }
  _down(e) {
    const rz = e.target.closest(".rz");
    if (rz) return this._resize(e, rz.parentElement);
    const th = e.target.closest("th");
    if (th) { if (e.button === 0) this._headerDrag(e, th); return; }
    const tr = e.target.closest("tr[data-id]");
    if (!tr) { if (e.button === 0) this.select([]); return; }
    if (e.button === 2 && this.sel.has(tr.dataset.id)) return;
    const id = tr.dataset.id;
    if (e.shiftKey && this.anchor) {
      const ids = this.view.map(this.o.id);
      const a = ids.indexOf(this.anchor), b = ids.indexOf(id);
      if (a >= 0 && b >= 0) return this.select(ids.slice(Math.min(a, b), Math.max(a, b) + 1), e.metaKey);
    }
    if (e.metaKey || e.ctrlKey) {
      this.sel.has(id) ? this.sel.delete(id) : this.sel.add(id);
      this.anchor = id;
      this._paintSel();
      this.o.onSel && this.o.onSel(this.selected());
      return;
    }
    this.anchor = id;
    this.select([id]);
  }
  _key(e) {
    const ids = this.view.map(this.o.id);
    if ((e.metaKey || e.ctrlKey) && e.key === "a") { e.preventDefault(); this.select(ids); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const cur = ids.indexOf(this.anchor);
      const nxt = Math.max(0, Math.min(ids.length - 1, cur + (e.key === "ArrowDown" ? 1 : -1)));
      if (ids[nxt]) {
        this.anchor = ids[nxt];
        this.select([ids[nxt]], false);
        $(`tr[data-id="${CSS.escape(ids[nxt])}"]`, this.wrap)?.scrollIntoView({ block: "nearest" });
      }
      return;
    }
    if (e.key === "Enter" && this.o.onEnter) { e.preventDefault(); this.o.onEnter(this.selected()); }
    if ((e.key === "Delete" || e.key === "Backspace") && this.o.onDelete) { e.preventDefault(); this.o.onDelete(this.selected()); }
  }
  // click = sort; drag sideways = move the column
  _headerDrag(e, th) {
    e.preventDefault();
    const key = th.dataset.key, x0 = e.clientX;
    const ths = [...th.parentElement.children];
    let moved = false, target = null;
    const mv = (ev) => {
      if (!moved && Math.abs(ev.clientX - x0) < 6) return;
      moved = true;
      th.classList.add("col-dragging");
      ths.forEach((h) => h.classList.remove("drop-before", "drop-after"));
      target = ths.find((h) => { const r = h.getBoundingClientRect(); return ev.clientX >= r.left && ev.clientX < r.right; });
      if (target && target !== th) {
        const r = target.getBoundingClientRect();
        target.classList.add(ev.clientX < r.left + r.width / 2 ? "drop-before" : "drop-after");
      }
    };
    const up = () => {
      document.removeEventListener("mousemove", mv);
      document.removeEventListener("mouseup", up);
      if (!moved) {
        this.sort = this.sort && this.sort.key === key ? (this.sort.desc ? null : { key, desc: true }) : { key, desc: false };
        store.set(`sort.${this.name}`, this.sort);
        return this.render();
      }
      const after = target && target.classList.contains("drop-after");
      if (!target || target === th) return this.render();
      const order = this.cols.map((c) => c.key).filter((k) => k !== key);
      order.splice(order.indexOf(target.dataset.key) + (after ? 1 : 0), 0, key);
      const rest = this.allCols.map((c) => c.key).filter((k) => !order.includes(k));
      this.savePrefs({ order: [...order, ...rest] });
    };
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", up);
  }
  _resize(e, th) {
    e.preventDefault();
    const key = th.dataset.key, x0 = e.clientX, w0 = th.getBoundingClientRect().width;
    const col = this.wrap.querySelectorAll("col")[[...th.parentElement.children].indexOf(th)];
    const mv = (ev) => { col.style.width = Math.max(24, w0 + ev.clientX - x0) + "px"; };
    const up = (ev) => {
      document.removeEventListener("mousemove", mv);
      document.removeEventListener("mouseup", up);
      this.widths[key] = Math.max(24, Math.round(w0 + ev.clientX - x0));
      store.set(`widths.${this.name}`, this.widths);
    };
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", up);
  }
}

// ---------------------------------------------------------------- tooltip
const Tip = {
  el: null, timer: null,
  schedule(e, fn) {
    clearTimeout(this.timer);
    this.hide();
    this.timer = setTimeout(() => {
      const html = fn();
      if (!html) return;
      this.el.innerHTML = html;
      this.el.style.display = "block";
      this.move(e);
    }, 550);
  },
  move(e) {
    if (this.el.style.display !== "block") { this._last = e; return; }
    const r = this.el.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 18;
    if (x + r.width > innerWidth - 4) x = innerWidth - r.width - 4;
    if (y + r.height > innerHeight - 4) y = e.clientY - r.height - 8;
    this.el.style.left = x + "px";
    this.el.style.top = y + "px";
  },
  hide() { clearTimeout(this.timer); if (this.el) this.el.style.display = "none"; },
};
function mediaTip(it) {
  if (!it) return "";
  const meta = [it.uploader, it.site, fmt.dur(it.duration), it.views != null ? fmt.views(it.views) + " views" : ""].filter(Boolean).join(" · ");
  return `<div class="tt">${it.thumbnail ? `<img src="${esc(it.thumbnail)}" referrerpolicy="no-referrer">` : ""}<div>
    <div class="tt-t">${esc(it.title)}</div><div class="tt-m">${esc(meta)}</div>
    ${it.playlist ? `<div class="tt-m">Playlist: ${esc(it.playlist)}</div>` : ""}</div></div>`;
}

// ---------------------------------------------------------------- menus
const Menu = {
  open: null,
  show(x, y, items, owner) {
    this.close();
    const el = document.createElement("div");
    el.className = "dropdown";
    el.innerHTML = items.map((it, i) => it === "-" ? `<div class="sep"></div>` :
      `<div class="mi ${it.disabled ? "disabled" : ""} ${it.checked ? "checked" : ""} ${it.radio ? "radio" : ""}" data-i="${i}">${esc(it.label)}${it.key ? `<span class="key">${esc(it.key)}</span>` : ""}</div>`).join("");
    $("#menus").appendChild(el);
    const r = el.getBoundingClientRect();
    el.style.left = Math.min(x, innerWidth - r.width - 4) + "px";
    el.style.top = Math.min(y, innerHeight - r.height - 4) + "px";
    el.addEventListener("mousedown", (e) => e.stopPropagation());
    el.addEventListener("click", (e) => {
      const mi = e.target.closest(".mi");
      if (!mi || mi.classList.contains("disabled")) return;
      const it = items[+mi.dataset.i];
      this.close();
      it.run && it.run();
    });
    this.open = { el, owner };
    owner && owner.classList.add("open");
  },
  close() {
    if (!this.open) return;
    this.open.el.remove();
    this.open.owner && this.open.owner.classList.remove("open");
    this.open = null;
  },
};
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".menu")) Menu.close(); });

const MENUS = {
  file: () => [
    { label: "Paste Link & Search", key: "⌘V", run: pasteAndSearch },
    { label: "New Search", key: "⌘N", run: newSearch },
    "-",
    { label: "Open Download Folder", key: "⌘O", run: () => api("/api/open", { what: "folder" }) },
    { label: "Options...", key: "⌘,", run: () => openOptions() },
    "-",
    { label: "Exit", key: "⌘Q", run: () => winAction("close"), disabled: !NATIVE },
  ],
  view: () => [
    { label: "Filter Panel", checked: !document.body.classList.contains("no-filters"), run: () => { document.body.classList.toggle("no-filters"); $("#page-search .sidebar").classList.toggle("hidden"); } },
    { label: "Download Details Pane", checked: !$("#job-details").classList.contains("hidden"), run: () => { $("#job-details").classList.toggle("hidden"); $("#job-splitter").classList.toggle("hidden"); } },
    "-",
    ...[["blue", "Luna Blue"], ["olive", "Olive Green"], ["silver", "Silver"]].map(([v, l]) => ({
      label: l, radio: S.settings.theme === v, run: () => saveSettings({ theme: v }),
    })),
  ],
  nav: () => [
    { label: "Search", key: "⌘1", run: () => showPage("search") },
    { label: "Monitor", key: "⌘2", run: () => showPage("monitor") },
    { label: "Library", key: "⌘3", run: () => showPage("library") },
    "-",
    { label: "Close Search Tab", key: "⌘W", run: () => S.active && closeSearch(S.active.id), disabled: !S.active },
  ],
  tools: () => [
    { label: "Options...", key: "⌘,", run: () => openOptions() },
    { label: "Update Download Engine", run: () => openOptions("engine") },
    "-",
    { label: "Pause All Downloads", run: () => api("/api/jobs/bulk", { action: "pause_all" }).then(poll) },
    { label: "Resume All Downloads", run: () => api("/api/jobs/bulk", { action: "resume_all" }).then(poll) },
    { label: "Clear Inactive Downloads", run: () => api("/api/jobs/bulk", { action: "clear" }).then(poll) },
  ],
  help: () => [
    { label: "Supported Sites", run: () => openUrl("https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md") },
    { label: "Downloading from Instagram, Facebook & X...", run: cookiesHelp },
    { label: "Keyboard Shortcuts", run: shortcutsHelp },
    "-",
    { label: "Check for Updates...", run: () => checkUpdates(true) },
    { label: "About NooNoo", run: about },
  ],
};
$$("#menubar .menu").forEach((m) => {
  m.addEventListener("mousedown", (e) => {
    e.stopPropagation();
    if (Menu.open && Menu.open.owner === m) return Menu.close();
    const r = m.getBoundingClientRect();
    Menu.show(r.left, r.bottom, MENUS[m.dataset.menu](), m);
  });
  m.addEventListener("mouseenter", () => {
    if (Menu.open && Menu.open.owner && Menu.open.owner !== m) {
      const r = m.getBoundingClientRect();
      Menu.show(r.left, r.bottom, MENUS[m.dataset.menu](), m);
    }
  });
});

// ---------------------------------------------------------------- dialogs
function dialog({ title, body, buttons = [], cls = "", width, onClose, modal = true }) {
  const back = document.createElement("div");
  back.className = modal ? "modal-back" : "";
  back.innerHTML = `<div class="xp-window ${cls}" ${width ? `style="width:${width}px"` : ""}>
      <div class="xw-title"><span>${esc(title)}</span><button class="tb-btn tb-close" title="Close"></button></div>
      <div class="xw-body"></div><div class="xw-foot"></div></div>`;
  const win = back.querySelector(".xp-window");
  const bodyEl = back.querySelector(".xw-body");
  typeof body === "string" ? (bodyEl.innerHTML = body) : bodyEl.appendChild(body);
  const foot = back.querySelector(".xw-foot");
  let closed = false;
  const close = (v) => { if (closed) return; closed = true; back.remove(); onClose && onClose(v); };
  buttons.forEach((b) => {
    const btn = document.createElement("button");
    btn.className = "xp-btn" + (b.default ? " default" : "") + (b.left ? " left" : "");
    btn.textContent = b.label;
    btn.onclick = () => { const r = b.run ? b.run(close) : undefined; if (r !== false && !b.keepOpen) close(b.value); };
    foot.appendChild(btn);
  });
  if (!buttons.length) foot.remove();
  back.querySelector(".tb-close").onclick = () => close(null);
  // drag by title bar
  const tb = back.querySelector(".xw-title");
  tb.addEventListener("mousedown", (e) => {
    if (e.target.closest(".tb-btn")) return;
    const r = win.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
    win.style.position = "fixed";
    const mv = (ev) => { win.style.left = ev.clientX - dx + "px"; win.style.top = Math.max(0, ev.clientY - dy) + "px"; win.style.right = "auto"; win.style.bottom = "auto"; };
    const up = () => { document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up); };
    mv(e);
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", up);
  });
  $("#dialogs").appendChild(back);
  paintIcons(back);
  back._close = close;
  return { el: back, win, body: bodyEl, close };
}
function msgbox(title, text, { icon: ic = "info32", buttons = [{ label: "OK", default: true, value: true }] } = {}) {
  return new Promise((res) => {
    dialog({ title, cls: "msgbox", buttons, onClose: res,
      body: `<div data-icon="${ic}"></div><div class="msg">${text}</div>` });
  });
}
const confirmBox = (title, text, ok = "OK") =>
  msgbox(title, text, { icon: "warn32", buttons: [{ label: ok, default: true, value: true }, { label: "Cancel", value: false }] });

function about() {
  api("/api/tools").then((t) => {
    const v = t.status;
    const ver = S.stats?.update?.current;
    msgbox("About NooNoo", `<b style="font-size:13px">NooNoo</b>${ver ? " " + esc(ver) : ""} — your own media downloader.
Designed and built by <b>${esc(AUTHOR)}</b>.

No limits, no ads. Powered by:
  yt-dlp ${esc(v["yt-dlp"] || "?")}   ·   ffmpeg ${esc(v.ffmpeg || "?")}   ·   deno ${esc(v.deno || "?")}

© ${new Date().getFullYear()} ${esc(AUTHOR)}`, { icon: "logoBig" });
  });
}
function cookiesHelp() {
  msgbox("Instagram, Facebook, X & private videos", `Many sites only show media to logged-in users.

NooNoo can borrow your login from a browser you already use:
<b>Tools › Options › Accounts</b> → pick that browser.

• Chrome/Brave/Edge: macOS will ask for Keychain access — <a href="#" data-keychain>what that prompt means</a>.
• Safari: give the terminal / NooNoo “Full Disk Access” in System Settings › Privacy.
• Firefox works without prompts.

Only use this for content you're allowed to download.`, { icon: "info32" });
  $$("#dialogs [data-keychain]").pop().onclick = (e) => { e.preventDefault(); keychainHelp(KEYCHAIN_NAMES[S.settings.cookies_browser] ? S.settings.cookies_browser : "chrome"); };
}
// Chromium browsers lock their cookies with a key kept in the login Keychain under "<name> Safe Storage".
const KEYCHAIN_NAMES = { chrome: "Chrome", brave: "Brave", edge: "Microsoft Edge", chromium: "Chromium", opera: "Opera", vivaldi: "Vivaldi" };
function keychainHelp(browser) {
  const name = KEYCHAIN_NAMES[browser];
  if (!name) return;
  const item = esc(name + " Safe Storage");
  msgbox("About the Keychain prompt", `When NooNoo first uses your ${esc(name)} login, macOS shows its own window:

  <i>“security wants to use your confidential information stored in “${item}” in your keychain.”</i>

That's expected, and it's safer than it sounds:
• <b>security</b> is Apple's built-in Keychain tool. yt-dlp uses it to ask macOS for one item, nothing else.
• <b>${item}</b> is the key ${esc(name)} locks its cookies with. It isn't your saved passwords, card details or Mac password.
• The password box belongs to macOS, not NooNoo. It wants your Keychain password (usually your Mac password). macOS checks it; NooNoo never sees it.

Which button?
• <b>Allow</b> — lets it through this once. Most private; macOS asks again next time.
• <b>Always Allow</b> — no more questions, but any app that uses Apple's security tool could then read this key without asking.
• <b>Deny</b> — fine too. NooNoo just stays logged out.

Changed your mind? Keychain Access › login › “${item}” › Access Control.`, { icon: "info32" });
}
function shortcutsHelp() {
  msgbox("Keyboard Shortcuts", `⌘V            Paste link(s) and search
⌘F / ⌘N       Focus search box / new search
Enter          Download selected (audio with ⇧Enter)
⌫ / Delete     Junk results · remove downloads
⌘A             Select all
⌘1 ⌘2 ⌘3      Search / Monitor / Library
⌘W             Close search tab
⌘,             Options
Space          Play / pause in Library`, { icon: "info32" });
}

// ---------------------------------------------------------------- NooNoo.app bridge
// In the app, window chrome, folder pickers and the clipboard are handled natively (Swift);
// dragging the title bar and the size grip are handled there too, so they track the cursor exactly.
const NATIVE = SHELL === "app" && window.webkit?.messageHandlers?.noonoo;
const nativeWait = {};
let nativeSeq = 0;
window.__nativeReply = (id, value) => { nativeWait[id]?.(value); delete nativeWait[id]; };
function native(action, data = {}) {
  if (!NATIVE) return Promise.resolve(null);
  return new Promise((res) => { const id = ++nativeSeq; nativeWait[id] = res; NATIVE.postMessage({ id, action, ...data }); });
}
function blink(host) {
  const svg = host && host.querySelector("svg:has(.nn-eyes)");
  if (!svg || svg.classList.contains("blink")) return;
  svg.classList.add("blink");
  svg.addEventListener("animationend", () => svg.classList.remove("blink"), { once: true });
}
// the app's title bar strip belongs to macOS, so the app tells us when the pointer reaches the icon
window.noonooBlink = (sel) => blink($(sel));
// app menu bar items (NooNoo › About, Preferences...) call in here
window.noonooMenu = (cmd) => ({ about, options: () => openOptions(), paste: pasteAndSearch, updates: () => checkUpdates(true),
  search: () => showPage("search"), monitor: () => showPage("monitor"), library: () => showPage("library") }[cmd] || (() => {}))();
function winAction(action) { return native(action); }
$$("[data-win]").forEach((b) => b.addEventListener("click", () => winAction(b.dataset.win)));
async function pickFolder(start) {
  if (NATIVE) return native("chooseFolder", { start });
  return (await api("/api/choose-folder", { start })).path;
}

// ---------------------------------------------------------------- pages
function showPage(p) {
  S.page = p;
  $$(".maintab").forEach((t) => t.classList.toggle("active", t.dataset.page === p));
  $$(".page").forEach((pg) => pg.classList.toggle("active", pg.id === "page-" + p));
  if (p === "library" && S.libDirty) loadLibrary();
  if (p === "monitor") { renderJobs(); $("#jobs-wrap").focus(); }
  if (p === "search") renderSearch();
}
$$(".maintab").forEach((t) => t.addEventListener("click", () => showPage(t.dataset.page)));

function openUrl(u) { return api("/api/open", { what: "url", path: u }).catch((e) => flash(e.message)); }

function flash(msg, ms = 4000) {
  S.flash = { msg, until: Date.now() + ms };
  renderStatus();
}

// ================================================================= SEARCH
let searchSeq = 0;
const VIDEO_Q = [["best", "Best available"], ["2160", "2160p (4K)"], ["1440", "1440p"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"], ["360", "360p"]];
const AUDIO_F = [["mp3", "MP3"], ["m4a", "M4A (AAC)"], ["opus", "Opus"], ["flac", "FLAC"], ["wav", "WAV"], ["wav_avid", "WAV 48k/24-bit (Avid)"], ["best", "Original"]];
const FORMAT_GROUPS = [["Everyday", (k) => k === "h264"],
                       ["Avid HD — 1920×1080", (k) => k.startsWith("dnxhd")],
                       ["Avid 4K & other sizes", (k) => k.startsWith("dnxhr")]];
const FPS = [["source", "Match source"], ["23.976", "23.976 fps"], ["24", "24 fps"], ["25", "25 fps"], ["29.97", "29.97 fps"],
             ["30", "30 fps"], ["50", "50 fps"], ["59.94", "59.94 fps"], ["60", "60 fps"]];

// <select> options for video formats: everyday ones, then editing (Avid / Premiere / Resolve) ones
function vformatOptions(cur) {
  const o = (k) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(S.videoFormats[k])}</option>`;
  return FORMAT_GROUPS.map(([label, test]) =>
    `<optgroup label="${esc(label)}">${Object.keys(S.videoFormats).filter(test).map(o).join("")}</optgroup>`).join("");
}
const shortPath = (p) => (p || "").replace(S.home, "~");
const baseName = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p;
function favFolders() {
  return S.settings.folders.split("\n").map((x) => x.trim()).filter(Boolean);
}
async function chooseFolder(start) {
  const r = { path: await pickFolder(start || S.settings.current_folder || S.settings.output_dir) };
  if (!r.path) return null;
  const favs = [r.path, ...favFolders().filter((f) => f !== r.path)].slice(0, 15);
  if (r.path !== S.settings.output_dir) await saveSettings({ folders: favs.join("\n") });
  return r.path;
}

// Popularity: 0-4 stars from the view count (1K, 10K, 100K, 1M+); blank when the site doesn't say
function stars(it) {
  if (it.views == null) return "";
  const n = Math.max(0, Math.min(4, Math.round(Math.log10(Math.max(1, it.views)) - 2)));
  let s = "";
  for (let i = 0; i < 4; i++) s += icon("star", i < n);
  return `<span class="stars" title="${fmt.views(it.views)} views">${s}</span>`;
}

const thumbImg = (url) => url ? `<img class="th" src="${esc(url)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">` : "";
const thumbCol = (get, extra = {}) => ({
  key: "thumb", label: "", title: "Thumbnail", thumb: true, cls: "c-thumb", render: (i) => thumbImg(get(i)),
  get w() { return S.settings?.thumbs === "large" ? 108 : 64; }, ...extra,
});
const thumbSizeMenu = () => [["small", "Small Thumbnails"], ["large", "Large Thumbnails"]].map(([v, l]) => ({
  label: l, radio: S.settings.thumbs === v, run: () => saveSettings({ thumbs: v }).then(() => { results.render(); jobsView.render(); }),
}));

const resultCols = [
  thumbCol((i) => i.thumb_small || i.thumbnail, {
    visible: () => S.settings?.thumbs !== "off",
    toggle: (on) => saveSettings({ thumbs: on ? "small" : "off" }).then(() => results.render()) }),
  { key: "title", label: "Name", w: null, fixed: true },
  { key: "duration", label: "Length", w: 54, cls: "num", render: (i) => fmt.dur(i.duration) },
  { key: "uploader", label: "Uploader", w: 120 },
  { key: "album", label: "Album", w: 120, render: (i) => i.album ? esc(i.album) : (i._details === "pending" ? `<span class="muted">…</span>` : "") },
  { key: "date", label: "Date", w: 78, render: (i) => i.date ? fmt.date(i.date) : (i._details === "pending" ? `<span class="muted">…</span>` : ""), sortVal: (i) => i.date || "" },
  { key: "views", label: "Views", w: 56, cls: "num", render: (i) => fmt.views(i.views) },
  { key: "rating", label: "Popularity", w: 70, render: stars, sortVal: (i) => i.views ?? -1, title: "Popularity (stars from view count)" },
  { key: "ext", label: "Type", w: 50, render: (i) => esc(i.kind === "audio" ? "audio" : (i.quality || "video")), sortVal: (i) => parseInt(i.quality) || 0 },
  { key: "size", label: "Size", w: 66, cls: "num", render: (i) => fmt.size(i.size) },
  { key: "kind", label: "", w: 24, cls: "c-icon", render: (i) => icon(i.kind === "audio" ? "fileAudio" : "fileVideo"), title: "File Icon" },
  { key: "site", label: "Source", w: 84 },
];

const results = new ListView($("#results-wrap"), "results", resultCols, {
  id: (i) => i._id,
  onDbl: (i) => i && download([i], S.settings.default_kind),
  onEnter: (sel) => download(sel, S.settings.default_kind),
  onDelete: (sel) => junk(sel),
  onSel: () => updateSearchButtons(),
  rowClass: (i) => (i._queued ? "queued" : ""),
  tooltip: mediaTip,
  colMenuExtra: () => S.settings.thumbs === "off" ? [] : thumbSizeMenu(),
  before: () => {
    const s = S.active;
    if (!s) return "";
    if (s.status === "error") return `<tr><td colspan="${resultCols.length}" class="err-banner">${esc(s.error)}</td></tr>`;
    if (s.errors && s.errors.length) return `<tr><td colspan="${resultCols.length}" class="err-banner">Some links failed:\n${esc(s.errors.join("\n"))}</td></tr>`;
    if (s.status === "searching" && !s.items.length) return `<tr class="row-msg"><td colspan="${resultCols.length}">Searching... (the first lookup on a site can take a few seconds)</td></tr>`;
    if (s.login && s.login.length) return `<tr><td colspan="${resultCols.length}" class="info-banner">Signed in with your ${esc(s.loginBrowser)} login for ${esc(s.login.join(", "))} — the site wouldn't show this logged out.</td></tr>`;
    return "";
  },
  onContext: (e, sel) => Menu.show(e.clientX, e.clientY, [
    { label: "Download Video", run: () => download(sel, "video") },
    { label: "Download Audio Only", run: () => download(sel, "audio") },
    { label: "Download to Folder...", run: async () => { const f = await chooseFolder(); f && download(sel, "video", "", f); } },
    { label: "Download Audio to Folder...", run: async () => { const f = await chooseFolder(); f && download(sel, "audio", "", f); } },
    "-",
    ...VIDEO_Q.slice(1).map(([v, l]) => ({ label: "Download at " + l, run: () => download(sel, "video", v) })),
    "-",
    { label: "Open Page in Browser", run: () => sel.forEach((i) => openUrl(i.url)), disabled: sel.length > 5 },
    { label: "Copy Link" + (sel.length > 1 ? "s" : ""), run: () => copyText(sel.map((i) => i.url).join("\n")) },
    { label: "Search Uploader", run: () => runSearch(sel[0].uploader), disabled: !sel[0].uploader },
    "-",
    { label: "Junk", key: "⌫", run: () => junk(sel) },
  ]),
});
$("#results-wrap").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); e.stopImmediatePropagation(); download(results.selected(), "audio"); }
}, true);

function visibleItems(s) {
  const f = s.filters;
  return s.items.filter((i) => !s.junked.has(i._id) &&
    (f.media === "all" || i.kind === f.media) &&
    (!f.site || i.site === f.site) &&
    (!f.uploader || i.uploader === f.uploader));
}

function newSearch() {
  showPage("search");
  const q = $("#q");
  q.value = "";
  q.focus();
}

function runSearch(query, source) {
  query = (query ?? $("#q").value).trim();
  if (!query) { $("#q").focus(); return; }
  source = source || "youtube";
  $("#q").value = query;
  showPage("search");
  const lines = query.split(/\s+/);
  const isUrls = lines.every((l) => /^(https?:\/\/|www\.)|^[\w-]+(\.[\w-]+)+\/\S*$/i.test(l));
  if (isUrls && lines.length === 1 && S.settings.instant_download) {
    download([{ url: query, title: query, _id: "x" }], S.settings.default_kind);
    return;
  }
  const s = {
    id: "s" + (++searchSeq), query, source, status: "searching", items: [], errors: [], error: "",
    label: isUrls ? shortUrl(lines[0]) + (lines.length > 1 ? ` +${lines.length - 1}` : "") : query,
    filters: { media: "all", site: null, uploader: null }, sel: new Set(), junked: new Set(),
  };
  S.searches.push(s);
  S.active = s;
  renderSearch();
  api("/api/search", { id: s.id, query, source })
    .then((r) => {
      s.items = r.items.map((it, i) => ({ ...it, _id: s.id + "-" + i }));
      s.errors = r.errors || [];
      s.login = r.login || [];
      s.loginBrowser = r.login_browser;
      s.status = "done";
      if (isUrls && s.items.length === 1) s.label = s.items[0].title;
      if (isUrls && s.items.length > 1 && s.items[0].playlist) s.label = s.items[0].playlist;
      if (s.items.length === 1) s.sel.add(s.items[0]._id);  // single link: ready to hit Download
      fetchDetails(s);
    })
    .catch((e) => { s.status = "error"; s.error = e.message; })
    .finally(() => { if (S.searches.includes(s)) renderSearch(); });
}
// Keyword searches come back without upload dates (and often sizes); look them up a few at a time
// and fill the rows in as answers arrive.
async function fetchDetails(s) {
  if (!S.settings.fetch_details) return;
  const todo = s.items.filter((i) => (!i.date || !i.size || !i.album) && /^https?:/.test(i.url));
  if (!todo.length) return;
  todo.forEach((i) => (i._details = "pending"));
  const chunks = [];
  for (const kind of ["video", "audio"]) {
    const group = todo.filter((i) => i.kind === kind);
    for (let k = 0; k < group.length; k += 3) chunks.push(group.slice(k, k + 3));
  }
  let redraw = null;
  const paint = () => { if (!redraw) redraw = setTimeout(() => { redraw = null; if (S.active === s) results.render(); }, 250); };
  const worker = async () => {
    while (chunks.length && S.searches.includes(s)) {
      const chunk = chunks.shift();
      try {
        const r = await api("/api/search/details", { id: s.id, urls: chunk.map((i) => i.url), kind: chunk[0].kind });
        chunk.forEach((i) => {
          const d = r.details[i.url];
          if (!d) return;
          i.size = i.size || d.size;
          i.album = i.album || d.album;
          if (i.site === "Spotify") {   // Spotify's own date and tags win; YouTube Music fills in the album
            if (i.tags && !i.tags.album && d.album) i.tags.album = d.album;
            return;
          }
          i.date = i.date || d.date;
          i.quality = i.quality || d.quality;
          i.views = i.views ?? d.views;
        });
      } catch {}
      chunk.forEach((i) => (i._details = "done"));
      paint();
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
  paint();
}
function shortUrl(u) {
  try { const x = new URL(u.startsWith("http") ? u : "https://" + u); return x.hostname.replace(/^www\./, "") + x.pathname.slice(0, 18); }
  catch { return u.slice(0, 30); }
}
function stopSearch() {
  const s = S.active;
  if (s && s.status === "searching") api("/api/search/cancel", { id: s.id });
}
function closeSearch(id) {
  const i = S.searches.findIndex((s) => s.id === id);
  if (i < 0) return;
  const [s] = S.searches.splice(i, 1);
  if (s.status === "searching") api("/api/search/cancel", { id: s.id });
  if (S.active === s) S.active = S.searches[i] || S.searches[i - 1] || null;
  renderSearch();
}

function renderSearch() {
  const s = S.active;
  // tabs
  $("#result-tabs").innerHTML = S.searches.map((x) => {
    const n = x.status === "searching" ? `<span class="spinner"></span>` : "";
    const cnt = x.status === "done" ? ` (${visibleItems(x).length})` : x.status === "error" ? " (!)" : "";
    return `<div class="rtab ${x === s ? "active" : ""}" data-id="${x.id}" title="${esc(x.query)}">
      <span class="rt-x" data-close="${x.id}">${icon("xRed")}</span>${n}
      <span class="rt-label ${x === s ? "hl" : ""}">${esc(x.label)}</span><span class="muted">${cnt}</span></div>`;
  }).join("");
  // filters
  renderFilters();
  $("#search-empty").classList.toggle("hidden", !!s);
  if (!s) { results.setItems([], new Set()); $("#results-wrap table")?.remove(); updateSearchButtons(); return; }
  results.setItems(visibleItems(s), s.sel);
  updateSearchButtons();
}
$("#result-tabs").addEventListener("mousedown", (e) => {
  const x = e.target.closest("[data-close]");
  if (x) { closeSearch(x.dataset.close); return; }
  const t = e.target.closest(".rtab");
  if (t) { S.active = S.searches.find((s) => s.id === t.dataset.id); renderSearch(); }
});

function renderFilters() {
  const s = S.active;
  const box = $("#filters");
  if (!s || !s.items.length) { box.innerHTML = ""; return; }
  const base = s.items.filter((i) => !s.junked.has(i._id));
  const count = (key, pred = () => true) => {
    const m = new Map();
    base.filter(pred).forEach((i) => { const v = i[key] || ""; if (v) m.set(v, (m.get(v) || 0) + 1); });
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  };
  const f = s.filters;
  const nA = base.filter((i) => i.kind === "audio").length;
  const li = (attr, val, label, n, sel, ic) =>
    `<li class="${sel ? "sel" : ""}" data-f="${attr}" data-v="${esc(val)}">${ic ? icon(ic) : ""}<span class="lbl">${esc(label)}</span><span class="cnt">${n != null ? n : ""}</span></li>`;
  const media = `<div class="filter-box"><div class="fb-head">Media</div><ul class="fb-list">
      ${li("media", "all", "All", `(${base.length})`, f.media === "all", "all16")}
      ${li("media", "audio", "Audio", nA, f.media === "audio", "fileAudio")}
      ${li("media", "video", "Video", base.length - nA, f.media === "video", "fileVideo")}</ul></div>`;
  const sites = count("site", (i) => f.media === "all" || i.kind === f.media);
  const ups = count("uploader", (i) => (f.media === "all" || i.kind === f.media) && (!f.site || i.site === f.site));
  const list = (title, attr, entries, cur, ic) => entries.length < 1 ? "" :
    `<div class="filter-box"><div class="fb-head">${title}</div><ul class="fb-list">
      ${li(attr, "", "All", `(${entries.length})`, !cur)}
      ${entries.slice(0, 100).map(([v, n]) => li(attr, v, v, n, cur === v, ic)).join("")}</ul></div>`;
  box.innerHTML = media + list("Source", "site", sites, f.site, "globe16") + list("Uploader", "uploader", ups, f.uploader, "user16");
}
$("#filters").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-f]");
  if (!li || !S.active) return;
  const f = S.active.filters;
  f[li.dataset.f] = li.dataset.f === "media" ? li.dataset.v : (li.dataset.v || null);
  if (li.dataset.f === "site") f.uploader = null;
  renderSearch();
});
$("#back-to-search").addEventListener("click", () => {
  if (S.active) S.active.filters = { media: "all", site: null, uploader: null };
  renderSearch();
  $("#q").focus();
  $("#q").select();
});

function updateSearchButtons() {
  const sel = results.selected ? (results.view ? results.selected() : []) : [];
  const has = sel.length > 0;
  $("#act-download").disabled = !has;
  $("#act-audio").disabled = !has;
  $("#act-browse").disabled = !has || sel.length > 5;
  $("#act-junk").disabled = !has;
  $("#act-stop").disabled = !(S.active && S.active.status === "searching");
}

function junk(sel) {
  const s = S.active;
  if (!s || !sel.length) return;
  sel.forEach((i) => { s.junked.add(i._id); s.sel.delete(i._id); });
  renderSearch();
}

async function download(items, kind, quality, folder) {
  items = items.filter(Boolean);
  if (!items.length) return;
  const q = kind === "video" ? (quality || S.settings.video_quality) : "";
  const dest = folder || S.settings.current_folder || S.settings.output_dir;
  try {
    const r = await api("/api/download", {
      items: items.map(({ url, title, site, uploader, thumbnail, duration, playlist, tags }) => ({ url, title, site, uploader, thumbnail, duration, playlist, tags })),
      kind, quality: q === "best" ? "" : q, folder: dest,
      vformat: S.settings.video_format, aformat: S.settings.audio_format,
      fps: S.settings.edit_fps,
    });
    items.forEach((i) => (i._queued = true));
    if (S.page === "search") results.render();
    const wav = /^wav/.test(S.settings.audio_format);
    const fpsNote = S.settings.edit_fps !== "source" && (kind === "video" || wav) ? ` at ${S.settings.edit_fps} fps` : "";
    const as = (kind === "video" ? S.videoFormats[S.settings.video_format] : (AUDIO_F.find((a) => a[0] === S.settings.audio_format) || [, "audio"])[1]) + fpsNote;
    flash(`Queued ${r.ids.length} download${r.ids.length === 1 ? "" : "s"} as ${as} → ${shortPath(dest)}`, 6000);
    poll();
  } catch (e) {
    msgbox("Download", esc(e.message), { icon: "warn32" });
  }
}

$("#go").addEventListener("click", () => runSearch());
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runSearch(); }
});
$("#act-download").addEventListener("click", () => download(results.selected(), "video"));
$("#act-audio").addEventListener("click", () => download(results.selected(), "audio"));
$("#act-browse").addEventListener("click", () => results.selected().forEach((i) => openUrl(i.url)));
$("#act-stop").addEventListener("click", stopSearch);
$("#act-junk").addEventListener("click", () => junk(results.selected()));

function fillQuick() {
  $("#quick-video").innerHTML = VIDEO_Q.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#quick-audio").innerHTML = AUDIO_F.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#quick-vformat").innerHTML = vformatOptions(S.settings.video_format);
  $("#quick-fps").innerHTML = FPS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#quick-fps").value = S.settings.edit_fps;
  $("#quick-fps").title = "Video is conformed to this rate (frames repeated or dropped, not retimed). WAV audio is stamped with it for Avid — MP3/M4A can't carry a frame rate.";
  $("#quick-video").value = S.settings.video_quality;
  $("#quick-audio").value = S.settings.audio_format;
  const def = S.settings.output_dir, cur = S.settings.current_folder || def;
  const opts = [def, ...favFolders().filter((f) => f !== def)];
  $("#quick-folder").innerHTML = opts.map((f) =>
      `<option value="${esc(f)}" title="${esc(f)}" ${f === cur ? "selected" : ""}>${esc(baseName(f))}${f === def ? " (default)" : ""}</option>`).join("") +
    `<option disabled>──────────</option><option value="__choose">Choose folder...</option><option value="__manage">Edit favourites...</option>`;
  $("#quick-folder").title = "New downloads go to " + cur;
}
$("#quick-video").addEventListener("change", (e) => saveSettings({ video_quality: e.target.value }));
$("#quick-audio").addEventListener("change", (e) => saveSettings({ audio_format: e.target.value }));
$("#quick-vformat").addEventListener("change", (e) => saveSettings({ video_format: e.target.value }));
$("#quick-fps").addEventListener("change", (e) => saveSettings({ edit_fps: e.target.value }));
$("#quick-folder").addEventListener("change", async (e) => {
  const v = e.target.value;
  if (v === "__manage") { fillQuick(); return openOptions("saving"); }
  if (v === "__choose") {
    const f = await chooseFolder();
    if (f) await saveSettings({ current_folder: f });
    fillQuick();
    return;
  }
  saveSettings({ current_folder: v === S.settings.output_dir ? "" : v });
});

async function pasteAndSearch() {
  try {
    const t = NATIVE ? await native("clipboard") : await navigator.clipboard.readText();
    if (t && t.trim()) return runSearch(t.trim());
  } catch {}
  newSearch();
  document.execCommand("paste");
}

// ================================================================= MONITOR
const STATUS_LABEL = {
  queued: "Queued", starting: "Connecting", downloading: "Downloading", processing: "Processing", converting: "Converting",
  paused: "Paused", done: "Complete", error: "Failed", cancelled: "Cancelled", skipped: "Skipped",
};
const ACTIVE = new Set(["starting", "downloading", "processing", "converting"]);
function formatLabel(j) {
  if (j.kind === "audio") return (AUDIO_F.find((a) => a[0] === j.aformat) || [, j.aformat || "audio"])[1];
  return (S.videoFormats[j.vformat] || j.vformat || "").replace(/ \(\.(mov|mxf)\)$/, " .$1");
}

const jobCols = [
  { key: "kind", label: "", w: 24, cls: "c-icon", render: (j) => icon(j.kind === "audio" ? "fileAudio" : "fileVideo"), title: "File Icon" },
  thumbCol((j) => j.thumbnail, { hidden: true }),
  { key: "title", label: "Name", w: null, fixed: true },
  { key: "total", label: "Size", w: 104, cls: "num", render: (j) =>
      j.status === "done" ? fmt.size(j.total) : j.total ? `${fmt.size(j.downloaded)} / ${fmt.size(j.total)}` : fmt.size(j.downloaded || null) },
  { key: "status", label: "Status", w: 160, render: (j) =>
      `<span class="status-pill st-${j.status}"><span class="status-dot"></span>${esc(j.stage && ACTIVE.has(j.status) ? j.stage : STATUS_LABEL[j.status] || j.status)}</span>`,
    sortVal: (j) => j.status },
  { key: "progress", label: "Progress", w: 112, render: (j) => {
      const indet = j.status === "starting" || j.status === "processing";
      const cls = j.status === "error" ? "err" : j.status === "paused" ? "paused" : indet ? "indet" : "";
      const pct = j.status === "done" ? 100 : j.progress || 0;
      return `<div class="xp-progress ${cls}"><i style="width:${pct}%"></i><span class="pct">${indet ? "" : Math.floor(pct) + "%"}</span></div>`;
    } },
  { key: "speed", label: "Speed", w: 80, cls: "num", render: (j) => fmt.speed(j.speed) },
  { key: "eta", label: "Time", w: 56, cls: "num", render: (j) => (j.eta != null && ACTIVE.has(j.status) ? fmt.dur(j.eta) : "") },
  { key: "quality", label: "Quality", w: 66, render: (j) => esc(j.kind === "audio" ? "audio" : j.quality || "best"), sortVal: (j) => parseInt(j.quality) || 0 },
  { key: "vformat", label: "Format", w: 118, render: (j) => esc(formatLabel(j)) },
  { key: "fps", label: "FPS", w: 52, hidden: true, render: (j) => esc(j.fps && j.fps !== "source" && (j.kind === "video" || /^wav/.test(j.aformat)) ? j.fps : "") },
  { key: "folder", label: "Saved In", w: 110, render: (j) => `<span title="${esc(j.folder)}">${esc(shortPath(j.folder).split("/").slice(-2).join("/"))}</span>` },
  { key: "site", label: "Source", w: 84 },
];

const jobsView = new ListView($("#jobs-wrap"), "jobs", jobCols, {
  id: (j) => j.id,
  sel: S.jobSel,
  empty: "No downloads yet. Find something on the Search tab and hit Download.",
  colMenuExtra: () => thumbSizeMenu(),
  onSel: () => { updateJobButtons(); renderDetails(true); },
  onDbl: (j) => j && (j.status === "done" ? openFile(j.filepath) : jobAction([j], j.status === "paused" || j.status === "error" ? "resume" : "pause")),
  onEnter: (sel) => sel[0] && sel[0].status === "done" && openFile(sel[0].filepath),
  onDelete: (sel) => removeJobs(sel),
  tooltip: mediaTip,
  onContext: (e, sel) => {
    const one = sel[0];
    Menu.show(e.clientX, e.clientY, [
      { label: "Launch", run: () => openFile(one.filepath), disabled: !(sel.length === 1 && one.status === "done") },
      { label: "Explore", run: () => api("/api/open", { what: "reveal", path: one.filepath }).catch((x) => flash(x.message)), disabled: !(sel.length === 1 && one.status === "done" && one.filepath) },
      { label: "Open Destination Folder", run: () => api("/api/open", { what: "folder", path: one.folder }).catch((x) => flash(x.message)), disabled: sel.length !== 1 || !one.folder },
      "-",
      { label: "Resume", run: () => jobAction(sel, "resume"), disabled: !sel.some((j) => ["paused", "error", "cancelled"].includes(j.status)) },
      { label: "Pause", run: () => jobAction(sel, "pause"), disabled: !sel.some((j) => ACTIVE.has(j.status) || j.status === "queued") },
      { label: "Kill", run: () => jobAction(sel, "cancel"), disabled: !sel.some((j) => ACTIVE.has(j.status) || ["queued", "paused"].includes(j.status)) },
      { label: "Retry (force re-download)", run: () => jobAction(sel, "retry"), disabled: sel.some((j) => ACTIVE.has(j.status)) },
      "-",
      { label: "Move Up", run: () => jobAction(sel, "up"), disabled: sel.length !== 1 },
      { label: "Move Down", run: () => jobAction(sel, "down"), disabled: sel.length !== 1 },
      "-",
      { label: "Open Page in Browser", run: () => openUrl(one.url), disabled: sel.length !== 1 },
      { label: "Copy Link", run: () => copyText(sel.map((j) => j.url).join("\n")) },
      { label: "Remove From List", key: "⌫", run: () => removeJobs(sel) },
    ]);
  },
});

function jobAction(sel, action) {
  return api("/api/job/action", { ids: sel.map((j) => j.id), action }).then(poll);
}
async function removeJobs(sel) {
  if (!sel.length) return;
  const live = sel.filter((j) => ACTIVE.has(j.status) || j.status === "queued");
  if (live.length && !(await confirmBox("Remove downloads", `${live.length} of these ${live.length === 1 ? "is" : "are"} still running and will be stopped. Continue?`, "Remove"))) return;
  jobAction(sel, "remove");
}
function openFile(p) { return api("/api/open", { what: "file", path: p }).catch((e) => flash(e.message)); }

function renderJobs() {
  if (S.page !== "monitor") return;
  jobsView.setItems(S.jobs);
  updateJobButtons();
  renderDetails(false);
  const done = S.jobs.filter((j) => j.status === "done").length;
  const fail = S.jobs.filter((j) => j.status === "error").length;
  $("#dl-summary").textContent = S.jobs.length ? `— ${S.jobs.length} total, ${done} complete${fail ? `, ${fail} failed` : ""}` : "";
}

function updateJobButtons() {
  const sel = jobsView.view ? jobsView.selected() : [];
  const any = (f) => sel.some(f);
  $("#job-resume").disabled = !any((j) => ["paused", "error", "cancelled"].includes(j.status));
  $("#job-pause").disabled = !any((j) => ACTIVE.has(j.status) || j.status === "queued");
  $("#job-cancel").disabled = !any((j) => ACTIVE.has(j.status) || ["queued", "paused"].includes(j.status));
  $("#job-retry").disabled = !sel.length || any((j) => ACTIVE.has(j.status));
  $("#job-play").disabled = !(sel.length === 1 && sel[0].status === "done");
  $("#job-reveal").disabled = !(sel.length === 1 && sel[0].status === "done" && sel[0].filepath);
  $("#job-clear").disabled = !S.jobs.some((j) => ["done", "error", "cancelled", "skipped"].includes(j.status));
}
$("#job-resume").addEventListener("click", () => jobAction(jobsView.selected(), "resume"));
$("#job-pause").addEventListener("click", () => jobAction(jobsView.selected(), "pause"));
$("#job-cancel").addEventListener("click", () => jobAction(jobsView.selected(), "cancel"));
$("#job-retry").addEventListener("click", () => jobAction(jobsView.selected(), "retry"));
$("#job-play").addEventListener("click", () => openFile(jobsView.selected()[0].filepath));
$("#job-reveal").addEventListener("click", () => api("/api/open", { what: "reveal", path: jobsView.selected()[0].filepath }).catch((e) => flash(e.message)));
$("#job-clear").addEventListener("click", () => api("/api/jobs/bulk", { action: "clear" }).then(poll));

let detailId = null, detailLog = null, showLog = store.get("showLog", false);
async function renderDetails(selChanged) {
  const sel = jobsView.view ? jobsView.selected() : [];
  const box = $("#job-details");
  if (sel.length !== 1) {
    detailId = null;
    box.innerHTML = `<div class="muted pad">${sel.length ? sel.length + " downloads selected." : "Select a download to see details."}</div>`;
    return;
  }
  const j = sel[0];
  if (selChanged || detailId !== j.id) { detailId = j.id; detailLog = null; }
  if (showLog) {
    api("/api/job?id=" + j.id).then((d) => {
      if (detailId !== j.id || !d.log) return;
      detailLog = d.log;
      const pre = $("pre.d-log", box);
      if (pre) {
        const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
        pre.textContent = d.log.join("\n");
        if (atBottom) pre.scrollTop = pre.scrollHeight;
      }
    }).catch(() => {});
  }
  const keepScroll = $("pre.d-log", box)?.scrollTop;
  box.innerHTML = `
    <div class="d-thumb">${j.thumbnail ? `<img src="${esc(j.thumbnail)}" referrerpolicy="no-referrer">` : icon("logoBig")}</div>
    <div class="d-body">
      <div class="d-title">${esc(j.title)}</div>
      <div class="d-row"><b>Status</b><span>${esc(STATUS_LABEL[j.status])}${j.stage && ACTIVE.has(j.status) ? " — " + esc(j.stage) : ""}</span></div>
      ${j.uploader ? `<div class="d-row"><b>Uploader</b><span>${esc(j.uploader)}</span></div>` : ""}
      <div class="d-row"><b>Format</b><span>${esc((j.quality ? j.quality + " · " : "") + formatLabel(j) + (j.fps && j.fps !== "source" && (j.kind === "video" || /^wav/.test(j.aformat)) ? " · " + j.fps + " fps" : ""))}</span></div>
      ${j.status !== "done" && j.folder ? `<div class="d-row"><b>Saving to</b><span><a class="lnk" data-folder="${esc(j.folder)}">${esc(shortPath(j.folder))}</a></span></div>` : ""}
      <div class="d-row"><b>Login</b><span>${j.used_login ? `Used your ${esc(j.used_login)} login — the site required it` : "Not used (downloaded logged out)"}</span></div>
      <div class="d-row"><b>Link</b><span><a class="lnk" data-open="${esc(j.url)}">${esc(j.url)}</a></span></div>
      ${j.filepath && j.status === "done" ? `<div class="d-row"><b>Saved to</b><span><a class="lnk" data-reveal="${esc(j.filepath)}">${esc(j.filepath)}</a></span></div>` : ""}
      ${j.error ? `<div class="d-err">${esc(j.error)}</div>` : ""}
      <div><a class="lnk" id="toggle-log">${showLog ? "Hide" : "Show"} engine log</a></div>
      ${showLog ? `<pre class="d-log">${esc((detailLog || ["Loading..."]).join("\n"))}</pre>` : ""}
    </div>`;
  if (keepScroll != null && $("pre.d-log", box)) $("pre.d-log", box).scrollTop = keepScroll;
}
$("#job-details").addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  if (a.id === "toggle-log") { showLog = !showLog; store.set("showLog", showLog); renderDetails(true); }
  else if (a.dataset.open) openUrl(a.dataset.open);
  else if (a.dataset.reveal) api("/api/open", { what: "reveal", path: a.dataset.reveal }).catch((x) => flash(x.message));
  else if (a.dataset.folder) api("/api/open", { what: "folder", path: a.dataset.folder }).catch((x) => flash(x.message));
});
(() => {  // drag splitter between job list and details
  const sp = $("#job-splitter"), det = $("#job-details");
  det.style.flexBasis = store.get("detailsH", 150) + "px";
  sp.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const y0 = e.clientY, h0 = det.getBoundingClientRect().height;
    const mv = (ev) => { det.style.flexBasis = Math.max(60, Math.min(innerHeight - 260, h0 - (ev.clientY - y0))) + "px"; };
    const up = () => { document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up); store.set("detailsH", parseInt(det.style.flexBasis)); };
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", up);
  });
})();

// ================================================================= LIBRARY
const libCols = [
  { key: "kind", label: "", w: 24, cls: "c-icon", render: (f) => icon(f.kind === "audio" ? "fileAudio" : "fileVideo"), title: "File Icon" },
  { key: "name", label: "Name", w: null, fixed: true },
  { key: "ext", label: "Type", w: 54 },
  { key: "size", label: "Size", w: 76, cls: "num", render: (f) => fmt.size(f.size) },
  { key: "mtime", label: "Downloaded", w: 150, render: (f) => fmt.time(f.mtime) },
  { key: "folder", label: "Folder", w: 140, render: (f) => `<span title="${esc(f.dir)}">${esc(baseName(f.root) + (f.folder === "." ? "" : "/" + f.folder))}</span>`, sortVal: (f) => f.dir },
];
const libView = new ListView($("#lib-wrap"), "library", libCols, {
  id: (f) => f.path,
  sel: S.libSel,
  sort: { key: "mtime", desc: true },
  empty: "Nothing here yet — finished downloads show up in this list.",
  onSel: () => updateLibButtons(),
  onDbl: (f) => f && Player.play(f),
  onEnter: (sel) => sel[0] && Player.play(sel[0]),
  onDelete: (sel) => trashFiles(sel),
  onContext: (e, sel) => Menu.show(e.clientX, e.clientY, [
    { label: "Play", run: () => Player.play(sel[0]), disabled: sel.length !== 1 },
    { label: "Launch in Default App", run: () => sel.forEach((f) => openFile(f.path)) },
    { label: "Explore", run: () => api("/api/open", { what: "reveal", path: sel[0].path }) },
    { label: "Copy Path", run: () => copyText(sel.map((f) => f.path).join("\n")) },
    "-",
    { label: "Move to Trash", key: "⌫", run: () => trashFiles(sel) },
  ]),
});
function libVisible() {
  const q = S.lib.filter.toLowerCase();
  const fo = S.lib.folder;
  const dir = fo.startsWith("dir:") ? fo.slice(4) : null;
  return S.lib.files.filter((f) =>
    (fo === "*" || (dir ? f.dir === dir || f.dir.startsWith(dir + "/") :
      fo === "audio" ? f.kind === "audio" : fo === "video" ? f.kind === "video" : fo === "week" ? f.mtime > Date.now() / 1000 - 7 * 86400 : false)) &&
    (!q || f.name.toLowerCase().includes(q)));
}
async function loadLibrary() {
  try {
    const d = await api("/api/library");
    S.lib.files = d.files;
    S.lib.roots = d.roots;
    S.libDirty = false;
    if (d.denied.length) flash(`macOS is blocking NooNoo from reading ${d.denied.map(shortPath).join(", ")} — allow it in System Settings › Privacy & Security › Files and Folders.`, 12000);
  } catch (e) { flash(e.message); }
  renderLibrary();
}
function renderLibrary() {
  const files = S.lib.files, roots = S.lib.roots || [];
  const nA = files.filter((f) => f.kind === "audio").length;
  const nW = files.filter((f) => f.mtime > Date.now() / 1000 - 7 * 86400).length;
  const under = (d) => files.filter((f) => f.dir === d || f.dir.startsWith(d + "/")).length;
  const item = (v, label, n, ic, cls = "", title = "") => `<li data-v="${esc(v)}" class="${cls} ${S.lib.folder === v ? "sel" : ""}" title="${esc(title)}">${icon(ic)}<span class="lbl">${esc(label)}</span><span class="cnt">${n}</span></li>`;
  // every download folder, with its first level of subfolders (Audio, Video, per-site...) underneath
  const tree = roots.map((r) => {
    const subs = [...new Set(files.filter((f) => f.root === r && f.folder !== ".").map((f) => f.folder.split("/")[0]))].sort();
    return item("dir:" + r, baseName(r), under(r), "folder16", "", r) +
      subs.map((sub) => item("dir:" + r + "/" + sub, sub, under(r + "/" + sub), "folder16", "sub", r + "/" + sub)).join("");
  }).join("");
  $("#lib-tree").innerHTML = item("*", "All Downloads", files.length, "library16") + item("audio", "Audio", nA, "fileAudio") +
    item("video", "Video", files.length - nA, "fileVideo") + item("week", "This Week", nW, "clock16") + tree;
  $("#lib-root").textContent = roots.length > 1 ? `${roots.length} folders` : shortPath(roots[0] || "");
  const vis = libVisible();
  $("#lib-title").textContent = $(`#lib-tree li.sel .lbl`)?.textContent || "All Downloads";
  $("#lib-summary").textContent = `— ${vis.length} file${vis.length === 1 ? "" : "s"}, ${fmt.size(vis.reduce((a, f) => a + f.size, 0)) || "0 B"}`;
  libView.setItems(vis);
  updateLibButtons();
}
$("#lib-tree").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-v]");
  if (li) { S.lib.folder = li.dataset.v; renderLibrary(); }
});
$("#lib-filter").addEventListener("input", (e) => { S.lib.filter = e.target.value; renderLibrary(); });
function updateLibButtons() {
  const sel = libView.view ? libView.selected() : [];
  $("#lib-play").disabled = sel.length !== 1;
  $("#lib-launch").disabled = !sel.length;
  $("#lib-reveal").disabled = sel.length !== 1;
  $("#lib-delete").disabled = !sel.length;
}
async function trashFiles(sel) {
  if (!sel.length) return;
  const ok = await confirmBox("Move to Trash", sel.length === 1 ? `Move “${esc(sel[0].name)}” to the Trash?` : `Move ${sel.length} files to the Trash?`, "Move to Trash");
  if (!ok) return;
  for (const f of sel) {
    try { await api("/api/open", { what: "trash", path: f.path }); } catch (e) { flash(e.message); }
  }
  loadLibrary();
}
$("#lib-play").addEventListener("click", () => Player.play(libView.selected()[0]));
$("#lib-launch").addEventListener("click", () => libView.selected().forEach((f) => openFile(f.path)));
$("#lib-reveal").addEventListener("click", () => api("/api/open", { what: "reveal", path: libView.selected()[0].path }));
$("#lib-delete").addEventListener("click", () => trashFiles(libView.selected()));
$("#lib-refresh").addEventListener("click", loadLibrary);
$("#lib-open-folder").addEventListener("click", () => {
  const fo = S.lib.folder;
  api("/api/open", { what: "folder", path: fo.startsWith("dir:") ? fo.slice(4) : "" }).catch((e) => flash(e.message));
});

// ================================================================= MEDIA PLAYER
const Player = {
  audio: $("#audio"), cur: null, win: null,
  src(f) { return `/media?t=${encodeURIComponent(window.TOKEN)}&path=${encodeURIComponent(f.path)}`; },
  list() { return S.lib.files.length ? libVisible() : []; },
  play(f) {
    if (!f) {
      if (this.cur && this.cur.kind === "audio" && this.audio.src) return this.audio.play();
      const sel = libView.view ? libView.selected()[0] : null;
      f = sel || this.list()[0];
      if (!f) return flash("Nothing to play — download something first.");
    }
    this.stop(true);
    this.cur = f;
    if (f.kind === "audio") {
      this.audio.src = this.src(f);
      this.audio.play().catch((e) => flash("Can't play this file here: " + e.message));
    } else {
      const d = dialog({ title: f.name, cls: "player-win", modal: false, onClose: () => { this.win = null; if (this.cur === f) this.stop(); },
        body: `<video src="${this.src(f)}" controls autoplay></video>` });
      d.el.style.position = "fixed"; d.el.style.inset = "auto 30px 50px auto"; d.el.style.zIndex = 1400;
      this.win = d;
      const v = $("video", d.el);
      v.addEventListener("ended", () => this.next());
      v.addEventListener("timeupdate", () => this.progress(v));
    }
    this.track(f.name, true);
  },
  stop(silent) {
    this.audio.pause();
    this.audio.removeAttribute("src");
    if (this.win) { const w = this.win; this.win = null; w.close(); }
    if (!silent) { this.cur = null; this.track("NooNoo Media Player", false); }
  },
  pause() {
    if (this.win) { const v = $("video", this.win.el); v.paused ? v.play() : v.pause(); return; }
    this.audio.paused ? this.audio.play() : this.audio.pause();
  },
  step(d) {
    const l = this.list();
    if (!l.length) return;
    const i = this.cur ? l.findIndex((f) => f.path === this.cur.path) : -1;
    this.play(l[(i + d + l.length) % l.length]);
  },
  next() { this.step(1); },
  progress(m) {
    const bar = $("#pl-track .pl-pos");
    if (bar && m.duration) bar.style.width = (100 * m.currentTime / m.duration) + "%";
  },
  track(name, playing) {
    const t = $("#pl-track");
    t.innerHTML = `<span>${esc(name)}</span><i class="pl-pos"></i>`;
    t.classList.toggle("playing", playing);
    t.title = name;
  },
};
Player.audio.addEventListener("ended", () => Player.next());
Player.audio.addEventListener("timeupdate", () => Player.progress(Player.audio));
$$(".pl-btn").forEach((b) => b.addEventListener("click", () => {
  const a = b.dataset.pl;
  if (a === "play") Player.play();
  else if (a === "pause") Player.pause();
  else if (a === "stop") Player.stop();
  else if (a === "next") Player.step(1);
  else if (a === "prev") Player.step(-1);
}));

// ================================================================= STATUS / POLLING
let pollTimer = null, polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const st = await api("/api/state");
    if (st.version !== S.jobVersion) {
      const before = new Map(S.jobs.map((j) => [j.id, j.status]));
      S.jobVersion = st.version;
      S.jobs = st.jobs;
      if (st.jobs.some((j) => j.status === "done" && before.get(j.id) && before.get(j.id) !== "done")) {
        S.libDirty = true;
        if (S.page === "library") loadLibrary();
      }
      renderJobs();
    }
    S.stats = st;
    updateChanged(st.update);
    renderStatus();
  } catch (e) {
    S.stats = null;
    renderStatus("Lost connection to NooNoo engine — is it still running?");
  } finally {
    polling = false;
  }
}
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = S.stats && (S.stats.active || S.stats.queued);
  pollTimer = setTimeout(() => poll().finally(schedulePoll), busy ? 500 : 1500);
}

// ================================================================= UPDATES
// The engine checks GitHub on launch and every few hours; when it finds a newer NooNoo an XP-style
// balloon pops up over the status bar. One click on Update Now downloads, verifies, restarts.
let updateBalloonShown = false, updateDialog = null;
function updateChanged(u) {
  if (!u) return;
  if (u.status === "available" && u.latest !== u.skipped && !updateBalloonShown && !updateDialog) {
    updateBalloonShown = true;
    balloon(`NooNoo ${u.latest} is available`, "Click here to see what's new and update.", () => showUpdate(u));
  }
  if (updateDialog) updateDialog.refresh(u);
}
function balloon(title, text, onClick) {
  $("#balloon")?.remove();
  const b = document.createElement("div");
  b.id = "balloon";
  b.className = "xp-balloon";
  b.innerHTML = `<div class="bl-head">${icon("info32")}<b>${esc(title)}</b><button class="bl-x" title="Close">×</button></div><div class="bl-text">${esc(text)}</div>`;
  b.querySelector(".bl-x").onclick = (e) => { e.stopPropagation(); b.remove(); };
  b.onclick = () => { b.remove(); onClick(); };
  document.body.appendChild(b);
  setTimeout(() => b.remove(), 30000);
}
async function checkUpdates(manual) {
  const box = manual ? dialog({ title: "NooNoo Update", cls: "msgbox", body: `<div data-icon="info32"></div><div class="msg">Checking for updates...</div>` }) : null;
  let u;
  try { u = await api("/api/update/check", {}); } catch (e) { u = { status: "error", error: e.message }; }
  if (box) box.close();
  if (u.status === "available") return showUpdate(u);
  if (!manual) return;
  if (u.status === "uptodate") msgbox("NooNoo Update", `You're up to date — NooNoo ${esc(u.current)} is the newest version.`, { icon: "info32" });
  else msgbox("NooNoo Update", esc(u.error || "Couldn't check for updates."), { icon: "warn32" });
}
function showUpdate(u) {
  if (updateDialog) return;
  const active = (S.stats?.active || 0) + (S.stats?.queued || 0);
  const body = document.createElement("div");
  body.className = "update-box";
  body.innerHTML = `<div class="up-head">${icon("logoBig")}<div><b class="up-title">NooNoo ${esc(u.latest)} is available</b>
      <div class="muted">You have ${esc(u.current || "an older version")}.${u.size ? " Download size " + fmt.size(u.size) + "." : ""}</div></div></div>
    <div class="up-label">What's new:</div><pre class="up-notes">${esc(u.notes || "No release notes.")}</pre>
    <div class="up-status muted">${u.can_install ? (active ? `${active} download${active === 1 ? "" : "s"} will pause while NooNoo restarts, then carry on.` : "NooNoo will restart to finish updating.")
                                     : "Updating needs the NooNoo app; in a browser, download it from the release page."}</div>
    <div class="xp-progress hidden"><i style="width:0%"></i><span class="pct"></span></div>`;
  const d = dialog({
    title: "NooNoo Update", body, width: 520, onClose: () => { updateDialog = null; },
    buttons: u.can_install ? [
      { label: "Skip This Version", left: true, run: () => api("/api/update/skip", { version: u.latest }) },
      { label: "Update Now", default: true, keepOpen: true, run: () => startUpdate() },
      { label: "Remind Me Later" },
    ] : [{ label: "Open Download Page", default: true, run: () => openUrl(u.page) }, { label: "Later" }],
  });
  const status = $(".up-status", d.el), bar = $(".xp-progress", d.el);
  let installing = false;
  async function startUpdate() {
    $$(".xw-foot .xp-btn", d.el).forEach((b) => (b.disabled = true));
    bar.classList.remove("hidden");
    status.textContent = "Downloading...";
    await api("/api/update/download", {}).catch(() => {});
  }
  updateDialog = {
    refresh(s) {
      if (s.status === "downloading") {
        $("i", bar).style.width = s.progress + "%";
        $(".pct", bar).textContent = Math.floor(s.progress) + "%";
      } else if (s.status === "ready" && !installing) {
        installing = true;
        $("i", bar).style.width = "100%";
        status.textContent = "Verified. Restarting NooNoo...";
        api("/api/update/install", {}).then((r) => {
          if (!r.ok) { status.textContent = r.error; $$(".xw-foot .xp-btn", d.el).forEach((b) => (b.disabled = false)); }
        }).catch(() => {});   // the app quits mid-request when all is well
      } else if (s.status === "available" && s.error && !bar.classList.contains("hidden")) {
        status.textContent = s.error;
        bar.classList.add("hidden");
        $$(".xw-foot .xp-btn", d.el).forEach((b) => (b.disabled = false));
      }
    },
  };
}

function renderStatus(err) {
  const st = S.stats || { active: 0, queued: 0, speed: 0 };
  $("#sb-count").textContent = st.active;
  const badge = st.active + st.queued;
  $("#monitor-badge").textContent = badge ? badge : "";
  const sig = $(".sb-signal");
  sig.classList.toggle("on", !err && !!S.toolsOk);
  sig.classList.toggle("busy", !!st.active);
  let text;
  if (err) text = err;
  else if (S.flash && S.flash.until > Date.now()) text = S.flash.msg;
  else if (st.active) text = `Downloading ${st.active} file${st.active === 1 ? "" : "s"} at ${fmt.speed(st.speed) || "..."}${st.queued ? ` — ${st.queued} queued` : ""}`;
  else if (st.queued) text = `${st.queued} download${st.queued === 1 ? "" : "s"} queued`;
  else if (!S.toolsOk) text = "Setting up download engine...";
  else text = "Ready. Paste a link or search for anything.";
  $("#sb-text").textContent = text;
  document.title = st.active ? `NooNoo — ${st.active} downloading` : "NooNoo";
}
$("#sb-text").addEventListener("click", () => showPage("monitor"));

// ================================================================= SETTINGS
async function loadSettings() {
  const d = await api("/api/settings");
  S.settings = d.settings;
  S.defaults = d.defaults;
  S.videoFormats = d.video_formats;
  S.editFormats = d.edit_formats;
  S.cookieSites = d.cookie_sites;
  S.home = d.home;
  applySettings();
}
async function saveSettings(patch) {
  const before = S.settings;
  const d = await api("/api/settings", { settings: { ...S.settings, ...patch } });
  S.settings = d.settings;
  if (["folders", "output_dir", "current_folder"].some((k) => before[k] !== d.settings[k])) {
    S.libDirty = true;
    if (S.page === "library") loadLibrary();
  }
  applySettings();
}
function applySettings() {
  document.body.dataset.theme = S.settings.theme;
  fillQuick();
}

const OPTION_PAGES = [
  { id: "saving", label: "Saving", icon: "folder16", fields: [
    { key: "output_dir", label: "Default folder", type: "folder" },
    { key: "folders", label: "Favourite folders", type: "folders",
      help: "These appear in <b>Save to</b> on the Search tab, so each download can go somewhere different. Right-click a result › <b>Download to Folder...</b> for a one-off location. The Library shows all of them." },
    { key: "organize", label: "Sort into subfolders by", type: "select", options: [["none", "Nothing"], ["site", "Site (YouTube, TikTok...)"], ["uploader", "Uploader / channel"], ["playlist", "Playlist"], ["date", "Download date (2026-10-26)"]] },
    { key: "filename_template", label: "File name pattern", type: "text", list: [
        ["%(title).180B [%(id)s].%(ext)s", "Title [id]"], ["%(title).200B.%(ext)s", "Title"],
        ["%(uploader)s - %(title).180B.%(ext)s", "Uploader - Title"],
        ["%(upload_date>%Y-%m-%d)s %(title).180B.%(ext)s", "Date Title"],
        ["%(playlist_index|)s%(playlist_index& - |)s%(title).180B.%(ext)s", "## - Title"]],
      help: "Uses yt-dlp output templates." },
    { key: "filename_id", label: "Add the site's video ID to file names — “Title [dQw4w9WgXcQ].mp4”", type: "check" },
    { key: "restrict_filenames", label: "Plain ASCII file names (no spaces, emoji or accents — safest for Avid)", type: "check" },
    { key: "skip_existing", label: "Skip media already downloaded to the same folder in the same format", type: "check" },
  ] },
  { id: "video", label: "Video", icon: "fileVideo", fields: [
    { key: "video_quality", label: "Highest resolution", type: "select", options: VIDEO_Q,
      help: "NooNoo always takes the biggest picture available up to this size, then converts it to the format below if needed." },
    { key: "video_format", label: "Save video as", type: "vformat" },
    { type: "html", html: `<div class="help"><b>MP4 · H.264</b> plays everywhere; 4K VP9/AV1 sources are converted on your Mac's media engine.<br>
        <b>DNxHD LB / SQ / HQ</b> is Avid's HD codec: always 1920×1080 at a broadcast frame rate, using Avid's own bitrates
        (e.g. DNxHD 36 / 115 / 175 at 23.976). Smaller or larger pictures are scaled to fit, with black bars for vertical or square video.<br>
        <b>DNxHR LB / SQ / HQ</b> keeps the original size — use it for 4K or vertical projects.<br>
        Both are .mov, 4:2:2, constant frame rate, 24-bit 48 kHz PCM audio, ready to link or import into Media Composer.</div>` },
    { key: "edit_fps", label: "Frame rate", type: "select", options: FPS,
      help: "Conforms video to your project rate (frames are repeated or dropped, not retimed). WAV audio is saved as Broadcast WAV stamped with this rate, which Avid reads on import; MP3 and M4A have nowhere to store one. Also on the Search tab, next to Quality." },
    { key: "keep_original", label: "Also keep the original download after converting", type: "check" },
    { key: "embed_chapters", label: "Embed chapter markers (not for editing formats)", type: "check" },
    { key: "subtitles", label: "Download and embed subtitles (not for editing formats)", type: "check" },
    { key: "sub_langs", label: "Subtitle languages", type: "text", help: "Comma separated, regex allowed: <code>en.*,es</code> · <code>all</code>" },
  ] },
  { id: "audio", label: "Audio", icon: "fileAudio", fields: [
    { key: "audio_format", label: "Format", type: "select", options: AUDIO_F, help: "“Original” keeps the site's own stream without re-encoding (fastest, best quality)." },
    { key: "audio_quality", label: "Quality", type: "select", options: [["0", "Best (VBR ~245 kbps)"], ["320K", "320 kbps"], ["256K", "256 kbps"], ["192K", "192 kbps"], ["128K", "128 kbps"], ["5", "Small (VBR ~130 kbps)"]] },
    { key: "embed_thumbnail", label: "Embed cover art / thumbnail", type: "check" },
    { key: "embed_metadata", label: "Write title, artist and date tags", type: "check" },
  ] },
  { id: "speed", label: "Speed", icon: "monitor16", fields: [
    { key: "max_concurrent", label: "Simultaneous downloads", type: "number", min: 1, max: 12 },
    { key: "fragments", label: "Connections per download", type: "number", min: 1, max: 32,
      help: "Parallel fragment downloads for streaming sites (YouTube, Twitch, X...). 4–16 is usually fastest." },
    { key: "rate_limit", label: "Speed limit", type: "text", placeholder: "unlimited — e.g. 5M or 800K" },
    { key: "retries", label: "Retries on network errors", type: "number", min: 0, max: 100 },
  ] },
  { id: "behaviour", label: "Behaviour", icon: "all16", fields: [
    { key: "default_kind", label: "Double-click / Enter downloads", type: "select", options: [["video", "Video"], ["audio", "Audio only"]] },
    { key: "instant_download", label: "Pasting a single link downloads it immediately (skip results list)", type: "check" },
    { key: "notify_done", label: "Show a notification when a download finishes", type: "check" },
    { key: "auto_update_check", label: "Check for new versions of NooNoo automatically", type: "check",
      help: "One anonymous request to GitHub on launch and every few hours. Help › Check for Updates works either way." },
    { key: "search_results", label: "Keyword search results", type: "number", min: 5, max: 200 },
    { key: "fetch_details", label: "Look up upload date, resolution and size for each search result (one extra request per result)", type: "check" },
    { key: "playlist_items_limit", label: "Max items to list from playlists / channels", type: "number", min: 10, max: 5000 },
    { key: "sponsorblock", label: "Cut sponsor segments out of YouTube videos (SponsorBlock)", type: "check" },
  ] },
  { id: "accounts", label: "Accounts", icon: "user16", fields: [
    { key: "cookies_browser", label: "Borrow logins from", type: "select", options: [["", "Nobody — always logged out"], ["chrome", "Chrome"], ["safari", "Safari"], ["firefox", "Firefox"], ["brave", "Brave"], ["edge", "Edge"], ["chromium", "Chromium"], ["opera", "Opera"], ["vivaldi", "Vivaldi"]] },
    { key: "cookies_profile", label: "Browser profile", type: "text", placeholder: "default — or e.g. Profile 2",
      help: "Tip: make a separate browser profile signed into a spare account just for downloading, and name it here." },
    { key: "cookies_sites", label: "Only for these sites", type: "sites" },
    { key: "cookies_custom", label: "Other sites", type: "text", placeholder: "e.g. nebula.tv, dropout.tv" },
    { key: "cookies_mode", label: "When", type: "select", options: [["fallback", "Only when a site refuses me logged out"], ["always", "Every time I use those sites"]] },
    { type: "html", html: `<div class="privacy"><b>What NooNoo does with your login</b><ul>
        <li>It never copies or saves your cookies. yt-dlp reads them from the browser at the moment of a download and forgets them when it finishes.</li>
        <li>They're only offered to the sites ticked above, and each site only receives its own cookies.</li>
        <li>In “only when refused” mode every download is tried logged out first, so sites only learn who you are when they insist.</li>
        <li>Every download and search that used your login says so (Monitor › details, and a green note on search results).</li>
        <li>Chrome, Brave, Edge and friends: macOS asks for your Keychain password in its own window. NooNoo never sees it. <a href="#" data-keychain>What that prompt means</a></li>
        <li>NooNoo has no accounts, analytics or telemetry. It only talks to the sites you download from, and to GitHub to check for updates (Options › Behaviour) and fetch the engine.</li>
      </ul></div>` },
  ] },
  { id: "network", label: "Network", icon: "globe16", fields: [
    { key: "proxy", label: "Proxy", type: "text", placeholder: "e.g. socks5://127.0.0.1:1080 or http://host:port" },
  ] },
  { id: "columns", label: "Columns", icon: "all16", fields: [
    { key: "thumbs", label: "Thumbnails in search results", type: "select", options: [["off", "Off"], ["small", "Small"], ["large", "Large"]] },
    { type: "columns" },
  ] },
  { id: "appearance", label: "Appearance", icon: "app16", fields: [
    { key: "theme", label: "Colour scheme", type: "theme" },
    { key: "xp_frame", label: "Use the XP title bar (restart NooNoo to apply)", type: "check" },
  ] },
  { id: "engine", label: "Download Engine", icon: "download32", fields: [{ type: "engine" }] },
  { id: "advanced", label: "Advanced", icon: "fileOther", fields: [
    { key: "extra_args", label: "Extra yt-dlp arguments", type: "text", placeholder: "--no-mtime --write-description",
      help: "Appended to every download. See <a class='lnk' data-url='https://github.com/yt-dlp/yt-dlp#usage-and-options'>yt-dlp options</a>." },
  ] },
];

function openOptions(startPage) {
  const draft = { ...S.settings };
  let page = startPage || store.get("optPage", "saving");
  let engineTimer = null;
  const wrap = document.createElement("div");
  wrap.className = "opts";
  wrap.innerHTML = `<div class="opts-nav">${OPTION_PAGES.map((p) => `<div data-p="${p.id}">${icon(p.icon === "download32" ? "fileOther" : p.icon)}${esc(p.label)}</div>`).join("")}</div><div class="opts-pane"></div>`;
  const pane = $(".opts-pane", wrap);

  function field(f) {
    const v = draft[f.key];
    const id = "opt-" + f.key;
    const help = f.help ? `<div class="help">${f.help}</div>` : "";
    if (f.type === "check") return `<label class="cb"><input type="checkbox" id="${id}" data-k="${f.key}" ${v ? "checked" : ""}>${esc(f.label)}</label>${help}`;
    if (f.type === "select") return `<div class="row"><label for="${id}">${esc(f.label)}</label><select id="${id}" data-k="${f.key}">${f.options.map(([o, l]) => `<option value="${esc(o)}" ${String(v) === o ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>${help}`;
    if (f.type === "number") return `<div class="row"><label for="${id}">${esc(f.label)}</label><input type="number" id="${id}" data-k="${f.key}" value="${esc(v)}" min="${f.min}" max="${f.max}" style="width:70px"></div>${help}`;
    if (f.type === "folder") return `<div class="row"><label>${esc(f.label)}</label><input id="${id}" data-k="${f.key}" value="${esc(v)}"><button class="xp-btn" data-browse="${f.key}">Browse...</button></div>${help}`;
    if (f.type === "text") return `<div class="row"><label for="${id}">${esc(f.label)}</label><input id="${id}" data-k="${f.key}" value="${esc(v)}" placeholder="${esc(f.placeholder || "")}" spellcheck="false">
        ${f.list ? `<select data-preset="${f.key}" style="width:22px;padding:0 0 0 18px" title="Presets"><option></option>${f.list.map(([o, l]) => `<option value="${esc(o)}">${esc(l)}</option>`).join("")}</select>` : ""}</div>${help}`;
    if (f.type === "theme") {
      const sw = { blue: ["Luna Blue", "linear-gradient(#3593ff,#0054e3 30%,#0055eb 70%,#003092)"], olive: ["Olive Green", "linear-gradient(#e5ecca,#97ac6c 20%,#8ba169 70%,#6f8848)"], silver: ["Silver", "linear-gradient(#fff,#d2d2de 30%,#c2c2d2 70%,#9c9cb4)"] };
      return `<div class="theme-swatches">${Object.entries(sw).map(([k, [l, g]]) =>
        `<label class="${v === k ? "sel" : ""}"><span class="sw" style="background:${g}"></span><span><input type="radio" name="theme" data-k="theme" value="${k}" ${v === k ? "checked" : ""}>${l}</span></label>`).join("")}</div>`;
    }
    if (f.type === "vformat") return `<div class="row"><label for="${id}">${esc(f.label)}</label><select id="${id}" data-k="${f.key}">${vformatOptions(v)}</select></div>`;
    if (f.type === "html") return f.html;
    if (f.type === "columns") {
      const prefs = (() => { try { return JSON.parse(draft.columns || "{}") || {}; } catch { return {}; } })();
      return `<div class="help">Tick the columns each list shows. You can also right-click any column header, drag headers to reorder them, and drag their edges to resize.</div>` +
        [[results, "Search results"], [jobsView, "Downloads (Monitor)"], [libView, "Library"]].map(([lv, title]) => {
          const hidden = new Set((prefs[lv.name] || {}).hidden || lv.allCols.filter((c) => c.hidden).map((c) => c.key));
          return `<fieldset><legend>${title}</legend><div class="site-grid">${lv.allCols.filter((c) => !c.fixed && !c.toggle).map((c) =>
            `<label class="cb"><input type="checkbox" data-col="${lv.name}:${c.key}" ${hidden.has(c.key) ? "" : "checked"}>${esc(c.title || c.label)}</label>`).join("")}</div></fieldset>`;
        }).join("") + `<button class="xp-btn" data-cols-reset>Reset All Columns</button>`;
    }
    if (f.type === "folders") return `<div><label>${esc(f.label)}</label><div class="folder-list" id="fav-list">${
        String(v).split("\n").filter((x) => x.trim()).map((x) => `<div data-fav="${esc(x)}" title="${esc(x)}">${icon("folder16")}${esc(shortPath(x))}</div>`).join("") ||
        `<div class="muted">No favourites yet</div>`}</div>
        <div class="row"><button class="xp-btn" data-fav-add>Add...</button><button class="xp-btn" data-fav-remove>Remove</button></div></div>${help}`;
    if (f.type === "sites") {
      const on = new Set(String(v).split(",").filter(Boolean));
      return `<div><label>${esc(f.label)}</label><div class="site-grid">${Object.entries(S.cookieSites).map(([k, l]) =>
        `<label class="cb"><input type="checkbox" data-site="${k}" ${on.has(k) ? "checked" : ""}>${esc(l)}</label>`).join("")}</div></div>`;
    }
    if (f.type === "engine") return `<div class="help">NooNoo uses <b>yt-dlp</b> to talk to 1,800+ sites, <b>ffmpeg</b> to merge and convert, and <b>deno</b> to solve YouTube's player challenges. Sites change often — if downloads from a site start failing, update first.</div>
        <table class="tools-table" id="tools-table"><tr><td>Loading...</td></tr></table>
        <div class="row"><button class="xp-btn" id="tools-update">Update All Now</button><span class="muted" id="tools-busy"></span></div>
        <div class="tools-log" id="tools-log"></div>`;
    return "";
  }

  function show(p) {
    page = p;
    store.set("optPage", p);
    clearInterval(engineTimer);
    $$(".opts-nav div", wrap).forEach((d) => d.classList.toggle("sel", d.dataset.p === p));
    const def = OPTION_PAGES.find((x) => x.id === p);
    pane.innerHTML = `<h3>${esc(def.label)}</h3>` + def.fields.map(field).join("");
    if (p === "engine") { refreshEngine(); engineTimer = setInterval(refreshEngine, 1000); }
  }
  async function refreshEngine() {
    try {
      const t = await api("/api/tools");
      const tbl = $("#tools-table", pane);
      if (!tbl) return;
      tbl.innerHTML = Object.entries(t.status).map(([k, v]) => `<tr><td><b>${k}</b></td><td>${v ? esc(v) : `<span style="color:#b00">not installed</span>`}</td></tr>`).join("") +
        `<tr><td class="muted">Location</td><td class="muted">${esc(t.bin)}</td></tr>`;
      $("#tools-busy", pane).textContent = t.busy ? "Working..." : "";
      $("#tools-update", pane).disabled = t.busy;
      const log = $("#tools-log", pane);
      log.textContent = t.log.join("\n");
      log.scrollTop = log.scrollHeight;
    } catch {}
  }

  wrap.addEventListener("click", async (e) => {
    const nav = e.target.closest(".opts-nav [data-p]");
    if (nav) return show(nav.dataset.p);
    if (e.target.closest("[data-keychain]")) { e.preventDefault(); return keychainHelp(KEYCHAIN_NAMES[draft.cookies_browser] ? draft.cookies_browser : "chrome"); }
    const br = e.target.closest("[data-browse]");
    if (br) {
      const r = { path: await pickFolder(draft[br.dataset.browse]) };
      if (r.path) { draft[br.dataset.browse] = r.path; $("#opt-" + br.dataset.browse, pane).value = r.path; }
      return;
    }
    const fav = e.target.closest("[data-fav]");
    if (fav) { $$("#fav-list [data-fav]", pane).forEach((d) => d.classList.toggle("sel", d === fav)); return; }
    if (e.target.closest("[data-cols-reset]")) {
      draft.columns = "";
      [results, jobsView, libView].forEach((lv) => { lv.widths = {}; store.set(`widths.${lv.name}`, {}); });
      return show(page);
    }
    if (e.target.closest("[data-fav-add]")) {
      const r = { path: await pickFolder(draft.output_dir) };
      const list = draft.folders.split("\n").filter((x) => x.trim());
      if (r.path && !list.includes(r.path)) { draft.folders = [...list, r.path].join("\n"); show(page); }
      return;
    }
    if (e.target.closest("[data-fav-remove]")) {
      const sel = $("#fav-list .sel", pane);
      if (sel) {
        draft.folders = draft.folders.split("\n").filter((x) => x.trim() && x !== sel.dataset.fav).join("\n");
        if (draft.current_folder === sel.dataset.fav) draft.current_folder = "";
        show(page);
      }
      return;
    }
    if (e.target.id === "tools-update") { await api("/api/tools/install", { update: true }); refreshEngine(); }
    const u = e.target.closest("[data-url]");
    if (u) openUrl(u.dataset.url);
  });
  wrap.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.preset) {
      if (t.value) { draft[t.dataset.preset] = t.value; $("#opt-" + t.dataset.preset, pane).value = t.value; }
      t.value = "";
      return;
    }
    if (t.dataset.col) {
      const [table, key] = t.dataset.col.split(":");
      const prefs = (() => { try { return JSON.parse(draft.columns || "{}") || {}; } catch { return {}; } })();
      const lv = [results, jobsView, libView].find((x) => x.name === table);
      const hidden = new Set((prefs[table] || {}).hidden || lv.allCols.filter((c) => c.hidden).map((c) => c.key));
      t.checked ? hidden.delete(key) : hidden.add(key);
      prefs[table] = { ...(prefs[table] || {}), hidden: [...hidden] };
      draft.columns = JSON.stringify(prefs);
      return;
    }
    if (t.dataset.site) {
      draft.cookies_sites = $$("[data-site]", pane).filter((c) => c.checked).map((c) => c.dataset.site).join(",");
      return;
    }
    const k = t.dataset.k;
    if (!k) return;
    draft[k] = t.type === "checkbox" ? t.checked : t.type === "number" ? Number(t.value) : t.value;
    if (k === "cookies_browser") keychainHelp(t.value);
    if (k === "theme") {
      document.body.dataset.theme = t.value;
      $$(".theme-swatches label", pane).forEach((l) => l.classList.toggle("sel", $("input", l).checked));
    }
  });
  wrap.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.k && t.type !== "checkbox" && t.type !== "radio") draft[t.dataset.k] = t.type === "number" ? Number(t.value) : t.value;
  });

  const apply = () => saveSettings(draft).catch((e) => msgbox("Options", esc(e.message), { icon: "warn32" }));
  dialog({
    title: "Options", body: wrap,
    onClose: () => { clearInterval(engineTimer); applySettings(); },
    buttons: [
      { label: "Restore Defaults", left: true, keepOpen: true, run: async () => {
          if (await confirmBox("Options", "Reset every option to its default?")) {
            Object.assign(draft, S.defaults); show(page); document.body.dataset.theme = draft.theme;
          }
        } },
      { label: "OK", default: true, run: () => { apply(); } },
      { label: "Cancel" },
      { label: "Apply", keepOpen: true, run: apply },
    ],
  });
  show(page);
}

// ================================================================= first-run engine setup
async function checkTools() {
  let t;
  try { t = await api("/api/tools"); } catch { return; }
  S.toolsOk = !t.missing.length;
  if (S.toolsOk && !t.busy) return renderStatus();
  const d = dialog({
    title: "Setting up NooNoo", width: 460,
    body: `<div style="display:flex;gap:12px"><div data-icon="logoBig"></div><div style="flex:1">
      <b style="font-size:12px">Installing the download engine</b>
      <p class="muted" style="margin:6px 0 8px">One-time download of yt-dlp, ffmpeg and deno (~120 MB) into NooNoo's own folder. Nothing is installed system-wide.</p>
      <div class="xp-progress indet" id="setup-bar"><i></i></div>
      <div class="tools-log" id="setup-log" style="margin-top:8px;height:110px"></div></div></div>`,
    buttons: [{ label: "Retry", keepOpen: true, run: () => api("/api/tools/install", {}) }, { label: "Hide" }],
  });
  const timer = setInterval(async () => {
    try {
      const x = await api("/api/tools");
      if (!$("#setup-log", d.el)) return clearInterval(timer);
      $("#setup-log", d.el).textContent = x.log.join("\n");
      const bar = $("#setup-bar", d.el);
      bar.classList.toggle("indet", x.busy);
      bar.classList.toggle("err", !x.busy && !!x.missing.length);
      if (!x.busy && !x.missing.length) {
        clearInterval(timer);
        S.toolsOk = true;
        renderStatus();
        d.close();
        flash("Download engine ready.");
      } else if (!x.busy && x.missing.length) {
        $("#setup-log", d.el).textContent += `\n\nStill missing: ${x.missing.join(", ")}. Check your connection and press Retry.`;
      }
    } catch {}
  }, 1000);
}

// ================================================================= keyboard, paste, drag & drop
document.addEventListener("keydown", (e) => {
  const inField = e.target.matches("input, textarea, select");
  if (e.key === "Escape") { Menu.close(); Tip.hide(); const top = $$("#dialogs > *").pop(); if (top && top._close) top._close(null); return; }
  if (!e.metaKey && !e.ctrlKey) {
    if (e.key === " " && S.page === "library" && !inField) { e.preventDefault(); Player.cur ? Player.pause() : Player.play(); }
    return;
  }
  const k = e.key.toLowerCase();
  if (k === "1" || k === "2" || k === "3") { e.preventDefault(); showPage(["search", "monitor", "library"][+k - 1]); }
  else if (k === ",") { e.preventDefault(); openOptions(); }
  else if (k === "f") { e.preventDefault(); showPage("search"); $("#q").focus(); $("#q").select(); }
  else if (k === "n") { e.preventDefault(); newSearch(); }
  else if (k === "w") { e.preventDefault(); if (S.page === "search" && S.active) closeSearch(S.active.id); }
  else if (k === "o") { e.preventDefault(); api("/api/open", { what: "folder" }); }
});
document.addEventListener("paste", (e) => {
  if (e.target.matches("input, textarea")) return;
  const t = (e.clipboardData || window.clipboardData).getData("text");
  if (t && t.trim()) { e.preventDefault(); runSearch(t.trim()); }
});
let dragDepth = 0;
document.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.some((t) => t.startsWith("text"))) { dragDepth++; document.body.classList.add("dragging"); } });
document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  const t = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
  if (t) runSearch(t.split(/\r?\n/).filter((l) => l && !l.startsWith("#")).join(" "));
});
document.addEventListener("contextmenu", (e) => { if (!e.target.matches("input, textarea")) e.preventDefault(); });

// ================================================================= boot
async function boot() {
  Tip.el = $("#tooltip");
  document.body.classList.toggle("browser-mode", SHELL !== "app");
  document.body.classList.toggle("framed", SHELL === "app" && FRAME === "xp");
  document.body.classList.toggle("native-frame", SHELL === "app" && FRAME !== "xp");
  // NooNoo blinks once when you point at her
  document.addEventListener("mouseover", (e) => {
    const host = e.target.closest(".logo, #search-empty, .tb-icon, .msgbox");
    if (host && !(e.relatedTarget && host.contains(e.relatedTarget))) blink(host);
  });
  paintIcons();
  await loadSettings();
  renderSearch();
  await poll();
  schedulePoll();
  checkTools();
  $("#q").focus();
}
boot().catch((e) => msgbox("NooNoo", "Couldn't start: " + esc(e.message), { icon: "warn32" }));
})();
