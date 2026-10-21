// ===== Global loader =========================================================================
// * a slim animated bar at the top for every request the USER triggers (saves, deletes, tab loads);
//   the automatic background refreshes (dashboard, meters, queue ...) stay silent
// * a blocking overlay with a message for long actions: busy.show("...") / busy.hide() / withBusy()
// * if the login expires (HTTP 401) the page goes back to the sign-in screen instead of filling with errors
(function () {
  const QUIET_GET = [/^\/api\/dashboard/, /^\/api\/meters/, /^\/api\/queue/, /^\/api\/zlans/, /^\/api\/connectivity-history/,
                     /^\/api\/auth\/status/, /^\/api\/backup\/status/];
  let loud = 0, showTimer = null, hideTimer = null, shownAt = 0;
  const bar = () => document.getElementById("top-progress");
  function start() {
    loud++;
    if (loud === 1) {
      clearTimeout(hideTimer);
      showTimer = setTimeout(() => { const b = bar(); if (b) { b.classList.add("on"); shownAt = Date.now(); } }, 200);   // fast requests never flash
    }
  }
  function stop() {
    loud = Math.max(0, loud - 1);
    if (loud === 0) {
      clearTimeout(showTimer);
      const wait = shownAt ? Math.max(0, 450 - (Date.now() - shownAt)) : 0;
      hideTimer = setTimeout(() => { const b = bar(); if (b) b.classList.remove("on"); shownAt = 0; }, wait);
    }
  }
  function sessionExpired() {
    try {
      const last = Number(sessionStorage.getItem("reloaded401") || 0);
      if (Date.now() - last < 30000) return;            // never loop
      sessionStorage.setItem("reloaded401", String(Date.now()));
    } catch (e) {}
    location.reload();                                  // checkAuth() then shows the sign-in screen
  }
  const orig = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const method = ((init && init.method) || "GET").toUpperCase();
    const path = url.replace(location.origin, "");
    const quiet = method === "GET" && QUIET_GET.some(re => re.test(path));
    if (!quiet) start();
    try {
      const r = await orig(input, init);
      if (r.status === 401 && !path.startsWith("/api/auth/")) sessionExpired();
      return r;
    } finally { if (!quiet) stop(); }
  };

  let overlayTimer = null, overlayDepth = 0;
  window.busy = {
    show(text, delay) {
      overlayDepth++;
      const ov = document.getElementById("busy-overlay");
      if (text) document.getElementById("busy-text").textContent = text;
      clearTimeout(overlayTimer);
      overlayTimer = setTimeout(() => { if (ov) ov.hidden = false; }, delay == null ? 150 : delay);   // short actions never flash an overlay
    },
    text(t) { const el = document.getElementById("busy-text"); if (el) el.textContent = t; },
    hide() {
      overlayDepth = Math.max(0, overlayDepth - 1);
      if (overlayDepth === 0) { clearTimeout(overlayTimer); const ov = document.getElementById("busy-overlay"); if (ov) ov.hidden = true; }
    },
  };
  window.withBusy = async function (text, fn) {
    busy.show(text);
    try { return await fn(); } finally { busy.hide(); }
  };
})();

// -- auth --
async function checkAuth() {
  const r = await fetch("/api/auth/status");
  const s = await r.json();
  if (s.logged_in) { showApp(); return; }
  document.getElementById("login-screen").style.display = "flex";
  document.getElementById("app").style.display = "none";
  document.getElementById("btn-login").textContent = s.configured ? "Sign in" : "Set up password";
}
document.getElementById("btn-login").addEventListener("click", async () => {
  const username = document.getElementById("login-user").value;
  const password = document.getElementById("login-pass").value;
  const statusRes = await fetch("/api/auth/status"); const s = await statusRes.json();
  const url = s.configured ? "/api/auth/login" : "/api/auth/setup";
  const r = await fetch(url, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({username, password}) });
  if (r.ok) { showApp(); } else {
    const d = await r.json(); document.getElementById("login-msg").textContent = d.detail || "Failed";
  }
});
document.getElementById("btn-logout").addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", {method:"POST"}); } catch (e) {}      // server clears the HttpOnly session cookie
  location.reload();
});

function showApp() {
  document.getElementById("login-screen").style.display = "none";
  document.getElementById("app").style.display = "block";
  boot();
}

// -- tabs --
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); document.getElementById("panel-"+t.dataset.tab).classList.add("active");
}));

function pager(id, loadFn) {
  let limit=50, offset=0, total=0;
  function render() {
    const el = document.getElementById(id);
    const start = total===0?0:offset+1, end=Math.min(offset+limit,total);
    el.innerHTML = `<span>${start}-${end} of ${total}</span><span><button id="${id}-p">Prev</button><button id="${id}-n">Next</button></span>`;
    document.getElementById(id+"-p").disabled = offset===0;
    document.getElementById(id+"-n").disabled = offset+limit>=total;
    document.getElementById(id+"-p").onclick=()=>{offset=Math.max(0,offset-limit);loadFn();};
    document.getElementById(id+"-n").onclick=()=>{offset+=limit;loadFn();};
  }
  return {setTotal(t){total=t;render();}, reset(){offset=0;}, get limit(){return limit;}, get offset(){return offset;}};
}

// -- dashboard --
async function loadDashboard() {
  const d = await (await fetch("/api/dashboard")).json();
  if (d.version) document.getElementById("app-version").textContent = "v" + d.version;
  document.getElementById("kpi-zlans").textContent = `${d.zlans_connected}/${d.zlans_total}`;
  document.getElementById("kpi-meters").textContent = `${d.meters_connected}/${d.meters_total}`;
  document.getElementById("kpi-queued").textContent = d.queued;
  document.getElementById("kpi-released").textContent = d.released;
  document.getElementById("kpi-failed").textContent = d.failed;
  const rows = document.getElementById("zlan-status-rows");
  const entries = Object.entries(d.per_zlan);
  rows.innerHTML = entries.length===0 ? '<tr><td colspan="6">No ZLANs configured.</td></tr>' :
    entries.map(([ip,v])=>{
      const statusLabel = v.status==='ok' ? '🟢 Up' : (v.status==='fail' ? '🔴 Down' : '⚪ Unknown');
      return `<tr><td>${v.label} (${ip})</td><td>${statusLabel}</td><td>${v.connected}/${v.total}</td><td>${v.queued}</td><td>${v.released}</td><td>${v.failed}</td></tr>`;
    }).join("");
  const ok = d.zlans_total===0 || d.zlans_connected===d.zlans_total;
  document.getElementById("system-state").textContent = ok ? "HEALTHY" : "DEGRADED";
  setBrokerChip(d.broker_connected);
}

