/* ===========================================================================
   论文助手 · 四合一站点 —— 前端交互
   ---------------------------------------------------------------------------
   设计要点
   1. 密钥只存在浏览器 localStorage，随每个请求放在请求头里发给后端；
      后端只在本次请求内转交给模型服务商，不落盘、不入日志。
   2. 会话 id 也存 localStorage，用来把「资料获取 → 写作 → 评估」串成一条链路。
      服务器只把会话放在内存里，所以任何 404/session_expired 都当作「重来一次」。
   3. 不引入任何前端框架：本站只有一个页面、四个面板，原生 JS 足够且没有构建步骤。
   =========================================================================== */
'use strict';

const LS_KEY = 'thesis-suite.llm';
const LS_SID = 'thesis-suite.sid';

/* ------------------------------------------------------------------ 小工具 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function bytes(n) {
  if (n < 1024) return n + ' 字';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' 千字';
  return (n / 1024 / 1024).toFixed(2) + ' 百万字';
}

/** 把模型/服务端返回的文本渲染成极简 Markdown。只支持意见书里真正用到的语法，
 *  目的是避免为了几行表格引入一个 Markdown 库（本站没有构建步骤）。 */
function md(text) {
  const lines = String(text ?? '').split('\n');
  const out = [];
  let inTable = false, inList = false;

  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');

  const closeBlocks = () => {
    if (inTable) { out.push('</table>'); inTable = false; }
    if (inList) { out.push('</ul>'); inList = false; }
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (/^\s*$/.test(line)) { closeBlocks(); continue; }

    if (/^\s*\|.*\|\s*$/.test(line)) {
      const cells = line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;   // 分隔行
      if (inList) { out.push('</ul>'); inList = false; }
      if (!inTable) { out.push('<table>'); inTable = true; out.push('<tr>' + cells.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr>'); }
      else out.push('<tr>' + cells.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>');
      continue;
    }
    if (inTable) { out.push('</table>'); inTable = false; }

    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeBlocks();
      const level = Math.min(m[1].length, 4);
      out.push(`<h${level}>${inline(m[2])}</h${level}>`);
      continue;
    }
    if (/^\s*>\s?/.test(line)) { closeBlocks(); out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`); continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    closeBlocks();
    out.push(`<p>${inline(line)}</p>`);
  }
  closeBlocks();
  return out.join('\n');
}

function note(el, kind, html) {
  el.innerHTML = kind ? `<div class="note ${kind}">${html}</div>` : '';
}

function setProgress(barEl, msgEl, wrapEl, ratio, message) {
  wrapEl.classList.add('on');
  barEl.style.width = Math.max(0, Math.min(1, ratio)) * 100 + '%';
  if (message !== undefined) msgEl.textContent = message;
}

function hideProgress(wrapEl) { wrapEl.classList.remove('on'); }

/* ------------------------------------------------------------------ 凭据 */
const LLM = {
  key: '', provider: '', base: '', model: '', providers: {},

  load() {
    try { Object.assign(this, JSON.parse(localStorage.getItem(LS_KEY) || '{}')); } catch (_) { /* 忽略损坏值 */ }
    if (!this.key) this.key = '';
    if (!this.provider) this.provider = 'deepseek';
  },
  save() {
    localStorage.setItem(LS_KEY, JSON.stringify({
      key: this.key, provider: this.provider, base: this.base, model: this.model,
    }));
    this.paint();
  },
  headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this.key) h['X-LLM-Key'] = this.key;
    const preset = this.providers[this.provider] || {};
    const base = (this.base || '').trim() || preset.base_url || '';
    const model = (this.model || '').trim() || preset.model || '';
    if (base) h['X-LLM-Base'] = base;
    if (model) h['X-LLM-Model'] = model;
    return h;
  },
  get ready() { return !!this.key.trim(); },
  label() {
    const preset = this.providers[this.provider] || {};
    return preset.label || this.provider || '未选服务商';
  },
  paint() {
    const badge = $('keyBadge');
    badge.className = 'keybadge' + (this.ready ? ' on' : '');
    badge.textContent = this.ready ? '已配置 · ' + this.label() : '未填密钥（仅离线出题）';
  },
};

/* ------------------------------------------------------------------ 会话 */
const SID = {
  get() { return localStorage.getItem(LS_SID) || ''; },
  set(v) { if (v) localStorage.setItem(LS_SID, v); },
  clear() { localStorage.removeItem(LS_SID); },
};

/* ------------------------------------------------------------------ 请求 */
async function api(path, { method = 'GET', body = null, raw = false } = {}) {
  const opts = { method, headers: LLM.headers() };
  if (body !== null) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (raw) {
    if (!resp.ok) throw new Error(await readError(resp));
    return resp.text();
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data.detail || data.error || `HTTP ${resp.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

async function readError(resp) {
  try {
    const data = await resp.json();
    const d = data.detail || data.error || ('HTTP ' + resp.status);
    return typeof d === 'string' ? d : JSON.stringify(d);
  } catch (_) { return 'HTTP ' + resp.status; }
}

/** 下载纯文本结果。用 blob 而不是直接开链接——POST 请求没法用 a[href]。 */
function download(text, filename) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 消费 SSE 流。原生 EventSource 不支持 POST，所以用 fetch + 手写解析。 */
async function sse(path, body, onEvent) {
  const resp = await fetch(path, { method: 'POST', headers: LLM.headers(), body: JSON.stringify(body) });
  if (!resp.ok) throw new Error(await readError(resp));
  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (!chunk.startsWith('data:')) continue;
      const payload = chunk.slice(5).trim();
      if (payload === '[DONE]') return;
      try { onEvent(JSON.parse(payload)); } catch (_) { /* 忽略坏帧 */ }
    }
  }
}

/* ================================================================== 页签 */
const ASSISTANTS = [
  { id: 'topics', num: '①', name: '题目生成' },
  { id: 'gather', num: '②', name: '资料获取' },
  { id: 'writing', num: '③', name: '写作' },
  { id: 'review', num: '④', name: '评估' },
];

function buildTabs() {
  $('tabbar').innerHTML = ASSISTANTS.map((a) =>
    `<button class="tab" data-tab="${a.id}"><span class="num">${a.num}</span>${a.name}</button>`).join('');
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => selectTab(btn.dataset.tab));
  });
}

function selectTab(id) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === id));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'p-' + id));
  location.hash = id;
}

