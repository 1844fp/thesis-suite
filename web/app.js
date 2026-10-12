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

/* ================================================================ 阶段工作流
   界面参考 ThesisMind（thesismindai.com）的组织方式：论文写作不是"四个并列的工具"，
   而是**一件事的五个阶段**，左边走阶段、右边做当前这一步，数据跨阶段留着。

   这里刻意把两件事分开：
     · **stage** 是给用户看的"阶段"（定题目 / 开题报告 / …），决定左侧导航；
     · **panel** 是实际承载界面的容器（p-topics / p-gather / …），决定右侧显示哪一块。
   一个阶段可以对应一个 panel，将来也可以几个阶段共用一个 panel，
   所以不把两者混成一个概念。 */
const STAGES = [
  { id: 'topic',    n: '1', name: '定题目',   desc: '先有一个能写下去的题', panel: 'topics' },
  { id: 'proposal', n: '2', name: '开题报告', desc: '把文献与依据找齐',     panel: 'gather' },
  { id: 'draft',    n: '3', name: '写初稿',   desc: '逐章生成正文',         panel: 'writing' },
  { id: 'revise',   n: '4', name: '修改与评估', desc: '查问题、出意见书',   panel: 'review' },
  { id: 'defense',  n: '5', name: '答辩材料', desc: '答辩稿与演示提纲',     panel: 'review' },
];

let CURRENT_STAGE = 'topic';

/** 阶段是否可用。答辩材料尚未实现——明确禁用，而不是让用户点进去看到空白。 */
function stageEnabled(id) {
  return id !== 'defense';
}

function buildStages() {
  const bar = $('tabbar');
  if (!bar) return;

  // 阶段按钮可能**已经在 HTML 里静态写好**（布局与文案放在 HTML 更直观，也利于
  // 首屏不闪），也可能需要这里渲染。两种都支持：有就不重复造，没有才补。
  // 早先这里无条件 innerHTML 覆盖，会把 HTML 里写好的按钮连同结构一起冲掉。
  let buttons = Array.from(bar.querySelectorAll('.stage'));

  if (!buttons.length) {
    bar.innerHTML = STAGES.map((s) => `
      <button class="stage" data-stage="${s.id}"${stageEnabled(s.id) ? '' : ' disabled'}>
        <span class="stage-num">${s.n}</span>
        <span class="stage-text"><strong>${esc(s.name)}</strong><small>${esc(s.desc)}</small></span>
        <span class="stage-dot" aria-hidden="true"></span>
      </button>`).join('');
    buttons = Array.from(bar.querySelectorAll('.stage'));
  }

  buttons.forEach((btn) => {
    const stage = STAGES.find((s) => s.id === btn.dataset.stage);
    if (!stage) return;
    if (!stageEnabled(stage.id)) btn.disabled = true;
    // 事件绑定前先移除同名监听是做不到的（匿名函数），所以用 dataset 打标防止重复绑定。
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => {
      if (btn.disabled) {
        // 明确说清为什么点不动，而不是让按钮静默无反应。
        const cur = STAGES.find((s) => s.id === CURRENT_STAGE);
        const host = cur ? document.getElementById('p-' + cur.panel) : null;
        const noteHost = (host && host.querySelector('.note')) || $('gatherNote') || $('topicNote');
        if (noteHost) note(noteHost, 'warn', '「答辩材料」还没做，后面会加上。');
        else alert('「答辩材料」还没做，后面会加上。');
        return;
      }
      selectStage(btn.dataset.stage);
    });
  });
}

/** 切换阶段。只切显隐，不刷新页面。 */
function selectStage(id) {
  const stage = STAGES.find((s) => s.id === id);
  if (!stage) return;
  CURRENT_STAGE = id;

  document.querySelectorAll('.stage').forEach((b) =>
    b.classList.toggle('active', b.dataset.stage === id));

  // panel 用旧的 id 方案（p-topics 等），因为四个功能块的内部逻辑都挂在那儿。
  document.querySelectorAll('.panel').forEach((p) =>
    p.classList.toggle('active', p.id === 'p-' + stage.panel));

  if (location.hash.replace('#', '') !== id) location.hash = id;

  // 切到某阶段时把对应的工作区滚到顶部——长稿件滚到一半再切阶段会很迷惑。
  const main = document.querySelector('main');
  if (main) main.scrollTop = 0;
}