function setBrokerChip(state) {
  const chip = document.getElementById("chip-broker");
  const label = document.getElementById("broker-state");
  chip.classList.remove("chip-good", "chip-bad", "chip-unknown");
  if (state === true) { chip.classList.add("chip-good"); label.textContent = "BROKER CONNECTED"; }
  else if (state === false) { chip.classList.add("chip-bad"); label.textContent = "BROKER DISCONNECTED"; }
  else { chip.classList.add("chip-unknown"); label.textContent = "BROKER UNKNOWN"; }
}

// -- meter_count -> N naming slots (Sec 2.1/2.2) --
document.getElementById("btn-gen-slots").addEventListener("click", () => {
  const n = Number(document.getElementById("meter-count-input").value);
  const box = document.getElementById("meter-slots");
  let html = "";
  for (let i=1;i<=n;i++) html += `<div class="add-form"><label>Unit ${i} meter code<input type="text" class="slot-input" data-unit="${i}" placeholder="M${String(i).padStart(3,'0')}"></label></div>`;
  html += `<button class="btn-primary" id="btn-save-slots">Save all meters</button>`;
  box.innerHTML = html;
  document.getElementById("btn-save-slots").addEventListener("click", async () => {
    const zlan_ip = document.getElementById("new-meter-zlan").value;
    const codes = Array.from(document.querySelectorAll(".slot-input")).map(i=>i.value.trim());
    const r = await fetch(`/api/zlans/${encodeURIComponent(zlan_ip)}/meters`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({meter_codes:codes})});
    const d = await r.json();
    const msg = document.getElementById("meter-msg");
    msg.textContent = `Created: ${d.created.length}. Errors: ${d.errors.length}`;
    msg.className = d.errors.length ? "msg error" : "msg success";
    box.innerHTML = ""; await loadMeters();
  });
});

// -- CSV bulk upload (Sec 2.4) --
document.getElementById("btn-bulk-upload").addEventListener("click", async () => {
  const file = document.getElementById("bulk-csv").files[0];
  if (!file) return;
  const text = await file.text();
  const lines = text.trim().split("\n");
  const headers = lines[0].split(",").map(h=>h.trim());
  const rows = lines.slice(1).map(line => {
    const vals = line.split(",");
    const row = {}; headers.forEach((h,i)=>row[h]=vals[i]?.trim());
    return row;
  });
  const r = await withBusy("Uploading meters\u2026", () => fetch("/api/meters/bulk-upload",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rows})}));
  const d = await r.json();
  alert(`Created: ${d.created.length}. Errors: ${d.errors.length}`);
  await loadZlans(); await loadMeters();
});

// -- UI helpers --------------------------------------------------------------
// 1) Tables that refresh themselves (ZLANs, Meters, Parameters) must never wipe what the
//    user is typing: values typed but not saved yet, and the cursor, survive a re-render.
// 2) Every Save shows "Saved" / "Not saved" right next to its button.
function renderRows(tbodyId, html) {
  const tbody = document.getElementById(tbodyId);
  const dirty = {};
  tbody.querySelectorAll("input").forEach(i => { if (i.id && i.value !== i.defaultValue) dirty[i.id] = i.value; });
  const a = document.activeElement;
  const focusId = (a && a.id && tbody.contains(a)) ? a.id : null;
  const caret = focusId && a.selectionStart != null ? a.selectionStart : null;
  tbody.innerHTML = html;
  tbody.querySelectorAll("input").forEach(i => {
    if (i.id in dirty) i.value = dirty[i.id];
    i.classList.toggle("dirty", i.value !== i.defaultValue);
  });
  if (focusId) {
    const f = document.getElementById(focusId);
    if (f) { f.focus(); try { if (caret != null) f.setSelectionRange(caret, caret); } catch (e) {} }
  }
  applyFlags();
}
document.addEventListener("input", e => {
  const i = e.target;
  if (i && i.classList && i.classList.contains("interval-input")) i.classList.toggle("dirty", i.value !== i.defaultValue);
});
const _flags = {};   // input id -> {ok, text, until}
function flashSaved(inputId, ok, text) {
  _flags[inputId] = { ok, text: text || (ok ? "\u2713 Saved" : "\u2717 Not saved"), until: Date.now() + (ok ? 3000 : 6000) };
  applyFlags();
  setTimeout(applyFlags, ok ? 3100 : 6100);
}
function applyFlags() {
  Object.keys(_flags).forEach(id => {
    const f = _flags[id], el = document.getElementById(id);
    if (!el) return;
    const btn = el.nextElementSibling;
    if (!btn) return;
    let span = btn.nextElementSibling && btn.nextElementSibling.classList.contains("save-flag") ? btn.nextElementSibling : null;
    if (Date.now() > f.until) { if (span) span.remove(); delete _flags[id]; return; }
    if (!span) { span = document.createElement("span"); btn.after(span); }
    span.className = "save-flag " + (f.ok ? "ok" : "bad");
    span.textContent = f.text;
  });
}
async function putJson(url, body) {
  try {
    const r = await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.ok) return { ok: true };
    let msg = "HTTP " + r.status;
    try { const d = await r.json(); if (d && typeof d.detail === "string") msg = d.detail; } catch (e) {}
    return { ok: false, msg };
  } catch (e) { return { ok: false, msg: "Network error" }; }
}
// Save one interval input: validate, PUT, reload the table, show the result next to the button.
async function saveInterval(inputId, url, key, { required, reload }) {
  const raw = document.getElementById(inputId).value.trim();
  if (raw === "" && required) { flashSaved(inputId, false, "\u2717 Enter minutes (1 or more)"); return; }
  const r = await putJson(url, { [key]: raw === "" ? null : Number(raw) });
  if (!r.ok) { flashSaved(inputId, false, "\u2717 " + r.msg); return; }
  await reload();
  flashSaved(inputId, true);
}

