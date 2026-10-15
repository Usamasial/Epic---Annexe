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
document.getElementById("btn-logout").addEventListener("click", () => { document.cookie="session=;max-age=0"; location.reload(); });

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
  const r = await fetch("/api/meters/bulk-upload",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rows})});
  const d = await r.json();
  alert(`Created: ${d.created.length}. Errors: ${d.errors.length}`);
  await loadZlans(); await loadMeters();
});

// -- ZLANs --
async function loadZlans() {
  const zlans = await (await fetch("/api/zlans")).json();
  document.getElementById("zlan-rows").innerHTML = zlans.length===0 ? '<tr><td colspan="6">None yet.</td></tr>' :
    zlans.map(z=>`<tr><td>${z.zlan_ip}</td><td>${z.zlan_label}</td>
      <td><input type="number" class="interval-input" id="zlan-int-${z.zlan_ip}" value="${z.default_polling_interval}" style="width:70px">
        <button class="btn-ghost" onclick="saveZlanInterval('${z.zlan_ip}')">Save</button></td>
      <td><input type="number" class="interval-input" id="zlan-pub-${z.zlan_ip}" value="${z.default_publish_interval||''}" placeholder="(every read)" style="width:70px">
        <button class="btn-ghost" onclick="saveZlanPublishInterval('${z.zlan_ip}')">Save</button></td>
      <td>${z.port}</td>
      <td><button class="btn-delete" onclick="delZlan('${z.zlan_ip}')">Remove</button></td></tr>`).join("");
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
  const minutes = Number(document.getElementById(`zlan-int-${zlan_ip}`).value);
  await fetch(`/api/zlans/${encodeURIComponent(zlan_ip)}/interval`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({default_polling_interval:minutes})});
  await loadZlans();
}
async function saveZlanPublishInterval(zlan_ip){
  const v = document.getElementById(`zlan-pub-${zlan_ip}`).value;
  await fetch(`/api/zlans/${encodeURIComponent(zlan_ip)}/publish-interval`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({default_publish_interval:v===""?null:Number(v)})});
  await loadZlans();
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
  document.getElementById("meter-rows").innerHTML = filtered.length===0 ? '<tr><td colspan="8">None yet.</td></tr>' :
    filtered.map(m=>`<tr><td>${m.meter_code}</td><td>${m.zlan_ip}</td><td>${m.unit_id}</td>
      <td><input type="number" class="interval-input" id="meter-int-${m.meter_code}" value="${m.polling_interval||''}" placeholder="(default)" style="width:70px">
        <button class="btn-ghost" onclick="saveMeterInterval('${m.meter_code}')">Save</button></td>
      <td><input type="number" class="interval-input" id="meter-pub-${m.meter_code}" value="${m.publish_interval||''}" placeholder="(zlan default)" style="width:70px">
        <button class="btn-ghost" onclick="saveMeterPublishInterval('${m.meter_code}')">Save</button></td>
      <td><span class="pill ${m.last_status==='ok'?'released':(m.last_status==='fail'?'failed':'queued')}">${m.active?m.last_status:'inactive'}</span></td>
      <td>${m.last_poll_at||"-"}</td><td><button class="btn-delete" onclick="toggleMeter('${m.meter_code}',${!m.active})">${m.active?'Deactivate':'Activate'}</button></td></tr>`).join("");
}
document.getElementById("meter-search").addEventListener("input", loadMeters);
async function toggleMeter(code,active){ await fetch(`/api/meters/${encodeURIComponent(code)}/active`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({active})}); await loadMeters(); }
async function saveMeterInterval(code){
  const v = document.getElementById(`meter-int-${code}`).value;
  await fetch(`/api/meters/${encodeURIComponent(code)}/interval`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({polling_interval:v===""?null:Number(v)})});
  await loadMeters();
}
async function saveMeterPublishInterval(code){
  const v = document.getElementById(`meter-pub-${code}`).value;
  await fetch(`/api/meters/${encodeURIComponent(code)}/publish-interval`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({publish_interval:v===""?null:Number(v)})});
  await loadMeters();
}