/* ---- 阶段状态：只在界面上标记，不做任何门禁 ----
   为什么不禁止跳阶段：作者往往手里已经有初稿，直接进"修改与评估"是完全正常的用法。
   把阶段做成硬性关卡只会挡住真实流程，所以状态只是提示，不拦人。 */
const Progress = {
  done: new Set(),

  markDone(id) {
    if (!id) return;
    this.done.add(id);
    this.paint();
  },

  paint() {
    document.querySelectorAll('.stage').forEach((b) => {
      b.classList.toggle('done', this.done.has(b.dataset.stage));
    });
    const bar = $('stageSummary');
    if (!bar) return;
    const order = STAGES.filter((s) => stageEnabled(s.id));
    const n = order.filter((s) => this.done.has(s.id)).length;
    bar.textContent = `已完成 ${n}/${order.length} 个阶段`;
  },
};

/* ================================================================ 暗色模式
   与 ThesisMind 一致：localStorage 键 thesis-theme，取值 light/dark/system。

   **"跟随系统"= 按当前小时判断（19:00–07:00 视为暗色）**，与操作系统设置无关。
   这不是偷懒，是参考站的既定行为——我重新抓过它的页面核实：它的内联脚本就是
   `var h = new Date().getHours(); p === "system" && (h >= 19 || h < 7)`，
   整页 HTML 里 `prefers-color-scheme` 一次都没出现。它的取舍是"晚上打开写作工具
   默认就是暗色"，与系统设置无关。

   早先这里挂过一个 `prefers-color-scheme` 监听，与上面的判定**语义冲突**：
   白天系统是暗色时，首屏内联脚本先给亮色、监听一触发又翻成暗色，用户看到闪一下。
   两处判断必须同源，所以监听已移除。改动这行之前请先想清楚"跟随系统"到底指哪个系统。 */
const Theme = {
  KEY: 'thesis-theme',

  get() {
    return localStorage.getItem(this.KEY) || 'system';
  },

  set(v) {
    localStorage.setItem(this.KEY, v);
    this.apply();
  },

  /** system 是否应视为暗色。**必须与 index.html 里那段防闪白脚本完全一致**，
   *  否则首屏与后续状态会打架（见上面注释里踩过的坑）。 */
  systemIsDark() {
    const h = new Date().getHours();
    return h >= 19 || h < 7;
  },

  resolved() {
    const p = this.get();
    if (p === 'dark') return 'dark';
    if (p === 'light') return 'light';
    return this.systemIsDark() ? 'dark' : 'light';
  },

  apply() {
    const dark = this.resolved() === 'dark';
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    const btn = $('themeToggle');
    if (btn) {
      const p = this.get();
      btn.textContent = p === 'system' ? '🌗' : (p === 'dark' ? '🌙' : '☀️');
      btn.title = `主题：${{ system: '跟随系统（19:00–07:00 自动暗色）', dark: '暗色', light: '亮色' }[p] }（点击切换）`;
    }
  },

  /** 三态循环：跟随系统 → 暗色 → 亮色 → 跟随系统 */
  cycle() {
    const order = ['system', 'dark', 'light'];
    const next = order[(order.indexOf(this.get()) + 1) % order.length];
    this.set(next);
  },

  bind() {
    const btn = $('themeToggle');
    if (btn) btn.addEventListener('click', () => this.cycle());
    this.apply();
  },
};

/* 兼容层：旧代码里还留着 selectTab('writing') 这类调用（例如从题目卡片跳到写作）。
   保留这个函数名并把旧页签 id 映射到阶段，避免到处改调用点——
   映射表写明了"旧概念 → 新概念"，读代码时不会困惑。 */
const LEGACY_TAB_TO_STAGE = {
  topics: 'topic', gather: 'proposal', writing: 'draft', review: 'revise',
};

function selectTab(id) {
  selectStage(LEGACY_TAB_TO_STAGE[id] || id);
}