// -- ZLANs --
async function loadZlans() {
  const zlans = await (await fetch("/api/zlans")).json();
  renderRows("zlan-rows", zlans.length===0 ? '<tr><td colspan="6">None yet.</td></tr>' :
    zlans.map(z=>`<tr><td>${z.zlan_ip}</td><td>${z.zlan_label}</td>
      <td><input type="number" class="interval-input" id="zlan-int-${z.zlan_ip}" value="${z.default_polling_interval}" style="width:70px">
        <button class="btn-ghost" onclick="saveZlanInterval('${z.zlan_ip}')">Save</button></td>
      <td><input type="number" class="interval-input" id="zlan-pub-${z.zlan_ip}" value="${z.default_publish_interval||''}" placeholder="(every read)" style="width:70px">
        <button class="btn-ghost" onclick="saveZlanPublishInterval('${z.zlan_ip}')">Save</button></td>
      <td>${z.port}</td>
      <td><button class="btn-delete" onclick="delZlan('${z.zlan_ip}')">Remove</button></td></tr>`).join(""));
  const sel = document.getElementById("new-meter-zlan");
  sel.innerHTML = zlans.map(z=>`<option value="${z.zlan_ip}">${z.zlan_label} (${z.zlan_ip})</option>`).join("");
  const connSel = document.getElementById("conn-zlan-filter");
  if (connSel) {
    const prevSelection = connSel.value;  // preserve the user's current filter choice --
      // this dropdown gets rebuilt on every connectivity event over the
      // WebSocket (to pick up newly added ZLANs), which was wiping out
      // whatever the user had selected back to "All ZLANs" every 2-3s.
    connSel.innerHTML = '<option value="">All ZLANs</option>' + zlans.map(z=>`<option value="${z.zlan_ip}">${z.zlan_label} (${z.zlan_ip})</option>`).join("");
    if (prevSelection && zlans.some(z => z.zlan_ip === prevSelection)) connSel.value = prevSelection;
  }
}
async function saveZlanInterval(zlan_ip){
  await saveInterval(`zlan-int-${zlan_ip}`, `/api/zlans/${encodeURIComponent(zlan_ip)}/interval`, "default_polling_interval", {required:true, reload:loadZlans});
}
async function saveZlanPublishInterval(zlan_ip){
  await saveInterval(`zlan-pub-${zlan_ip}`, `/api/zlans/${encodeURIComponent(zlan_ip)}/publish-interval`, "default_publish_interval", {required:false, reload:loadZlans});
}
document.getElementById("btn-add-zlan").addEventListener("click", async () => {
  const body = {zlan_ip: document.getElementById("new-zlan-ip").value, zlan_label: document.getElementById("new-zlan-label").value,
    default_polling_interval: Number(document.getElementById("new-zlan-interval").value), port: Number(document.getElementById("new-zlan-port").value)};
  const r = await fetch("/api/zlans",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json(); const msg=document.getElementById("zlan-msg");
  if(!r.ok){msg.textContent=d.detail;msg.className="msg error";return;}
  msg.textContent="Added."; msg.className="msg success"; await loadZlans();
});
async function delZlan(ip){ if(!confirm("Remove ZLAN "+(ip||"(blank)")+"?"))return; await fetch("/api/zlans?zlan_ip="+encodeURIComponent(ip),{method:"DELETE"}); await loadZlans(); await loadMeters(); }

// -- Meters --
async function loadMeters() {
  const search = document.getElementById("meter-search").value;
  const meters = await (await fetch("/api/meters")).json();
  const filtered = search ? meters.filter(m=>m.meter_code.toLowerCase().includes(search.toLowerCase())) : meters;
  renderRows("meter-rows", filtered.length===0 ? '<tr><td colspan="8">None yet.</td></tr>' :
    filtered.map(m=>`<tr><td>${m.meter_code}</td><td>${m.zlan_ip}</td><td>${m.unit_id}</td>
      <td><input type="number" class="interval-input" id="meter-int-${m.meter_code}" value="${m.polling_interval||''}" placeholder="(default)" style="width:70px">
        <button class="btn-ghost" onclick="saveMeterInterval('${m.meter_code}')">Save</button></td>
      <td><input type="number" class="interval-input" id="meter-pub-${m.meter_code}" value="${m.publish_interval||''}" placeholder="(zlan default)" style="width:70px">
        <button class="btn-ghost" onclick="saveMeterPublishInterval('${m.meter_code}')">Save</button></td>
      <td><span class="pill ${m.last_status==='ok'?'released':(m.last_status==='fail'?'failed':'queued')}">${m.active?m.last_status:'inactive'}</span></td>
      <td>${m.last_poll_at||"-"}</td><td><button class="btn-delete" onclick="toggleMeter('${m.meter_code}',${!m.active})">${m.active?'Deactivate':'Activate'}</button></td></tr>`).join(""));
}
document.getElementById("meter-search").addEventListener("input", loadMeters);
async function toggleMeter(code,active){ await fetch(`/api/meters/${encodeURIComponent(code)}/active`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({active})}); await loadMeters(); }
async function saveMeterInterval(code){
  await saveInterval(`meter-int-${code}`, `/api/meters/${encodeURIComponent(code)}/interval`, "polling_interval", {required:false, reload:loadMeters});
}
async function saveMeterPublishInterval(code){
  await saveInterval(`meter-pub-${code}`, `/api/meters/${encodeURIComponent(code)}/publish-interval`, "publish_interval", {required:false, reload:loadMeters});
}