// -- Parameters --
async function loadParams() {
  const ps = await (await fetch("/api/parameters")).json();
  document.getElementById("param-rows").innerHTML = ps.length===0 ? '<tr><td colspan="9">None yet.</td></tr>' :
    ps.map(p=>`<tr><td>${p.label}</td><td>${p.address}</td><td>${p.wire_code}</td><td>${p.priority}</td><td>${p.min_value??'-'}</td><td>${p.max_value??'-'}</td>
      <td><input type="number" min="1" class="interval-input" id="param-poll-${p.label}" value="${p.poll_interval||''}" placeholder="(each cycle)" style="width:115px"></td>
      <td><input type="number" min="1" class="interval-input" id="param-pub-${p.label}" value="${p.publish_interval||''}" placeholder="(meter/ZLAN)" style="width:115px">
        <button class="btn-ghost" onclick="saveParamIntervals('${p.label}')">Save</button></td>
      <td><button class="btn-delete" onclick="delParam('${p.label}')">Remove</button></td></tr>`).join("");
}
async function saveParamIntervals(label){
  const poll = document.getElementById("param-poll-"+label).value, pub = document.getElementById("param-pub-"+label).value;
  const r = await fetch("/api/parameters/"+encodeURIComponent(label)+"/intervals",{method:"PUT",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({poll_interval: poll===""?null:Number(poll), publish_interval: pub===""?null:Number(pub)})});
  const msg=document.getElementById("param-msg");
  if(!r.ok){const d=await r.json(); msg.textContent=d.detail; msg.className="msg error"; return;}
  msg.textContent="Saved "+label+" intervals."; msg.className="msg success"; await loadParams();
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
document.getElementById("btn-save-output").addEventListener("click", async () => {
  const body = {connection_type: document.getElementById("conn-type").value, server_url: document.getElementById("out-url").value,
    mqtt_broker: document.getElementById("mqtt-broker").value, mqtt_port: document.getElementById("mqtt-port").value,
    mqtt_username: document.getElementById("mqtt-username").value, mqtt_password: document.getElementById("mqtt-password").value,
    mqtt_topic_template: document.getElementById("mqtt-topic-tmpl").value, mqtt_response_topic: document.getElementById("mqtt-resp-topic").value,
    mqtt_connectivity_topic_template: document.getElementById("mqtt-conn-topic-tmpl").value,
    mqtt_response_timeout: document.getElementById("mqtt-resp-timeout").value, server_enabled: document.getElementById("out-enabled").checked?"true":"false"};
  await fetch("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const msg=document.getElementById("output-msg"); msg.textContent="Saved."; msg.className="msg success";
});
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
  await fetch("/api/config/import",{method:"POST",headers:{"Content-Type":"application/json"},body:text});
  alert("Configuration imported."); location.reload();
});
document.getElementById("btn-change-pw").addEventListener("click", async () => {
  const body = {current: document.getElementById("pw-current").value, new: document.getElementById("pw-new").value};
  const r = await fetch("/api/auth/change-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json(); const msg=document.getElementById("pw-msg");
  msg.textContent = r.ok ? "Password updated." : (d.detail||"Failed"); msg.className = r.ok?"msg success":"msg error";
});

async function restartApp(){
  if (!confirm("Restart the app process now? The dashboard will briefly disconnect and come back automatically.")) return;
  await fetch("/api/system/restart",{method:"POST"});
  alert("Restarting... reload this page in a few seconds.");
}
async function resetSettingsOnly(){
  if (!confirm("Reset all settings to their defaults? Your ZLANs, meters, and history are not affected.")) return;
  await fetch("/api/settings/reset",{method:"POST"});
  alert("Settings reset to defaults.");
  await loadOutput();
  location.reload();
}
async function factoryReset(){
  if (!confirm("This will PERMANENTLY DELETE everything -- ZLANs, meters, parameters, all history, and settings, including your login. Continue?")) return;
  const password = prompt("Re-enter your current password to confirm factory reset:");
  if (!password) return;
  const r = await fetch("/api/system/factory-reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password})});
  if (!r.ok) { const d = await r.json(); alert(d.detail || "Failed."); return; }
  alert("Factory reset complete. The app is restarting -- reload this page in a few seconds.");
}

// -- websocket --
function connectWs(){
  const proto = location.protocol==="https:"?"wss":"ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  ws.onmessage=(e)=>{
    const {channel, payload}=JSON.parse(e.data);
    if(["reading","released","attempt_failed"].includes(channel)) loadDashboard();
    if(channel === "server_status") setBrokerChip(payload.connected);
    if(channel === "connectivity") { loadConnectivity(); loadMeters(); loadZlans(); }
  };
  ws.onclose=()=>setTimeout(connectWs,1500);
}

async function boot(){
  await loadDashboard(); await loadZlans(); await loadMeters(); await loadParams();
  await loadQueue(); await loadHistory(); await loadConnectivity(); await loadOutput();
  connectWs();
  setInterval(loadDashboard, 4000); setInterval(loadMeters, 5000); setInterval(loadQueue, 4000);
}
checkAuth();