/* ================================================================== ① 题目生成 */
const Topic = { lastResult: null };

async function topicOptions() {
  const data = await api('/api/topics/options');
  LLM.providers = data.providers || {};
  $('provider').innerHTML = Object.entries(LLM.providers).map(([k, v]) =>
    `<option value="${esc(k)}">${esc(v.label)}</option>`).join('');
  $('tLevel').innerHTML = (data.levels || []).map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
  $('tLevel').value = '硕士学位论文';
  $('disciplineList').innerHTML = (data.disciplines || []).map((x) => `<option value="${esc(x)}">`).join('');
  $('tCount').max = data.max_count || 12;
}

function renderTopics(result) {
  $('topicResultCard').classList.remove('hidden');
  const offline = result.mode === 'offline';
  $('topicStat').innerHTML = [
    ['题目数', result.count],
    ['学科', esc(result.discipline || '—')],
    ['生成方式', offline ? '离线知识库' : esc(result.model || '模型')],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  if (result.notice) note($('topicNote'), result.notice_kind === 'warning' ? 'warn' : 'ok', esc(result.notice));

  $('topicList').innerHTML = (result.topics || []).map((t) => {
    const tags = [];
    if (t.type) tags.push(`<span class="tag">${esc(t.type)}</span>`);
    if (t.methodology) tags.push(`<span class="tag">${esc(t.methodology)}</span>`);
    if (t.difficulty) tags.push(`<span class="tag">难度 ${esc(t.difficulty)}</span>`);
    if (t.brief_from_model) tags.push('<span class="tag ok">简报完整</span>');
    else tags.push('<span class="tag warn">简报为占位</span>');
    return `<div class="topic">
      <h3><span class="idx">${t.id ?? ''}</span>${esc(t.title || '')}</h3>
      ${t.brief ? `<div class="brief">${esc(t.brief)}</div>` : ''}
      ${tags.length ? `<div class="tags">${tags.join('')}</div>` : ''}
    </div>`;
  }).join('');
}

async function runTopics() {
  const discipline = $('tDiscipline').value.trim();
  if (!discipline) { note($('topicNote'), 'err', '请先填写学科 / 专业。'); return; }
  const btn = $('btnTopic');
  btn.disabled = true; btn.textContent = '生成中…';
  note($('topicNote'), '', '');
  $('topicResultCard').classList.add('hidden');
  setProgress($('topicBar'), $('topicMsg'), $('topicProg'), 0.02, '已连接，正在准备…');

  const body = {
    discipline,
    direction: $('tDirection').value.trim(),
    level: $('tLevel').value,
    count: parseInt($('tCount').value, 10) || 6,
    methodology: $('tMethod').value.trim(),
    keywords: $('tKeywords').value.trim(),
    extra: $('tExtra').value.trim(),
    provider: LLM.provider,
    mode: 'auto',
  };

  try {
    await sse('/api/topics/generate', body, (ev) => {
      if (ev.type === 'stage') {
        setProgress($('topicBar'), $('topicMsg'), $('topicProg'), ev.progress || 0, ev.message || '');
        if (ev.stage === 'fallback') note($('topicNote'), 'warn', esc(ev.message));
      } else if (ev.type === 'result') {
        Topic.lastResult = ev.result;
        renderTopics(ev.result);
        setProgress($('topicBar'), $('topicMsg'), $('topicProg'), 1, '完成');
        $('btnTopicExport').disabled = false;
        setTimeout(() => hideProgress($('topicProg')), 1200);
      } else if (ev.type === 'error') {
        note($('topicNote'), 'err', esc(ev.message));
      }
    });
  } catch (err) {
    note($('topicNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '开始生成';
  }
}

async function exportTopics() {
  if (!Topic.lastResult) return;
  try {
    const text = await api('/api/topics/export', { method: 'POST', body: { result: Topic.lastResult }, raw: true });
    download(text, `选题-${Topic.lastResult.discipline || '未命名'}.md`);
  } catch (err) { note($('topicNote'), 'err', esc(err.message)); }
}

/* ================================================================== ② 资料获取 */
const Gather = { items: [], sid: '', imported: [] };

/* ---- 文件导入 ----
   解析在后端完成，**不需要 API Key**，所以这里用 fetch + FormData 直接传，
   不走 api() 那个 JSON 封装（它会给请求加 Content-Type: application/json，
   那会让 multipart 的 boundary 失效）。 */
async function importFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const zone = $('gDrop');
  const btn = $('btnExtract');
  zone.classList.add('busy');
  btn.disabled = true;
  note($('gatherNote'), '', '');
  setProgress($('gatherBar'), $('gatherMsg'), $('gatherProg'), 0.25,
    `正在解析 ${files.length} 个文件…`);

  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));

  try {
    const resp = await fetch('/api/gather/import', { method: 'POST', body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));

    Gather.imported = data.sources || [];
    renderImported(data, files);

    // 把解析出的正文追加到文本区，这样访客能接着编辑，也能看到解析结果长什么样。
    // 用 `---` 分隔，与原有的手动粘贴格式一致，抽取时走同一套切分逻辑。
    const blocks = (data.sources || [])
      .map((s) => `${s.title || '未命名'}\n${s.text || ''}`.trim())
      .filter(Boolean);
    if (blocks.length) {
      const prev = $('gSources').value.trim();
      $('gSources').value = (prev ? prev + '\n\n---\n\n' : '') + blocks.join('\n\n---\n\n');
    }

    const failed = data.failed || 0;
    const warn = (data.files || []).filter((f) => f.ok && f.warning);
    let kind = failed ? 'warn' : 'ok';
    let msg = `已解析 ${data.parsed} 个文件，生成 ${data.source_count} 段可抽取文本。`;
    if (failed) msg += ` ${failed} 个文件未能解析（见下表）。`;
    if (warn.length) msg += ` ${warn.length} 个文件有提示。`;
    msg += ' 现在点「抽取证据」把它们变成可引用的证据。';
    note($('gatherNote'), kind, msg);
    setProgress($('gatherBar'), $('gatherMsg'), $('gatherProg'), 0.6, '解析完成');
    setTimeout(() => hideProgress($('gatherProg')), 1200);
  } catch (err) {
    note($('gatherNote'), 'err', '导入失败：' + esc(err.message));
    hideProgress($('gatherProg'));
  } finally {
    zone.classList.remove('busy');
    btn.disabled = false;
    $('gFiles').value = '';        // 允许重复选择同一个文件
  }
}