// -- Parameters --
async function loadParams() {
  const ps = await (await fetch("/api/parameters")).json();
  renderRows("param-rows", ps.length===0 ? '<tr><td colspan="9">None yet.</td></tr>' :
    ps.map(p=>`<tr><td>${p.label}</td><td>${p.address}</td><td>${p.wire_code}</td><td>${p.priority}</td><td>${p.min_value??'-'}</td><td>${p.max_value??'-'}</td>
      <td><input type="number" min="1" class="interval-input" id="param-poll-${p.label}" value="${p.poll_interval||''}" placeholder="(each cycle)" style="width:115px"></td>
      <td><input type="number" min="1" class="interval-input" id="param-pub-${p.label}" value="${p.publish_interval||''}" placeholder="(meter/ZLAN)" style="width:115px">
        <button class="btn-ghost" onclick="saveParamIntervals('${p.label}')">Save</button></td>
      <td><button class="btn-delete" onclick="delParam('${p.label}')">Remove</button></td></tr>`).join(""));
}
async function saveParamIntervals(label){
  const pollId = "param-poll-"+label, pubId = "param-pub-"+label;
  const val = id => document.getElementById(id).value.trim();
  const msg = document.getElementById("param-msg");
  const r = await putJson("/api/parameters/"+encodeURIComponent(label)+"/intervals",
    { poll_interval: val(pollId)==="" ? null : Number(val(pollId)), publish_interval: val(pubId)==="" ? null : Number(val(pubId)) });
  if (!r.ok) { msg.textContent = r.msg; msg.className = "msg error"; flashSaved(pubId, false, "\u2717 "+r.msg); return; }
  msg.textContent = "Saved "+label+" intervals."; msg.className = "msg success";
  await loadParams();
  flashSaved(pubId, true);
}
document.getElementById("btn-add-param").addEventListener("click", async () => {
  const body = {label:document.getElementById("new-param-label").value, address:Number(document.getElementById("new-param-address").value),
    wire_code:document.getElementById("new-param-wire").value, priority:Number(document.getElementById("new-param-priority").value),
    min_value: document.getElementById("new-param-min").value || null, max_value: document.getElementById("new-param-max").value || null,
    poll_interval: document.getElementById("new-param-poll").value || null, publish_interval: document.getElementById("new-param-publish").value || null};
  const r = await fetch("/api/parameters",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json(); const msg=document.getElementById("param-msg");
  if(!r.ok){msg.textContent=d.detail;msg.className="msg error";return;}
  msg.textContent="Added."; msg.className="msg success"; await loadParams();
});
async function delParam(label){ if(!confirm("Remove "+label+"?"))return; await fetch("/api/parameters/"+encodeURIComponent(label),{method:"DELETE"}); await loadParams(); }

// -- Queue --
const queuePager = pager("queue-pagination", ()=>loadQueue());
async function loadQueue() {
  const search = document.getElementById("queue-search").value;
  const r = await (await fetch(`/api/queue?search=${encodeURIComponent(search)}&limit=${queuePager.limit}&offset=${queuePager.offset}`)).json();
  document.getElementById("queue-rows").innerHTML = r.items.length===0 ? '<tr><td colspan="7">Queue is empty.</td></tr>' :
    r.items.map(q=>`<tr><td>${q.meter_code}</td><td>${q.parameter}</td><td>${q.value}</td><td>${q.date_time}</td><td>${q.attempts}</td><td>${q.priority}</td>
      <td>${q.awaiting_response?'awaiting response':'pending retry'}</td></tr>`).join("");
  queuePager.setTotal(r.total);
}
document.getElementById("queue-search").addEventListener("input", ()=>{queuePager.reset();loadQueue();});

// -- History --
const historyPager = pager("history-pagination", ()=>loadHistory());
function pill(status, note) {
  // "released" with a server-side note (e.g. Laravel's "Meter Doesn't
  // Exist!") means the message was delivered but NOT actually usable
  // data -- flagging this distinctly so it can't be mistaken for a
  // clean success at a glance.
  if (status === "released" && note) return `<span class="pill warn-released">released (see note)</span>`;
  return `<span class="pill ${status}">${status}</span>`;
}
let historySort = {by: "", dir: "desc"};
async function loadHistory() {
  const search = document.getElementById("history-search").value;
  const status = document.getElementById("history-status-filter").value;
  const meter_code = document.getElementById("history-meter-filter").value;
  const parameter = document.getElementById("history-param-filter").value;
  const date_from = document.getElementById("history-date-from").value;
  const date_to = document.getElementById("history-date-to").value;
  const qs = new URLSearchParams({search,status,meter_code,parameter,date_from,date_to,
    order_by:historySort.by, order_dir:historySort.dir, limit:historyPager.limit,offset:historyPager.offset});
  const r = await (await fetch(`/api/history?${qs}`)).json();
  document.getElementById("history-rows").innerHTML = r.items.length===0 ? '<tr><td colspan="9">No history.</td></tr>' :
    r.items.map(h=>`<tr><td><input type="checkbox" class="history-check" value="${h.msg_no}"></td><td>${h.meter_code}</td><td>${h.parameter}</td><td>${h.value}</td><td>${h.date_time}</td>
      <td>${(h.released_at||"").slice(0,19).replace("T"," ")||"-"}</td><td>${pill(h.status, h.error_message)}</td>
      <td>${h.error_message||"-"}</td>
      <td>${h.status==='failed'?`<button class="btn-delete" onclick="retryHist('${h.msg_no}')">Retry</button>`:''}</td></tr>`).join("");
  historyPager.setTotal(r.total);
  document.querySelectorAll("#panel-history th.sortable .sort-arrow").forEach(el=>el.remove());
  if (historySort.by) {
    const th = document.querySelector(`#panel-history th.sortable[data-sort="${historySort.by}"]`);
    if (th) th.insertAdjacentHTML("beforeend", `<span class="sort-arrow">${historySort.dir==="asc"?"▲":"▼"}</span>`);
  }
}
document.querySelectorAll("#panel-history th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (historySort.by === col) historySort.dir = historySort.dir === "asc" ? "desc" : "asc";
    else { historySort.by = col; historySort.dir = "asc"; }
    historyPager.reset();
    loadHistory();
  });
});
document.getElementById("history-select-all").addEventListener("change", (e) => {
  document.querySelectorAll(".history-check").forEach(cb => cb.checked = e.target.checked);
});
function getSelectedHistoryMsgNos(){
  return Array.from(document.querySelectorAll(".history-check:checked")).map(cb => cb.value);
}
async function deleteHistorySelected(){
  const msg_nos = getSelectedHistoryMsgNos();
  if (msg_nos.length === 0) { alert("No rows selected."); return; }
  if (!confirm(`Delete ${msg_nos.length} selected history row(s)? This cannot be undone.`)) return;
  const r = await fetch("/api/history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"selected",msg_nos})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadHistory();
}
async function deleteHistoryMonths(n){
  if (!confirm(`Delete all history older than ${n} month(s)? This cannot be undone.`)) return;
  const r = await fetch("/api/history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"months",months:n})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadHistory();
}
async function deleteHistoryCustom(){
  const date = document.getElementById("history-delete-custom").value;
  if (!date) { alert("Pick a date first."); return; }
  if (!confirm(`Delete all history created before ${date}? This cannot be undone.`)) return;
  const r = await fetch("/api/history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"custom",before:date})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadHistory();
}
async function deleteHistoryAll(){
  if (!confirm("Delete ALL history records? This cannot be undone.")) return;
  const r = await fetch("/api/history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"all"})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadHistory();
}
["history-search","history-meter-filter","history-param-filter"].forEach(id=>document.getElementById(id).addEventListener("input", ()=>{historyPager.reset();loadHistory();}));
["history-status-filter","history-date-from","history-date-to"].forEach(id=>document.getElementById(id).addEventListener("change", ()=>{historyPager.reset();loadHistory();}));
async function retryHist(msgNo){ await fetch(`/api/history/${encodeURIComponent(msgNo)}/retry`,{method:"POST"}); await loadHistory(); }
async function retryAllFailed(){
  if (!confirm("Retry all failed history rows? They'll be re-queued for delivery.")) return;
  const r = await fetch("/api/history/retry-all",{method:"POST"});
  const d = await r.json();
  alert(`Re-queued ${d.requeued} failed row(s).`);
  await loadHistory();
}

function exportTable(name){ window.open(`/api/export/${name}`, "_blank"); }

// -- Connectivity history --
const connPager = pager("connectivity-pagination", ()=>loadConnectivity());
function localDateStr(d){
  const y = d.getFullYear(), m = String(d.getMonth()+1).padStart(2,"0"), day = String(d.getDate()).padStart(2,"0");
  return `${y}-${m}-${day}`;
}
function connPresetRange(preset){
  const to = new Date();
  const from = new Date();
  if (preset === "daily") { /* from stays as today */ }
  else if (preset === "weekly") from.setDate(to.getDate()-7);
  else if (preset === "monthly") from.setDate(to.getDate()-30);
  else return {from:"", to:""};
  return {from: localDateStr(from), to: localDateStr(to)};
}
document.getElementById("conn-range-preset").addEventListener("change", () => {
  const preset = document.getElementById("conn-range-preset").value;
  const custom = preset === "custom";
  document.getElementById("conn-date-from").style.display = custom ? "" : "none";
  document.getElementById("conn-date-to").style.display = custom ? "" : "none";
  if (!custom) {
    const {from, to} = connPresetRange(preset);
    document.getElementById("conn-date-from").value = from;
    document.getElementById("conn-date-to").value = to;
  }
  connPager.reset(); loadConnectivity();
});
function connQuery(){
  return {
    entity_type: document.getElementById("conn-entity-type").value,
    entity_id: document.getElementById("conn-entity-id").value,
    zlan_ip: document.getElementById("conn-zlan-filter").value,
    date_from: document.getElementById("conn-date-from").value,
    date_to: document.getElementById("conn-date-to").value,
  };
}
async function loadConnectivity(){
  const qs = new URLSearchParams({...connQuery(), limit:connPager.limit, offset:connPager.offset});
  const r = await (await fetch(`/api/connectivity-history?${qs}`)).json();
  document.getElementById("connectivity-rows").innerHTML = r.items.length===0 ? '<tr><td colspan="7">No connectivity events.</td></tr>' :
    r.items.map(c=>`<tr><td><input type="checkbox" class="conn-check" value="${c.msg_no}"></td><td>${c.entity_type}</td><td>${c.entity_id}</td>
      <td>${c.zlan_label ? `${c.zlan_label} (${c.zlan_ip})` : "-"}</td>
      <td><span class="pill ${c.status==='connected'?'released':'failed'}">${c.status}</span></td>
      <td><span class="pill ${c.delivery_status}">${c.delivery_status}</span></td>
      <td>${c.at}</td></tr>`).join("");
  connPager.setTotal(r.total);
}
document.getElementById("conn-zlan-filter").addEventListener("change", ()=>{connPager.reset();loadConnectivity();});
document.getElementById("conn-select-all").addEventListener("change", (e) => {
  document.querySelectorAll(".conn-check").forEach(cb => cb.checked = e.target.checked);
});
function getSelectedConnMsgNos(){
  return Array.from(document.querySelectorAll(".conn-check:checked")).map(cb => cb.value);
}
async function deleteConnectivitySelected(){
  const msg_nos = getSelectedConnMsgNos();
  if (msg_nos.length === 0) { alert("No rows selected."); return; }
  if (!confirm(`Delete ${msg_nos.length} selected connectivity row(s)? This cannot be undone.`)) return;
  const r = await fetch("/api/connectivity-history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"selected",msg_nos})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadConnectivity();
}
async function deleteConnectivityMonths(n){
  if (!confirm(`Delete all connectivity history older than ${n} month(s)? This cannot be undone.`)) return;
  const r = await fetch("/api/connectivity-history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"months",months:n})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadConnectivity();
}
async function deleteConnectivityCustom(){
  const date = document.getElementById("conn-delete-custom").value;
  if (!date) { alert("Pick a date first."); return; }
  if (!confirm(`Delete all connectivity history before ${date}? This cannot be undone.`)) return;
  const r = await fetch("/api/connectivity-history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"custom",before:date})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadConnectivity();
}
async function deleteConnectivityAll(){
  if (!confirm("Delete ALL connectivity history? This cannot be undone.")) return;
  const r = await fetch("/api/connectivity-history",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"all"})});
  const d = await r.json(); alert(`Deleted ${d.deleted} row(s).`); await loadConnectivity();
}
document.getElementById("conn-entity-type").addEventListener("change", ()=>{connPager.reset();loadConnectivity();});
document.getElementById("conn-entity-id").addEventListener("input", ()=>{connPager.reset();loadConnectivity();});
document.getElementById("conn-date-from").addEventListener("change", ()=>{connPager.reset();loadConnectivity();});
document.getElementById("conn-date-to").addEventListener("change", ()=>{connPager.reset();loadConnectivity();});
function exportConnectivity(){
  const qs = new URLSearchParams(connQuery());
  window.open(`/api/export/connectivity-history?${qs}`, "_blank");
}

// -- Output settings --
function toggleConnFields(){
  const t = document.getElementById("conn-type").value;
  document.getElementById("http-fields").style.display = t==="http"?"":"none";
  document.getElementById("mqtt-fields").style.display = t==="mqtt"?"":"none";
}
document.getElementById("conn-type").addEventListener("change", toggleConnFields);
async function loadOutput() {
  const s = await (await fetch("/api/settings")).json();
  document.getElementById("conn-type").value = s.connection_type;
  document.getElementById("out-url").value = s.server_url;
  document.getElementById("mqtt-broker").value = s.mqtt_broker;
  document.getElementById("mqtt-username").value = s.mqtt_username || "";
  document.getElementById("mqtt-password").value = s.mqtt_password || "";
  document.getElementById("mqtt-port").value = s.mqtt_port;
  document.getElementById("mqtt-topic-tmpl").value = s.mqtt_topic_template;
  document.getElementById("mqtt-resp-topic").value = s.mqtt_response_topic;
  document.getElementById("mqtt-conn-topic-tmpl").value = s.mqtt_connectivity_topic_template;
  document.getElementById("mqtt-resp-timeout").value = s.mqtt_response_timeout;
  document.getElementById("out-enabled").checked = s.server_enabled==="true";
  document.getElementById("set-max-attempts").value = s.max_attempts;
  document.getElementById("set-max-queue").value = s.max_queue_size;
  document.getElementById("set-queue-retention").value = s.queue_retention_days;
  document.getElementById("set-retention").value = s.retention_days;
  document.getElementById("set-conn-retention").value = s.connectivity_retention_days;
  document.getElementById("set-modbus-timeout").value = s.modbus_timeout;
  document.getElementById("set-inter-delay").value = s.modbus_inter_request_delay;
  document.getElementById("set-publish-throttle-enabled").checked = s.publish_throttle_enabled==="true";
  document.getElementById("set-log-skipped-events").checked = s.log_skipped_events==="true";
  document.getElementById("set-connectivity-debounce-enabled").checked = s.connectivity_debounce_enabled==="true";
  document.getElementById("set-connectivity-min-gap").value = s.mqtt_connectivity_min_gap_seconds;
  document.getElementById("set-heartbeat-enabled").checked = s.connectivity_heartbeat_enabled==="true";
  document.getElementById("set-heartbeat-hours").value = s.connectivity_heartbeat_hours;
  document.getElementById("set-heartbeat-check-minutes").value = s.connectivity_heartbeat_check_minutes;
  document.getElementById("set-backup-enabled").checked = s.backup_enabled!=="false";
  document.getElementById("set-backup-retention").value = s.backup_retention_days;
  loadBackupStatus();
  toggleConnFields();
}
// ===== Output connection: validation, save, connection check =================================
const OUT = {   // setting key -> input id
  connection_type: "conn-type", server_url: "out-url", mqtt_broker: "mqtt-broker", mqtt_port: "mqtt-port",
  mqtt_username: "mqtt-username", mqtt_password: "mqtt-password", mqtt_topic_template: "mqtt-topic-tmpl",
  mqtt_response_topic: "mqtt-resp-topic", mqtt_response_timeout: "mqtt-resp-timeout",
  mqtt_connectivity_topic_template: "mqtt-conn-topic-tmpl",
};
const $ = id => document.getElementById(id);
function readOutput() {
  const v = {};
  for (const [k, id] of Object.entries(OUT)) v[k] = $(id).value.trim();
  v.mqtt_password = $("mqtt-password").value;                       // passwords may contain spaces
  v.server_enabled = $("out-enabled").checked ? "true" : "false";
  return v;
}
function templateError(t, allowed) {                                // same rules as the server
  if (!t) return "Required.";
  const rest = t.replace(/\{(\w+)\}/g, (m, name) => allowed.includes(name) ? "" : "\u0000" + name);
  const bad = rest.match(/\u0000(\w+)/);
  if (bad) return "Unknown placeholder {" + bad[1] + "}. Allowed: " + allowed.map(a => "{" + a + "}").join(", ") + ".";
  if (/[{}]/.test(rest)) return "The { } braces do not match. Allowed placeholders: " + allowed.map(a => "{" + a + "}").join(", ") + ".";
  if (/[+#]/.test(t)) return "A publish topic cannot contain + or #.";
  return null;
}
function validateOutput(v, needAll) {                               // quick checks before asking the server
  const e = {}, need = needAll || v.server_enabled === "true";
  if (v.connection_type === "http") {
    if (!v.server_url) { if (need) e.server_url = "Required."; }
    else if (!/^https?:\/\/[^\s/]+\S*$/.test(v.server_url)) e.server_url = "Must start with http:// or https:// followed by the server address.";
    return e;
  }
  if (!v.mqtt_broker) { if (need) e.mqtt_broker = "Required."; }
  else if (/:\/\/|\/|\s/.test(v.mqtt_broker)) e.mqtt_broker = "Enter only the address (for example 103.224.93.124): no mqtt://, no path, no spaces.";
  else if ((v.mqtt_broker.match(/:/g) || []).length === 1) e.mqtt_broker = "Put the port in the Port box, not in the address.";
  if (!v.mqtt_port) { if (need) e.mqtt_port = "Required."; }
  else if (!/^\d+$/.test(v.mqtt_port) || +v.mqtt_port < 1 || +v.mqtt_port > 65535) e.mqtt_port = "Port must be a whole number from 1 to 65535 (usually 1883).";
  if (v.mqtt_password && !v.mqtt_username) e.mqtt_username = "Required when a password is entered.";
  if (v.mqtt_topic_template || need) { const m = templateError(v.mqtt_topic_template, ["meter_code", "label"]); if (m) e.mqtt_topic_template = m; }
  if (v.mqtt_connectivity_topic_template || need) { const m = templateError(v.mqtt_connectivity_topic_template, ["entity_id"]); if (m) e.mqtt_connectivity_topic_template = m; }
  if (!v.mqtt_response_timeout) { if (need && v.mqtt_response_topic) e.mqtt_response_timeout = "Required when a response topic is set."; }
  else if (!/^\d+$/.test(v.mqtt_response_timeout) || +v.mqtt_response_timeout < 1 || +v.mqtt_response_timeout > 300) e.mqtt_response_timeout = "Seconds, from 1 to 300.";
  return e;
}
function clearOutputErrors() {
  for (const [k, id] of Object.entries(OUT)) {
    $(id).classList.remove("invalid");
    const el = $("err-" + k); if (el) el.textContent = "";
  }
}
function showOutputErrors(errors) {
  clearOutputErrors();
  let first = null;
  for (const [k, msg] of Object.entries(errors)) {
    const id = OUT[k]; if (!id) continue;
    $(id).classList.add("invalid");
    const el = $("err-" + k); if (el) el.textContent = msg;
    if (!first) first = $(id);
  }
  if (first) { first.scrollIntoView({block: "center", behavior: "smooth"}); first.focus({preventScroll: true}); }
}
document.querySelectorAll("#panel-output input, #panel-output select").forEach(el => {
  el.addEventListener("input", () => {                                // an error disappears as soon as the box is edited
    el.classList.remove("invalid");
    const k = Object.keys(OUT).find(key => OUT[key] === el.id); if (k && $("err-" + k)) $("err-" + k).textContent = "";
  });
});
$("mqtt-show-pw").addEventListener("change", e => { $("mqtt-password").type = e.target.checked ? "text" : "password"; });

// status panel: banner + one line per check (built with textContent: host names come from the user)
function renderStatus(state, title, lines) {
  const box = $("output-status"); box.hidden = false; box.replaceChildren();
  const banner = document.createElement("div"); banner.className = "conn-banner " + state;
  if (state === "run") { const sp = document.createElement("div"); sp.className = "spinner small"; banner.append(sp); }
  else banner.append(Object.assign(document.createElement("span"), {textContent: state === "ok" ? "✓" : state === "warn" ? "!" : "✗"}));
  banner.append(Object.assign(document.createElement("span"), {textContent: title}));
  box.append(banner);
  if (lines && lines.length) {
    const ul = document.createElement("ul"); ul.className = "conn-checks";
    for (const l of lines) {
      const li = document.createElement("li"); li.className = l.state;
      const ic = document.createElement("span"); ic.className = "ic";
      if (l.state === "run") { const sp = document.createElement("div"); sp.className = "spinner small"; ic.append(sp); }
      else ic.textContent = l.state === "ok" ? "✓" : "✗";
      const body = document.createElement("div");
      if (l.name) body.append(Object.assign(document.createElement("b"), {textContent: l.name + " "}));
      body.append(document.createTextNode(l.text));
      if (l.hint) body.append(Object.assign(document.createElement("small"), {textContent: l.hint}));
      li.append(ic, body);
      if (l.meta) li.append(Object.assign(document.createElement("span"), {className: "meta", textContent: l.meta}));
      ul.append(li);
    }
    box.append(ul);
  }
}
function targetOf(v) { return v.connection_type === "mqtt" ? `${v.mqtt_broker}:${v.mqtt_port}` : v.server_url; }
function checkLines(res) {
  return res.checks.map((c, i) => ({state: c.ok ? "ok" : "bad", name: c.name, text: c.text, hint: c.hint,
                                    meta: (i === 1 && c.ok && res.latency_ms != null) ? res.latency_ms + " ms" : ""}));
}
async function apiJson(url, method, body) {
  try {
    const r = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    let data = null; try { data = await r.json(); } catch (e) {}
    return {ok: r.ok, status: r.status, data};
  } catch (e) { return {ok: false, status: 0, data: {detail: "Cannot reach the gateway (network error)."}}; }
}

let outputBusy = false;
async function outputFlow(save) {
  if (outputBusy) return;
  const v = readOutput();
  clearOutputErrors();
  // saving a half-filled form is fine while the output is switched off; a connection test always needs the full set
  const errs = validateOutput(v, !save);
  if (Object.keys(errs).length) {
    showOutputErrors(errs);
    renderStatus("bad", "Please fix the highlighted fields" + (save ? " — nothing was saved." : "."), []);
    return;
  }
  outputBusy = true; $("btn-save-output").disabled = true; $("btn-test-output").disabled = true;
  const done = [];
  try {
    busy.show(save ? "Saving connection settings…" : `Checking connection to ${targetOf(v)}…`);   // one show, one hide (in finally)
    if (save) {
      renderStatus("run", "Saving settings…", []);
      const r = await apiJson("/api/settings", "PUT", v);
      if (!r.ok) {
        if (r.data && r.data.errors) showOutputErrors(r.data.errors);
        renderStatus("bad", "Not saved: " + ((r.data && r.data.detail) || "unexpected error"), []);
        return;
      }
      done.push({state: "ok", text: "Settings saved"});
    }
    const complete = Object.keys(validateOutput(v, true)).length === 0;
    if (save && !complete) {                                   // a draft: nothing to connect to yet
      renderStatus("warn", "Saved. The connection was not checked because some fields are still empty.", done);
      return;
    }
    busy.text(`Checking connection to ${targetOf(v)}…`);
    renderStatus("run", `Checking connection to ${targetOf(v)}…`, done.concat([{state: "run", text: "Contacting the server…"}]));
    const t = await apiJson("/api/output/test", "POST", v);
    if (!t.ok) {
      if (t.data && t.data.errors) showOutputErrors(t.data.errors);
      renderStatus("bad", (save ? "Saved, but the connection check could not run: " : "Connection check could not run: ") + ((t.data && t.data.detail) || "error"), done);
      return;
    }
    const res = t.data, lines = done.concat(checkLines(res));
    if (res.ok) {
      const off = save && v.server_enabled !== "true";
      renderStatus(off ? "warn" : "ok",
        (save ? "Saved and connected" : "Connection works") + ` — ${res.target}` + (off ? ". The output is switched OFF, so nothing is being sent yet." : "."), lines);
    } else {
      renderStatus("bad", (save ? "Saved, but the connection check failed. " : "Connection check failed. ") + res.message, lines);
    }
    if (save) { setTimeout(loadDashboard, 1500); setTimeout(loadDashboard, 4500); }       // refresh the broker chip once it reconnected
  } finally {
    busy.hide(); outputBusy = false; $("btn-save-output").disabled = false; $("btn-test-output").disabled = false;
  }
}
$("btn-save-output").addEventListener("click", () => outputFlow(true));
$("btn-test-output").addEventListener("click", () => outputFlow(false));
document.getElementById("btn-save-settings").addEventListener("click", async () => {
  const body = {max_attempts: document.getElementById("set-max-attempts").value, max_queue_size: document.getElementById("set-max-queue").value,
    queue_retention_days: document.getElementById("set-queue-retention").value};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("settings-msg"); msg.textContent="Saved."; msg.className="msg success";
});
document.getElementById("btn-save-history-retention").addEventListener("click", async () => {
  const body = {retention_days: document.getElementById("set-retention").value, connectivity_retention_days: document.getElementById("set-conn-retention").value};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("history-retention-msg"); msg.textContent="Saved."; msg.className="msg success";
});
document.getElementById("btn-save-modbus-tuning").addEventListener("click", async () => {
  const body = {modbus_timeout: document.getElementById("set-modbus-timeout").value,
    modbus_inter_request_delay: document.getElementById("set-inter-delay").value};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("modbus-tuning-msg"); msg.textContent="Saved. Restart the app to apply to running ZLANs."; msg.className="msg success";
});
async function loadBackupStatus(){
  try {
    const b = await (await fetch("/api/backup/status")).json();
    document.getElementById("backup-status").textContent = (b.enabled ? "Backup is ON. " : "Backup is OFF. ") +
      `${b.files} file(s), ${b.size_mb} MB, latest: ${b.latest_file||"none yet"}. Folder: ${b.path}. Free disk: ${b.free_disk_mb} MB.`;
  } catch(e) { document.getElementById("backup-status").textContent = "Backup status unavailable."; }
}
document.getElementById("btn-save-backup").addEventListener("click", async () => {
  const body = {backup_enabled: document.getElementById("set-backup-enabled").checked?"true":"false",
    backup_retention_days: document.getElementById("set-backup-retention").value || "0"};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("backup-msg"); msg.textContent="Saved."; msg.className="msg success"; loadBackupStatus();
});
document.getElementById("btn-save-throttle").addEventListener("click", async () => {
  const body = {publish_throttle_enabled: document.getElementById("set-publish-throttle-enabled").checked?"true":"false",
    log_skipped_events: document.getElementById("set-log-skipped-events").checked?"true":"false"};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("throttle-msg"); msg.textContent="Saved."; msg.className="msg success";
});
document.getElementById("btn-save-debounce").addEventListener("click", async () => {
  const body = {connectivity_debounce_enabled: document.getElementById("set-connectivity-debounce-enabled").checked?"true":"false",
    mqtt_connectivity_min_gap_seconds: document.getElementById("set-connectivity-min-gap").value};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("debounce-msg"); msg.textContent="Saved."; msg.className="msg success";
});
document.getElementById("btn-save-heartbeat").addEventListener("click", async () => {
  const body = {connectivity_heartbeat_enabled: document.getElementById("set-heartbeat-enabled").checked?"true":"false",
    connectivity_heartbeat_hours: document.getElementById("set-heartbeat-hours").value,
    connectivity_heartbeat_check_minutes: document.getElementById("set-heartbeat-check-minutes").value};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("heartbeat-msg"); msg.textContent="Saved."; msg.className="msg success";
});
document.getElementById("btn-export-config").addEventListener("click", async () => {
  const d = await (await fetch("/api/config/export")).json();
  const blob = new Blob([JSON.stringify(d,null,2)], {type:"application/json"});
  const a = document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="bridge_config.json"; a.click();
});
document.getElementById("btn-import-config").addEventListener("click", () => document.getElementById("import-file").click());
document.getElementById("import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if(!file) return;
  const text = await file.text();
  await withBusy("Importing configuration\u2026", () => fetch("/api/config/import",{method:"POST",headers:{"Content-Type":"application/json"},body:text}));
  alert("Configuration imported."); location.reload();
});
document.getElementById("btn-change-pw").addEventListener("click", async () => {
  const body = {current: document.getElementById("pw-current").value, new: document.getElementById("pw-new").value};
  const r = await fetch("/api/auth/change-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json(); const msg=document.getElementById("pw-msg");
  msg.textContent = r.ok ? "Password updated." : (d.detail||"Failed"); msg.className = r.ok?"msg success":"msg error";
});

// after a restart: keep the overlay up until the app answers again, then reload the page
function waitForApp(text) {
  busy.show(text + " This page reloads by itself.", 0);
  const started = Date.now();
  const tick = async () => {
    try { const r = await fetch("/api/auth/status", {cache: "no-store"}); if (r.ok && Date.now() - started > 3000) { location.reload(); return; } } catch (e) {}
    if (Date.now() - started > 90000) { busy.text("The app is taking long to come back. Reload this page manually."); return; }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2500);
}
async function restartApp(){
  if (!confirm("Restart the app process now? The dashboard will briefly disconnect and come back automatically.")) return;
  await withBusy("Restarting the app\u2026", () => fetch("/api/system/restart",{method:"POST"}));
  waitForApp("Restarting the app\u2026");
}
async function resetSettingsOnly(){
  if (!confirm("Reset all settings to their defaults? Your ZLANs, meters, and history are not affected.")) return;
  await withBusy("Resetting settings\u2026", () => fetch("/api/settings/reset",{method:"POST"}));
  alert("Settings reset to defaults.");
  await loadOutput();
  location.reload();
}
async function factoryReset(){
  if (!confirm("This will PERMANENTLY DELETE everything -- ZLANs, meters, parameters, all history, and settings, including your login. Continue?")) return;
  const password = prompt("Re-enter your current password to confirm factory reset:");
  if (!password) return;
  const r = await withBusy("Resetting everything\u2026", () => fetch("/api/system/factory-reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password})}));
  if (!r.ok) { const d = await r.json(); alert(d.detail || "Failed."); return; }
  waitForApp("Factory reset complete. The app is restarting\u2026");
}

// -- refresh policy --
// A busy gateway emits several live events per second. Reloading the dashboard / meters / connectivity on EVERY
// event (and on a timer, even in a background tab) kept the gateway's database busy for nothing, so refreshes are
// limited to one per 3 s and are skipped while the tab is hidden.
function throttled(fn, ms) {
  let last = 0, timer = null;
  return function () {
    if (document.hidden) return;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) { last = Date.now(); fn(); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); fn(); }, wait);
  };
}
const refreshDashboard = throttled(() => loadDashboard(), 3000);
const refreshMeters = throttled(() => loadMeters(), 3000);
const refreshZlans = throttled(() => loadZlans(), 3000);
const refreshConnectivity = throttled(() => loadConnectivity(), 3000);

// -- websocket --
function connectWs(){
  const proto = location.protocol==="https:"?"wss":"ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  ws.onmessage=(e)=>{
    const {channel, payload}=JSON.parse(e.data);
    if(["reading","released","attempt_failed"].includes(channel)) refreshDashboard();
    if(channel === "server_status") setBrokerChip(payload.connected);
    if(channel === "connectivity") { refreshConnectivity(); refreshMeters(); refreshZlans(); }
  };
  ws.onclose=()=>setTimeout(connectWs,1500);
}

async function boot(){
  await withBusy("Loading\u2026", async () => {
    await loadDashboard(); await loadZlans(); await loadMeters(); await loadParams();
    await loadQueue(); await loadHistory(); await loadConnectivity(); await loadOutput();
  });
  connectWs();
  const visible = fn => () => { if (!document.hidden) fn(); };       // no polling from a background tab
  setInterval(visible(loadDashboard), 5000); setInterval(visible(loadMeters), 5000); setInterval(visible(loadQueue), 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadDashboard(); loadMeters(); loadQueue(); } });
}
checkAuth();