/* ---------------------------------------------------------------- 下一步按钮
   连续工作流不能只是"把四个页签改叫阶段"——每个阶段做完必须有一眼可见的出口，
   否则用户照样会停在原地不知道往哪走。这个函数在每个 panel 底部注入一个
   "下一步：xxx"按钮。

   用 JS 注入而不是写进 HTML：按钮属于**流程控制**，流程定义在 STAGES 里，
   两边分开放会不同步（改了阶段顺序却忘了改按钮）。
   注入发生在 boot 阶段，那时子代理对 index.html 的改动已经就位。 */
const NEXT_OF_PANEL = {
  topics: { to: 'proposal', label: '下一步：找文献与依据（开题报告）' },
  gather: { to: 'draft', label: '下一步：逐章写初稿' },
  writing: { to: 'revise', label: '下一步：跑检查、出意见书（修改与评估）' },
  review: null,                       // 最后一站，没有下一步
};

/** 注入"下一步"区块的样式。
 *
 *  为什么不写进 theme.css：这段样式只为 injectNextButtons 造出来的 DOM 服务，
 *  放在同一个文件里，改了按钮结构就不会忘记改样式。而且 theme.css 是设计系统的
 *  地盘（令牌、布局、通用组件），流程控制样式混进去会让两边的职责变模糊。
 *  只注入一次，重复调用是安全的。 */
function injectNextButtonStyles() {
  if (document.getElementById('stageNextStyles')) return;
  const style = document.createElement('style');
  style.id = 'stageNextStyles';
  style.textContent = `
    .stage-next {
      margin-top: 18px; padding-top: 16px;
      border-top: 1px dashed var(--tm-border, var(--line));
      display: flex; flex-direction: column; gap: 10px; align-items: flex-start;
    }
    .stage-next-btn { font-size: 14px; }
    .stage-next-hint { font-size: 13px; color: var(--tm-muted-foreground, var(--ink-3)); }
    .stage-next .note { margin: 0; width: 100%; }
  `;
  document.head.appendChild(style);
}