function renderImported(data, files) {
  const el = $('gFileList');
  const rows = data.files || [];
  if (!rows.length) { el.innerHTML = ''; return; }

  const totalBytes = data.total_bytes || 0;
  el.innerHTML = `<div class="stat" style="margin-top:14px">
      <div>收到文件<b>${data.received}</b></div>
      <div>解析成功<b>${data.parsed}</b></div>
      <div>失败<b>${data.failed}</b></div>
      <div>可抽取段落<b>${data.source_count}</b></div>
      <div>合计大小<b>${(totalBytes / 1024).toFixed(0)} KB</b></div>
    </div>
    <table class="list">
      <tr><th>文件</th><th style="width:88px">类型</th><th style="width:74px">字数</th>
          <th style="width:60px">页数</th><th>状态</th></tr>
      ${rows.map((f) => {
        let cls = '', text = '就绪';
        if (f.error) { cls = 'bad'; text = esc(f.error); }
        else if (f.warning) { cls = 'warn'; text = esc(f.warning); }
        else if (!f.ok) { cls = 'warn'; text = '没有取到文字'; }
        const kindCls = f.error ? 'bad' : (f.warning ? 'warn' : '');
        return `<tr>
          <td><strong>${esc(f.filename)}</strong>
              <div style="color:var(--ink-3);font-size:12px">${esc(String(f.preview || '').slice(0, 70))}</div></td>
          <td><span class="fkind ${kindCls}">${esc(f.kind || '?')}</span></td>
          <td>${f.chars || 0}${f.truncated ? ' <span class="fkind warn">截断</span>' : ''}</td>
          <td>${f.pages || '—'}</td>
          <td style="font-size:12.5px">${text}</td>
        </tr>`;
      }).join('')}
    </table>`;
}

