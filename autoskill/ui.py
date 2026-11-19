"""Web 调试面板（单文件、零构建、原生 JS）：技能池 / 全链路试玩 / Agent 对话。"""

UI_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>AutoSkill 控制台</title>
<style>
  :root { --bg:#0f1420; --card:#171e2e; --fg:#dbe2f0; --dim:#8b97ad; --accent:#4f8cff; --ok:#3fb27f; --bad:#e2606c; }
  body { background:var(--bg); color:var(--fg); font-family:"Microsoft YaHei",system-ui,sans-serif; margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--dim); font-size:12px; margin-bottom:20px; }
  .card { background:var(--card); border-radius:10px; padding:16px; margin-bottom:16px; }
  h2 { font-size:14px; margin:0 0 10px; color:var(--accent); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #232c40; }
  .badge { padding:1px 8px; border-radius:10px; font-size:11px; }
  .active { background:#1d3a2f; color:var(--ok); } .quarantined { background:#3a1d22; color:var(--bad); }
  textarea, input { width:100%; box-sizing:border-box; background:#0c111c; color:var(--fg); border:1px solid #2a3550;
    border-radius:6px; padding:8px; font-size:13px; font-family:inherit; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; }
  button:disabled { opacity:.5; }
  pre { background:#0c111c; border-radius:6px; padding:10px; font-size:12px; overflow:auto; max-height:260px; white-space:pre-wrap; }
  .row { display:flex; gap:10px; align-items:flex-start; }
  .row > * { flex:1; }
  #health { font-size:12px; color:var(--dim); }
  .msg { border-left:3px solid var(--accent); padding:6px 10px; margin:6px 0; font-size:13px; }
  .banner { background:#3a2d1d; color:#ffd08a; padding:8px 14px; border-radius:8px; margin-bottom:14px; font-size:13px; }
</style>
</head>
<body>
<!--SHOWCASE_BANNER-->
<h1>AutoSkill 控制台</h1>
<div class="sub" id="health">加载中…</div>

<div class="card">
  <h2>技能池（每 10s 自动刷新）</h2>
  <table id="skills"><thead><tr><th>名称</th><th>版本</th><th>状态</th><th>描述</th></tr></thead><tbody></tbody></table>
</div>

<div class="card">
  <h2>全链路试玩（召回 / 自动生成）</h2>
  <textarea id="req" rows="3" placeholder="输入自然语言技能需求，例如：生成一个技能：计算字符串的 UTF-8 编码字节数"></textarea>
  <div style="margin-top:8px"><button id="go" onclick="ensure()">执行 ensure</button> <span id="ensState" class="sub"></span></div>
  <pre id="ensOut" hidden></pre>
</div>

<div class="card">
  <h2>Agent 对话</h2>
  <div class="row"><input id="chatIn" placeholder="例如：把字符串 ab 重复 3 次是多少？" onkeydown="if(event.key==='Enter')chat()"><button onclick="chat()">发送</button></div>
  <div id="chatOut"></div>
</div>

<script>
const $ = id => document.getElementById(id);
async function refresh() {
  try {
    const h = await (await fetch('/health')).json();
    $('health').textContent = `v${h.version} | 模型 ${h.llm_model} | 技能 ${h.skills} | ${h.executor} | ${h.store}`;
    const skills = await (await fetch('/skills')).json();
    const tb = $('skills').querySelector('tbody');
    tb.innerHTML = skills.map(s =>
      `<tr><td>${s.name}</td><td>v${s.version}</td><td><span class="badge ${s.status}">${s.status}</span></td><td>${s.description}</td></tr>`
    ).join('') || '<tr><td colspan="4">（空池——执行一次 ensure 试试）</td></tr>';
  } catch (e) { $('health').textContent = '服务不可达: ' + e; }
}
async function ensure() {
  const req = $('req').value.trim(); if (!req) return;
  $('go').disabled = true; $('ensState').textContent = '执行中（生成约需 8~60s）…'; $('ensOut').hidden = true;
  try {
    const r = await (await fetch('/ensure', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({requirement:req})})).json();
    $('ensOut').hidden = false;
    $('ensOut').textContent = JSON.stringify(r, null, 2);
    $('ensState').textContent = r.ok ? '✅ 成功' : '❌ ' + (r.error || '').slice(0, 80);
    refresh();
  } finally { $('go').disabled = false; }
}
async function chat() {
  const msg = $('chatIn').value.trim(); if (!msg) return;
  $('chatIn').value = '';
  const div = document.createElement('div'); div.className = 'msg'; div.textContent = '🧑 ' + msg;
  $('chatOut').prepend(div);
  try {
    const r = await (await fetch('/agent/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:msg})})).json();
    const out = document.createElement('div'); out.className = 'msg';
    const steps = (r.steps || []).map(s => `${s.skill}:${s.ok ? '✅' : '❌'}`).join(' → ');
    out.textContent = `🤖 ${r.content}${steps ? '（工具: ' + steps + '）' : ''}`;
    $('chatOut').prepend(out); refresh();
  } catch (e) {
    const out = document.createElement('div'); out.className = 'msg'; out.textContent = '⚠️ ' + e;
    $('chatOut').prepend(out);
  }
}
refresh(); setInterval(refresh, 10000);
</script>
</body>
</html>
"""