function injectNextButtons() {
  injectNextButtonStyles();

  Object.entries(NEXT_OF_PANEL).forEach(([panel, next]) => {
    const host = document.getElementById('p-' + panel);
    if (!host) return;

    // 已经注入过就不重复（boot 可能被再次调用，例如以后加热重载）
    if (host.querySelector('.stage-next')) return;

    const wrap = document.createElement('div');
    wrap.className = 'stage-next';
    if (!next) {
      wrap.innerHTML = '<span class="stage-next-hint">到这里整条链路就走完了。' +
        '产物可以逐个导出，也可以回上面任意阶段继续改。</span>';
    } else {
      const btn = document.createElement('button');
      btn.className = 'btn stage-next-btn';
      btn.textContent = next.label;
      btn.addEventListener('click', () => {
        selectStage(next.to);
        const msg = document.getElementById('stageNextNote');
        if (msg) {
          msg.className = 'note ok';
          msg.textContent = `已进入「${STAGES.find((s) => s.id === next.to).name}」阶段。`;
        }
      });
      wrap.appendChild(btn);
      const hint = document.createElement('div');
      hint.id = 'stageNextNote';
      hint.className = 'note';
      hint.style.display = 'none';
      wrap.appendChild(hint);
    }
    host.appendChild(wrap);
  });
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
        Progress.markDone('topic');
        setProgress($('topicBar'), $('topicMsg'), $('topicProg'), 1, '完成');
        $('btnTopicExport').disabled = false;
        // 阶段化之后要主动告诉用户"下一步做什么"，否则他会停在原地。
        note($('topicNote'), 'ok',
          '选题完成。<strong>下一步：去「开题报告」阶段把文献与依据找齐</strong>——' +
          '写作只依据证据库与联网检索到的真实文献，依据越足，初稿越实。' +
          '<br>点左侧的阶段导航即可继续；也可以点下面任意一个题目，把题目带到写作阶段。');
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

/* ============================================================ Agent 实时状态
   四个助手在后端已经是真 agent（可寻址、共享状态、每次动作留事件）。
   这里把它们的实时状态显示出来——**这就是"实时更新子 agent"那件事的可见部分**。

   为什么用轮询而不是 SSE：agent 的动作是"请求驱动"的短过程（秒级到分钟级），
   而发起动作的请求本身已经有自己的 SSE 流在处理进度。再开一条状态流会多一套
   连接管理，收益却只是把刷新从 1.5 秒提到瞬时。轮询足够，而且失败时静默重试即可、
   不需要重连逻辑。 */
const AgentPanel = {
  timer: null,
  state: [],

  async refresh() {
    const sid = SID.get();
    if (!sid) { this.paint([]); return; }
    try {
      const data = await api(`/api/agents/state?sid=${encodeURIComponent(sid)}`);
      this.paint(data.agents || []);
    } catch (_) {
      // 轮询失败不弹错误：会话可能刚过期，让用户继续操作，下次轮询自然恢复。
    }
  },

  paint(agents) {
    this.state = agents;
    // 1) 侧栏阶段按钮上显示"这个阶段的 agent 正在干活"
    document.querySelectorAll('.stage').forEach((btn) => {
      const sid_ = btn.dataset.stage;
      const a = agents.find((x) => x.stage === sid_);
      btn.classList.toggle('busy', !!(a && a.busy));
      const dot = btn.querySelector('.stage-dot');
      if (dot && a && a.busy) dot.setAttribute('title', `${a.title} 正在：${a.current?.message || a.current?.action || ''}`);
    });

    // 2) 底部状态条：四个 agent 各一行
    const host = $('agentPanel');
    if (!host) return;
    if (!agents.length) {
      host.innerHTML = '<div class="agent-empty">开始使用后，这里会显示四个助手各自在做什么。</div>';
      return;
    }
    host.innerHTML = agents.map((a) => {
      const busy = a.busy;
      const last = a.last || {};
      const icon = busy ? '◐' : (a.failed_count && !a.ok_count ? '✕' : (a.total ? '●' : '○'));
      const cls = busy ? 'busy' : (a.total ? 'done' : 'idle');
      const text = busy
        ? (a.current?.message || a.current?.action || '处理中…')
        : (a.total ? `${last.action || ''} · ${last.seconds ?? ''}s` : '还没用过');
      return `<div class="agent-row ${cls}" data-agent="${a.name}">
          <span class="agent-icon">${icon}</span>
          <span class="agent-name">${esc(a.title)}</span>
          <span class="agent-text" title="${esc(text)}">${esc(text)}</span>
          <span class="agent-count">${a.ok_count}✓${a.failed_count ? ' ' + a.failed_count + '✕' : ''}</span>
        </div>`;
    }).join('');
  },

  start() {
    if (this.timer) return;
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 1500);
  },
};

/* ============================================================ 新功能接线
   五个补齐的能力都挂在对应阶段的工具栏上。它们都走**通用 agent 接口**
   （/api/agents/{name}/{action}），而不是各自单独的路由——
   这样前端只需要一个调用函数，加新动作也不用改这里。 */
async function callAgent(name, action, body) {
  const payload = Object.assign({ sid: SID.get() }, body || {});
  const data = await api(`/api/agents/${name}/${action}`, { method: 'POST', body: payload });
  if (data.sid) SID.set(data.sid);
  return data;
}