function bindDropzone() {
  const zone = $('gDrop');
  const input = $('gFiles');

  input.addEventListener('change', (e) => importFiles(e.target.files));

  // dragover 必须 preventDefault，否则浏览器会直接打开文件、页面跳走。
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) importFiles(e.dataTransfer.files);
  });

  // 页面其他地方误拖入文件时不要让浏览器跳走
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());
}

function renderGather(items) {
  Gather.items = items || [];
  const card = $('gatherResultCard');
  if (!Gather.items.length) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const verified = Gather.items.filter((i) => i.verified === 'confirmed').length;
  const suspect = Gather.items.filter((i) => i.verified && i.verified !== 'confirmed').length;
  $('gatherStat').innerHTML = [
    ['证据条数', Gather.items.length],
    ['已核实', verified],
    ['存疑/查无', suspect],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  $('gatherList').innerHTML = `<table class="list">
    <tr><th style="width:34px">#</th><th>题名 / 来源</th><th style="width:74px">年份</th><th style="width:110px">核验</th><th>原文片段</th></tr>
    ${Gather.items.map((it, i) => {
      const verdict = it.verified
        ? `<span class="tag ${it.verified === 'confirmed' ? 'ok' : 'bad'}">${esc(it.verified)}</span>`
        : '<span class="tag">未核验</span>';
      const passages = (it.passages || []).slice(0, 2).map((p) => esc(String(p).slice(0, 90))).join('<br>');
      return `<tr>
        <td>${i + 1}</td>
        <td><strong>${esc(it.title || '（未识别出题名）')}</strong>
            <div style="color:var(--ink-3);font-size:12px">${esc(it.source || '')} ${it.doi ? '· ' + esc(it.doi) : ''}</div></td>
        <td>${esc(it.year || '')}</td>
        <td>${verdict}</td>
        <td style="font-size:12px;color:var(--ink-2)">${passages || '—'}</td>
      </tr>`;
    }).join('')}
  </table>`;

  $('btnVerify').disabled = false;
  $('btnGatherClear').disabled = false;
}

/** 按一行 `---` 切分用户粘贴的资料块。 */
function splitSources(text) {
  return text.split(/^\s*-{3,}\s*$/m)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => {
      const doi = (chunk.match(/10\.\d{4,9}\/[^\s，。；)）]+/) || [''])[0];
      const titleLine = chunk.split('\n')[0].trim();
      return { text: chunk, doi, title: titleLine.length <= 120 ? titleLine : '' };
    });
}

async function runExtract() {
  const raw = $('gSources').value.trim();
  if (!raw) { note($('gatherNote'), 'err', '请先粘贴资料内容。'); return; }
  if (!LLM.ready) { note($('gatherNote'), 'err', '抽取证据需要模型：请先在右上角填入你自己的 API Key。'); return; }

  const sources = splitSources(raw);
  const btn = $('btnExtract');
  btn.disabled = true; btn.textContent = '抽取中…';
  note($('gatherNote'), '', '');
  setProgress($('gatherBar'), $('gatherMsg'), $('gatherProg'), 0.1, `正在处理 ${sources.length} 份资料…`);

  try {
    const data = await api('/api/gather/extract', {
      method: 'POST', body: { sid: SID.get(), sources },
    });
    SID.set(data.sid); Gather.sid = data.sid;
    const dropped = data.dropped_passages || 0;
    let kind = 'ok';
    let msg = `已入池 ${data.added} 条证据（共 ${data.total} 条）。`;
    if (dropped) msg += ` 其中 <strong>${dropped} 条"原文片段"经逐字校验不通过已被丢弃</strong>——它们很可能是模型编造的。`;
    if ((data.errors || []).length) {
      kind = 'warn';
      msg += ' 失败：' + data.errors.map((e) => esc(e.title || '') + '（' + esc(e.error) + '）').join('；');
    }
    note($('gatherNote'), kind, msg);
    renderGather(data.items);
    setProgress($('gatherBar'), $('gatherMsg'), $('gatherProg'), 1, '完成');
    setTimeout(() => hideProgress($('gatherProg')), 1200);
  } catch (err) {
    note($('gatherNote'), 'err', esc(err.message));
    hideProgress($('gatherProg'));
  } finally {
    btn.disabled = false; btn.textContent = '抽取证据';
  }
}