/** 由大纲生成章节结构，填进写作阶段的章节框。 */
async function runOutline() {
  const title = $('wTitle').value.trim() || ($('tDiscipline').value.trim() ? '' : '');
  if (!title) { note($('writeNote'), 'err', '请先填写论文题目。'); return; }
  if (!LLM.ready) { note($('writeNote'), 'err', '生成大纲需要模型：请先填入 API Key。'); return; }
  const btn = $('btnOutline');
  btn.disabled = true; btn.textContent = '生成中…';
  note($('writeNote'), '', '');
  try {
    const genreName = ($('wGenre').selectedOptions[0] || {}).textContent || '';
    const sections = $('wSections').value.split('\n').map((s) => s.trim()).filter(Boolean);
    const data = await callAgent('topic', 'outline', { title, genre_name: genreName, sections });
    if (!data.ok) throw new Error(data.error || '生成失败');
    renderOutline(data.outline);
    if (data.section_text) $('wSections').value = data.section_text;
    note($('writeNote'), 'ok', '大纲已生成并填入下方章节结构，可直接逐章生成；也可以手工调整后再写。');
  } catch (err) {
    note($('writeNote'), 'err', '生成大纲失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '生成大纲';
  }
}

function renderOutline(outline) {
  const host = $('outlineBox');
  const secs = (outline || {}).sections || [];
  if (!secs.length) { host.innerHTML = ''; return; }
  host.innerHTML = secs.map((s) => `
    <div class="outline-sec">
      <div class="outline-title">${esc(s.title)}${s.focus ? ` <span class="outline-focus">${esc(s.focus)}</span>` : ''}</div>
      ${(s.children || []).map((c) => `
        <div class="outline-sub">
          <strong>${esc(c.title)}</strong>
          ${(c.points || []).length ? `<div class="outline-line">要点：${c.points.map(esc).join('；')}</div>` : ''}
          ${(c.needs || []).length ? `<div class="outline-line outline-need">需要材料：${c.needs.map(esc).join('；')}</div>` : ''}
        </div>`).join('')}
    </div>`).join('');
}

/** 按评估意见改写某一章。 */
async function runRevise() {
  const heading = $('reviseHeading').value.trim();
  if (!heading) { note($('reviewNote'), 'err', '请填写要改写的章节标题（例如「第2章」）。'); return; }
  if (!LLM.ready) { note($('reviewNote'), 'err', '按意见改写需要模型：请先填入 API Key。'); return; }
  const btn = $('btnRevise');
  btn.disabled = true; btn.textContent = '改写中…';
  setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 0.5, '正在按意见定点改写…');
  try {
    const data = await callAgent('writer', 'revise', { heading });
    if (!data.ok) throw new Error(data.error || '改写失败');
    renderDraft(data.draft);
    const changes = (data.changes || []).map((c) => `· ${c.what}${c.why ? `（${c.why}）` : ''}`).join('<br>');
    const unsolved = (data.unsolved || []).map((x) => `· ${x}`).join('<br>');
    note($('reviewNote'), (data.unsolved || []).length ? 'warn' : 'ok',
      `<strong>改写完成</strong>（第 ${data.version_count} 版，可回退）。` +
      (changes ? `<br>改动：<br>${changes}` : '') +
      (unsolved ? `<br><strong>材料不足、未解决：</strong><br>${unsolved}` : ''));
  } catch (err) {
    note($('reviewNote'), 'err', '改写失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '按意见改写';
    hideProgress($('reviewProg'));
  }
}

/** 查重风险自查。 */
async function runDuplication() {
  const btn = $('btnDup');
  btn.disabled = true; btn.textContent = '自查中…';
  setProgress($('reviewBar'), $('reviewMsg'), $('reviewProg'), 0.4, '正在做内部重复检测与文献核验…');
  try {
    const data = await callAgent('review', 'duplication', { text: $('rText').value, ref_limit: 8 });
    if (!data.ok) throw new Error(data.error || '自查失败');
    renderDuplication(data);
  } catch (err) {
    note($('reviewNote'), 'err', '查重自查失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '查重风险自查';
    hideProgress($('reviewProg'));
  }
}

function renderDuplication(data) {
  const host = $('dupBox');
  host.classList.remove('hidden');
  const riskLabel = { high: '偏高', medium: '需注意', low: '未发现明显问题' };
  const riskCls = { high: 'bad', medium: 'warn', low: 'ok' };
  const internal = data.internal || {};
  const refs = data.references || {};
  host.innerHTML = `
    <h2>查重风险自查</h2>
    <div class="note ${riskCls[data.risk] || ''}">
      内部重复风险：<strong>${riskLabel[data.risk] || data.risk}</strong>
      —— 检查了 ${internal.paragraphs || 0} 个段落，发现 ${internal.duplicate_pair_count || 0} 处高度重复。
    </div>
    <div class="note warn">${esc(data.disclaimer || '')}</div>
    ${(internal.duplicate_pairs || []).length ? `
      <table class="list">
        <tr><th style="width:70px">相似度</th><th>段落 A</th><th>段落 B</th></tr>
        ${internal.duplicate_pairs.slice(0, 8).map((p) => `<tr>
          <td>${p.similarity}</td>
          <td style="font-size:12.5px">第 ${p.a_index} 段：${esc(p.a_preview)}</td>
          <td style="font-size:12.5px">第 ${p.b_index} 段：${esc(p.b_preview)}</td>
        </tr>`).join('')}
      </table>` : '<div class="note">未发现文档内部的段落重复。</div>'}
    <h2 style="margin-top:16px">参考文献可核验性</h2>
    <div class="stat">
      <div>已核验<b>${refs.checked || 0}</b></div>
      <div>查不到<b>${(refs.unverifiable || []).length}</b></div>
      <div>无法确认<b>${(refs.tally || {}).inconclusive || 0}</b></div>
    </div>
    ${(refs.unverifiable || []).length ? `<table class="list">
      <tr><th>在公开文献库里查不到</th></tr>
      ${refs.unverifiable.map((u) => `<tr><td>${esc(u.title)}${u.doi ? `（DOI: ${esc(u.doi)}）` : ''}</td></tr>`).join('')}
    </table>` : ''}
    <div class="note">${esc(refs.limits || '')}</div>`;
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** 学校模板比对。 */
async function runTemplateCompare() {
  const tplFile = $('tplFile').files[0];
  if (!tplFile) { note($('reviewNote'), 'err', '请先选择学校模板文件（.docx）。'); return; }
  const btn = $('btnTemplate');
  btn.disabled = true; btn.textContent = '比对中…';
  try {
    const b64 = await fileToBase64(tplFile);
    // 参数契约与后端一致：base64 字符串走 *_b64 字段。
    // 早先我在这里把文件名和内容拼成一个字符串，那种约定很脆——后端稍改就静默不匹配。
    const data = await callAgent('review', 'template', { template_b64: b64 });
    if (!data.ok) throw new Error(data.error || '比对失败');
    renderTemplate(data);
  } catch (err) {
    note($('reviewNote'), 'err', '模板比对失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '与模板比对';
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => {
      const s = String(fr.result || '');
      resolve(s.slice(s.indexOf(',') + 1));
    };
    fr.onerror = () => reject(new Error('读取文件失败'));
    fr.readAsDataURL(file);
  });
}

function renderTemplate(data) {
  const host = $('tplBox');
  host.classList.remove('hidden');
  const tpl = data.template || {};
  const rows = Object.entries(tpl.styles || {}).map(([k, v]) => `
    <tr><td>${esc(k)}</td><td>${v.size_pt ?? '—'}</td>
        <td>${esc(v.east_asia_font || v.font || '—')}</td>
        <td>${v.line_spacing ?? '—'}</td></tr>`).join('');
  host.innerHTML = `
    <h2>学校模板比对</h2>
    <div class="note ${data.conform ? 'ok' : 'warn'}">
      ${data.conform ? '稿件格式与模板一致。' : `发现 <strong>${(data.diffs || []).length}</strong> 处与模板不一致。`}
    </div>
    <h3 style="font-size:14px;margin:12px 0 6px">从模板识别出的格式</h3>
    <table class="list">
      <tr><th>样式</th><th style="width:80px">字号(pt)</th><th style="width:110px">中文字体</th><th style="width:80px">行距</th></tr>
      ${rows || '<tr><td colspan="4">没能从模板里读出样式（可能模板用的是直接格式而非样式）</td></tr>'}
    </table>
    ${(data.diffs || []).length ? `
      <h3 style="font-size:14px;margin:14px 0 6px">不一致项</h3>
      <table class="list">
        <tr><th>项目</th><th style="width:110px">模板要求</th><th style="width:110px">你的稿件</th></tr>
        ${data.diffs.map((d) => `<tr><td>${esc(d.item)}</td><td>${esc(String(d.expected))}</td><td>${esc(String(d.actual))}</td></tr>`).join('')}
      </table>` : ''}
    <div class="note">${esc(data.limits || '')}</div>`;
  host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** 导出 Word。走 agent 接口拿到 base64，再在浏览器里落盘。 */
async function exportDocx() {
  const btn = $('btnDraftDocx');
  btn.disabled = true; btn.textContent = '导出中…';
  try {
    const data = await callAgent('writer', 'export_docx', {});
    if (!data.ok) throw new Error(data.error || '导出失败');
    const bin = atob(data.content_base64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const blob = new Blob([bytes], { type: data.content_type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = data.filename || '稿件.docx';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    note($('writeNote'), 'ok', `已导出 Word（${Math.round((data.bytes || 0) / 1024)} KB）。`);
  } catch (err) {
    note($('writeNote'), 'err', '导出 Word 失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '导出 Word';
  }
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
    Progress.markDone('proposal');
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
        Progress.markDone('draft');
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 1, '完成');
        $('btnDraftExport').disabled = false;
        const cov = ev.coverage || {};
        // 编造引用与无依据数字必须用醒目的提示，不能只塞在覆盖度面板里。
        if (cov.citations_invalid || cov.ungrounded_numbers) {
          note($('writeNote'), 'warn',
            `生成完成，但有 <strong>${cov.citations_invalid || 0} 处编造的引用编号</strong>、` +
            `<strong>${cov.ungrounded_numbers || 0} 处找不到依据的数字</strong>，` +
            '已在稿件上方逐章标出，请务必人工核对。' +
            '<br><strong>下一步：去「修改与评估」阶段跑一遍检查。</strong>');
        } else {
          note($('writeNote'), 'ok',
            '初稿完成，引用与数字都能在依据池里找到。' +
            '<br><strong>下一步：去「修改与评估」阶段跑 53 项检查，看还有什么问题。</strong>');
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
    Progress.markDone('revise');
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
  // 主题要**先**应用：晚一步会看到亮色闪一下再变暗。
  Theme.bind();

  buildStages();
  const hash = location.hash.replace('#', '');
  const valid = STAGES.some((s) => s.id === hash && stageEnabled(s.id));
  selectStage(valid ? hash : 'topic');

  // 浏览器前进/后退要能切阶段，否则用户按返回键会跳出站点。
  window.addEventListener('hashchange', () => {
    const id = location.hash.replace('#', '');
    if (id && id !== CURRENT_STAGE && STAGES.some((s) => s.id === id && stageEnabled(s.id))) {
      selectStage(id);
    }
  });

  bindDropzone();
  injectNextButtons();
  // agent 状态轮询：页面一打开就启动，让"谁在干活"这件事始终可见。
  AgentPanel.start();

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

// 本轮补齐的功能
$('btnOutline') && $('btnOutline').addEventListener('click', runOutline);
$('btnDraftDocx') && $('btnDraftDocx').addEventListener('click', exportDocx);
$('btnRevise') && $('btnRevise').addEventListener('click', runRevise);
$('btnDup') && $('btnDup').addEventListener('click', runDuplication);
$('btnTemplate') && $('btnTemplate').addEventListener('click', runTemplateCompare);
$('btnReviewExport').addEventListener('click', () => {
  if (Review.markdown) download(Review.markdown, '审稿意见书.md');
});

// 点一个题目 → 题目存下来，然后进"开题报告"阶段。
//
// 早先这里直接跳到写作页签并提示"先去录入资料"，但那时界面是四个并列页签，
// 用户看不出该往哪走。改成工作流之后，题目应当顺着阶段往下流动：
// 定题目 → 开题报告（找依据）→ 写初稿。所以这里只做第一步，
// 把题目带到下一步，并明确告诉他下一步要干什么。
document.addEventListener('click', (e) => {
  const h = e.target.closest('.topic h3');
  if (!h) return;
  const title = h.textContent.replace(/^\s*\d+\s*/, '').trim();
  if (!title) return;

  $('wTitle').value = title;              // 写作阶段的题目输入框
  Write.chosenTitle = title;
  Progress.markDone('topic');

  selectStage('proposal');
  note($('gatherNote'), 'ok',
    `已选定题目：<strong>${esc(title)}</strong>` +
    '<br>这一步把文献与依据找齐——可以直接<strong>拖入 PDF / Word 资料</strong>，' +
    '或粘贴参考文献让助手联网核验。依据备齐后点左侧「写初稿」继续。');
});

boot();