async function runVerify() {
  if (!LLM.ready) { note($('gatherNote'), 'err', '抽取证据需要模型：请先填入 API Key。'); return; }
  const sid = SID.get();
  if (!sid) { note($('gatherNote'), 'err', '还没有证据库，请先抽取证据。'); return; }
  const btn = $('btnVerify');
  btn.disabled = true; btn.textContent = '核验中…';
  setProgress($('gatherBar'), $('gatherMsg'), $('gatherProg'), 0.3, '正在向 Crossref 核对 DOI/标题…');
  try {
    const data = await api('/api/gather/verify', { method: 'POST', body: { sid, limit: 15 } });
    const tally = Object.entries(data.tally || {}).map(([k, v]) => `${k} ${v}`).join('、');
    note($('gatherNote'), 'ok', `核验完成：${esc(tally || '无结果')}。<br>说明：<code>confirmed</code> 表示数据库里找到了对应文献；<code>not_found</code> 高度可疑；<code>inconclusive</code> 是网络原因没能确认，<strong>不等于通过</strong>。`);
    await refreshGather();
  } catch (err) {
    note($('gatherNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '核验文献真伪';
    hideProgress($('gatherProg'));
  }
}

async function refreshGather() {
  const sid = SID.get();
  const data = await api('/api/gather/status' + (sid ? '?sid=' + encodeURIComponent(sid) : ''));
  renderGather(data.items);
}

async function exportGather(format) {
  const sid = SID.get();
  if (!sid) return;
  try {
    const text = await api('/api/gather/export', { method: 'POST', body: { sid, format }, raw: true });
    download(text, `参考文献-${format}.${format === 'bibtex' ? 'bib' : 'md'}`);
  } catch (err) { note($('gatherNote'), 'err', esc(err.message)); }
}

async function clearGather() {
  const sid = SID.get();
  if (!sid) return;
  if (!confirm('确定清空服务器上这个会话的证据库？（不可恢复）')) return;
  try {
    const data = await api('/api/gather/evidence?sid=' + encodeURIComponent(sid), { method: 'DELETE' });
    note($('gatherNote'), 'ok', `已清空 ${data.removed} 条证据。`);
    renderGather([]);
    $('btnVerify').disabled = true;
    $('btnGatherClear').disabled = true;
  } catch (err) { note($('gatherNote'), 'err', esc(err.message)); }
}

/* ================================================================== ③ 写作 */
const Write = { draft: null };

async function writingOptions() {
  const data = await api('/api/writing/options');
  $('wGenre').innerHTML = (data.genres || []).map((g) =>
    `<option value="${esc(g.id)}" data-sections="${esc((g.sections || []).map((s) => s.title).join('\n'))}">${esc(g.name)}</option>`).join('');
  applyGenreSections();
}

function applyGenreSections() {
  const opt = $('wGenre').selectedOptions[0];
  if (opt && !$('wSections').value.trim()) {
    $('wSections').value = opt.dataset.sections || '';
  }
}

async function runDraft() {
  const title = $('wTitle').value.trim();
  if (!title) { note($('writeNote'), 'err', '请先填写论文题目。'); return; }
  if (!LLM.ready) { note($('writeNote'), 'err', '写作需要模型：请先在右上角填入你自己的 API Key。'); return; }

  const btn = $('btnDraft');
  btn.disabled = true; btn.textContent = '生成中…';
  note($('writeNote'), '', '');
  $('writeResultCard').classList.add('hidden');
  setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 0.02, '正在检查证据库…');

  const sections = $('wSections').value.split('\n').map((s) => s.trim()).filter(Boolean);
  const body = {
    sid: SID.get(), title, genre: $('wGenre').value, sections, provider: LLM.provider, mode: 'auto',
    use_web: $('wUseWeb').checked,
  };

  try {
    await sse('/api/writing/draft', body, (ev) => {
      if (ev.type === 'stage') {
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), ev.progress || 0, ev.message || '');
        // 联网检索与降级提示要显眼：前者说明依据从哪来，后者说明依据变薄了。
        if (ev.stage === 'warn') note($('writeNote'), 'warn', esc(ev.message));
        else if (ev.stage === 'search') note($('writeNote'), '', esc(ev.message));
      } else if (ev.type === 'section') {
        renderDraft(ev.partial || { title, sections: ev.sections || [] });
      } else if (ev.type === 'result') {
        SID.set(ev.sid); Write.draft = ev.draft;
        renderDraft(ev.draft, ev.coverage, ev.notice);
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 1, '完成');
        $('btnDraftExport').disabled = false;
        const cov = ev.coverage || {};
        // 编造引用与无依据数字必须用醒目的提示，不能只塞在覆盖度面板里。
        if (cov.citations_invalid || cov.ungrounded_numbers) {
          note($('writeNote'), 'warn',
            `生成完成，但有 <strong>${cov.citations_invalid || 0} 处编造的引用编号</strong>、` +
            `<strong>${cov.ungrounded_numbers || 0} 处找不到依据的数字</strong>，` +
            '已在稿件上方逐章标出，请务必人工核对。');
        } else {
          note($('writeNote'), 'ok', '生成完成。');
        }
        setTimeout(() => hideProgress($('writeProg')), 1200);
      } else if (ev.type === 'error') {
        note($('writeNote'), 'err', esc(ev.message));
      }
    });
  } catch (err) {
    note($('writeNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '逐章生成';
  }
}

/* 章节标题用单个 '#'：后端把草稿拼成待检文本时依赖这个层级来切分章节，
   「正文数字是否有依据」那条检查才跑得起来。这里与后端保持一致。 */
const sectionHeading = (text) => `<h1>${esc(text || '')}</h1>`;

/* 依据覆盖度面板。
   这是本次改动最关键的可解释性部分：放宽了写作依据之后，最大的风险不是"写不出来"，
   而是**作者以为每一句都有依据**。所以把"用了多少 A 层、多少 B 层、有没有编造引用、
   有没有找不到依据的数字"直接摆在稿件上方，而不是埋在日志里。 */
function renderCoverage(cov, notice) {
  const el = $('writeCoverage');
  if (!cov) { el.innerHTML = ''; return; }
  const bad = (cov.citations_invalid || 0) + (cov.ungrounded_numbers || 0);
  const head = bad
    ? `<div class="note warn"><strong>有 ${bad} 处需要人工核对</strong>——见下方明细与生成提示。</div>`
    : `<div class="note ok">引用编号与数字都能在依据池里找到，未发现编造痕迹。</div>`;

  const rows = [
    ['引用总数', cov.citations_total ?? 0],
    ['A 你的资料', cov.citations_user ?? 0],
    ['B 联网文献', cov.citations_web ?? 0],
    ['编造的引用编号', cov.citations_invalid ?? 0],
    ['找不到依据的数字', cov.ungrounded_numbers ?? 0],
    ['检索到的真实文献', cov.web_records_retrieved ?? 0],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  const sources = (cov.web_sources || []).join('、') || '无';
  el.innerHTML = head +
    `<div class="stat">${rows}</div>` +
    `<div style="font-size:13px;color:var(--ink-2);margin-bottom:6px">
       依据构成：<strong>${esc(cov.verdict || '未知')}</strong>
       ${cov.web_records_retrieved ? `（检索源：${esc(sources)}）` : ''}
     </div>` +
    (notice ? `<div class="note">${esc(notice)}</div>` : '');
}

function renderDraft(draft, coverage, notice) {
  if (!draft) return;
  $('writeResultCard').classList.remove('hidden');
  const sections = draft.sections || [];
  const total = sections.reduce((n, s) => n + String(s.body || '').length, 0);
  $('writeStat').innerHTML = [
    ['章节数', sections.length],
    ['正文字数', total],
    ['引用编号', draft.citation_count ?? '—'],
    ['联网文献', draft.web_records_total ?? 0],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  renderCoverage(coverage || draft.coverage, notice || draft.notice);

  $('writeBody').innerHTML = sectionHeading(draft.title) +
    sections.map((s) => headingWithCite(s) + `<div>${esc(s.body || '')}</div>`).join('');
}

/* 每章标题后面挂一个小徽标，显示这一章各自用了多少依据。
   整篇的覆盖度看不出"哪一章是凭记忆写的"，逐章标出来才用得上。 */
function headingWithCite(section) {
  const c = section.citations;
  if (!c) return sectionHeading(section.heading);
  const bits = [];
  if (c.user) bits.push(`资料 ${c.user}`);
  if (c.web) bits.push(`联网 ${c.web}`);
  if (!bits.length) bits.push('无引用依据');
  const cls = c.invalid ? 'bad' : (c.total ? 'ok' : 'warn');
  return sectionHeading(section.heading) +
    `<div class="tags"><span class="tag ${cls}">${esc(bits.join(' · '))}</span>` +
    (c.invalid ? `<span class="tag bad">编造引用 ${c.invalid}</span>` : '') +
    ((section.grounding && section.grounding.ungrounded_count)
      ? `<span class="tag warn">无依据数字 ${section.grounding.ungrounded_count}</span>` : '') +
    `</div>`;
}

async function exportDraft() {
  const sid = SID.get();
  if (!sid) return;
  try {
    const text = await api('/api/writing/export', { method: 'POST', body: { sid }, raw: true });
    download(text, `${($('wTitle').value || '稿件').trim()}.md`);
  } catch (err) { note($('writeNote'), 'err', esc(err.message)); }
}

/* ================================================================== ④ 评估 */
const Review = { markdown: '', checkData: null };

async function runCheck() {
  const btn = $('btnCheck');
  btn.disabled = true; btn.textContent = '检查中…';
  note($('reviewNote'), '', '');
  setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 0.4, '正在跑本地检查器…');
  try {
    const data = await api('/api/review/check', {
      method: 'POST',
      body: { text: $('rText').value, sid: SID.get(), title: $('wTitle').value.trim() || '提交的稿件' },
    });
    Review.checkData = data;
    Review.markdown = data.markdown || '';
    renderCheck(data);
    setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 1, '完成');
    setTimeout(() => hideProgress($('reviewProg')), 1200);
    $('btnOpinion').disabled = !LLM.ready;
    $('btnReviewExport').disabled = !Review.markdown;
  } catch (err) {
    note($('reviewNote'), 'err', esc(err.message));
    hideProgress($('reviewProg'));
  } finally {
    btn.disabled = false; btn.textContent = '跑本地检查';
  }
}

const KIND_LABEL = { CRITICAL: '致命', MAJOR: '重要', MINOR: '轻微', INFO: '提示' };

function renderCheck(data) {
  $('reviewResultCard').classList.remove('hidden');
  const counts = data.counts || {};
  $('reviewStat').innerHTML = [
    ['评分', data.score],
    ['问题总数', data.issue_count],
    ['致命', counts.CRITICAL || 0],
    ['重要', counts.MAJOR || 0],
    ['轻微', counts.MINOR || 0],
    ['检查器', data.checked + ' 项'],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  const failed = data.failed_checkers || [];
  if (failed.length) {
    note($('reviewNote'), 'warn', `有 ${failed.length} 个检查器执行出错（其余结果仍然有效）：` +
      failed.map((f) => esc(f.name)).join('、'));
  } else {
    note($('reviewNote'), '', '');
  }

  const dims = data.dimensions || {};
  const blocks = Object.entries(dims).filter(([, list]) => list.length).map(([name, list]) => `
    <h3 style="margin:16px 0 6px;font-size:14.5px">${esc(name)} <span style="color:var(--ink-3);font-weight:400">${list.length} 条</span></h3>
    <table class="list">
      <tr><th style="width:90px">严重度</th><th>问题</th><th style="width:34%">修改建议</th></tr>
      ${list.map((f) => `<tr>
        <td><span class="tag ${f.kind === 'CRITICAL' ? 'bad' : (f.kind === 'MAJOR' ? 'warn' : '')}">${esc(KIND_LABEL[f.kind] || f.kind || '')}</span></td>
        <td>${esc(f.message)}${f.evidence ? `<div style="color:var(--ink-3);font-size:12px;margin-top:3px">原文：${esc(String(f.evidence).slice(0, 120))}</div>` : ''}</td>
        <td style="font-size:12.5px;color:var(--ink-2)">${esc(f.fix_hint || '—')}</td>
      </tr>`).join('')}
    </table>`).join('');

  $('reviewDims').innerHTML = blocks || '<div class="note">没有发现问题。</div>';

  const byChecker = (data.by_checker || []).filter((c) => c.count > 0);
  if (byChecker.length) {
    $('reviewDims').innerHTML += `<details class="adv"><summary>按检查器看命中分布（${byChecker.length} 项有命中）</summary>
      <div style="margin-top:10px"><table class="list">
      <tr><th>检查器</th><th style="width:70px">命中</th></tr>
      ${byChecker.map((c) => `<tr><td class="mono" style="font-size:12px">${esc(c.name)}</td><td>${c.count}</td></tr>`).join('')}
      </table></div></details>`;
  }

  $('reviewRaw').innerHTML = md(data.markdown || '');
  $('reviewRawWrap').classList.remove('hidden');
}

async function runOpinion() {
  const btn = $('btnOpinion');
  btn.disabled = true; btn.textContent = '撰写中…';
  setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 0.6, '模型正在依据检查结果写审稿意见…');
  try {
    const data = await api('/api/review/opinion', {
      method: 'POST',
      body: { text: $('rText').value, sid: SID.get(), title: $('wTitle').value.trim() || '提交的稿件' },
    });
    Review.markdown = data.markdown || Review.markdown;
    $('reviewRaw').innerHTML = md(Review.markdown);
    $('reviewRawWrap').classList.remove('hidden');
    note($('reviewNote'), 'ok', `审稿意见已生成（模型：${esc(data.model || '')}）。注意：<strong>评分 ${data.score} 仍由机器检查结果推导，模型无权改动</strong>。`);
    $('btnReviewExport').disabled = false;
  } catch (err) {
    note($('reviewNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '生成审稿意见（需 Key）';
    hideProgress($('reviewProg'));
  }
}

async function runVerifyRefs() {
  const btn = $('btnVerifyRefs');
  btn.disabled = true; btn.textContent = '核验中…';
  setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 0.4, '正在向 Crossref / OpenAlex 核对…');
  try {
    const data = await api('/api/review/verify', {
      method: 'POST', body: { text: $('rText').value, sid: SID.get(), limit: 15 },
    });
    $('verifyResultCard').classList.remove('hidden');
    $('verifyStat').innerHTML = [
      ['识别条目', data.total_entries],
      ['本次核验', data.checked],
      ['未核验', data.truncated],
    ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

    const label = { confirmed: '已确认真实', not_found: '查无此文（高度可疑）', weak: '弱匹配', inconclusive: '无法确认' };
    $('verifyList').innerHTML = `<table class="list">
      <tr><th style="width:120px">结论</th><th>条目</th></tr>
      ${(data.items || []).map((it) => `<tr>
        <td><span class="tag ${it.verdict === 'confirmed' ? 'ok' : (it.verdict === 'not_found' ? 'bad' : 'warn')}">${esc(label[it.verdict] || it.verdict)}</span></td>
        <td style="font-size:12.5px">${esc(it.raw)}${it.best && it.best.title ? `<div style="color:var(--ink-3);margin-top:3px">库中匹配：${esc(it.best.title)}${it.best.year ? '（' + esc(it.best.year) + '）' : ''}</div>` : ''}</td>
      </tr>`).join('')}
    </table>`;
    note($('reviewNote'), 'ok', `核验完成。TL.DR：<code>not_found</code> 的条目很可能是 AI 编造的，务必逐条回查。`);
  } catch (err) {
    note($('reviewNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '核验参考文献真伪';
    hideProgress($('reviewProg'));
  }
}

/* ================================================================== 启动 */
async function showCapabilities() {
  const data = await api('/api/capabilities');
  const lines = (data.assistants || []).map((a) =>
    `${a.name}：${a.summary}\n    步骤：${(a.steps || []).join(' → ')}\n    需密钥：${a.needs_key ? '是' : '否'}${a.offline ? '（可离线）' : ''}\n    ${a.note}`).join('\n\n');
  alert('四个助手的能力说明\n\n' + lines);
}

async function boot() {
  buildTabs();
  const hash = location.hash.replace('#', '');
  selectTab(ASSISTANTS.some((a) => a.id === hash) ? hash : 'topics');

  bindDropzone();

  LLM.load();
  try {
    await topicOptions();
  } catch (err) {
    note($('topicNote'), 'err', '无法读取服务配置：' + esc(err.message));
  }
  $('provider').value = LLM.provider;
  LLM.paint();
  try { await writingOptions(); } catch (_) { /* 写作选项失败不阻塞其它页签 */ }

  // 恢复已有会话的证据库
  if (SID.get()) { try { await refreshGather(); } catch (_) { SID.clear(); } }

  const health = await api('/api/health').catch(() => null);
  if (health && health.packages) {
    const bad = Object.entries(health.packages).filter(([, v]) => !v.ok);
    if (bad.length) note($('topicNote'), 'err', '服务端装配异常：' + bad.map(([k, v]) => esc(k) + ' ' + esc(v.error)).join('；'));
  }
}

/* ---- 事件绑定 ---- */
$('apiKey').addEventListener('input', (e) => { LLM.key = e.target.value.trim(); LLM.save(); });
$('provider').addEventListener('change', (e) => { LLM.provider = e.target.value; LLM.base = ''; LLM.model = ''; LLM.save(); });
$('btnDocs').addEventListener('click', (e) => { e.preventDefault(); showCapabilities().catch(() => {}); });

$('btnTopic').addEventListener('click', runTopics);
$('btnTopicExport').addEventListener('click', exportTopics);
$('btnTopicClear').addEventListener('click', () => {
  Topic.lastResult = null;
  $('topicResultCard').classList.add('hidden');
  note($('topicNote'), '', '');
});

$('btnExtract').addEventListener('click', runExtract);
$('btnVerify').addEventListener('click', runVerify);
$('btnGatherClear').addEventListener('click', clearGather);
$('btnExportGbt').addEventListener('click', () => exportGather('gbt'));
$('btnExportBib').addEventListener('click', () => exportGather('bibtex'));
$('btnExportMd').addEventListener('click', () => exportGather('markdown'));

$('wGenre').addEventListener('change', () => { $('wSections').value = ''; applyGenreSections(); });
$('btnDraft').addEventListener('click', runDraft);
$('btnDraftExport').addEventListener('click', exportDraft);

$('btnCheck').addEventListener('click', runCheck);
$('btnVerifyRefs').addEventListener('click', runVerifyRefs);
$('btnOpinion').addEventListener('click', runOpinion);
$('btnReviewExport').addEventListener('click', () => {
  if (Review.markdown) download(Review.markdown, '审稿意见书.md');
});

// 从「题目生成」一键带到「写作」：题目是链路的起点，手动复制很烦。
document.addEventListener('click', (e) => {
  const h = e.target.closest('.topic h3');
  if (!h) return;
  const title = h.textContent.replace(/^\s*\d+\s*/, '').trim();
  if (!title) return;
  $('wTitle').value = title;
  selectTab('writing');
  note($('writeNote'), 'ok', '已把题目带到写作页签。请先在「资料获取」录入资料，写作只依据证据库。');
});

boot();
