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
  //: 本站是否配置了自己的模型凭据。为真时访客**不需要**填 Key，
  //: 顶部那个输入框会收起成一行说明——否则访客会以为自己必须去申请一个 API Key，
  //: 而实际上直接用就行。
  serverSide: false,
  serverMasked: '',

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
    // 访客填了 Key 就带上去（会用他自己的，优先于本站的）；没填就不带，
    // 由服务端决定用本站凭据还是走离线模式。
    if (this.key) h['X-LLM-Key'] = this.key;
    const preset = this.providers[this.provider] || {};
    const base = (this.base || '').trim() || preset.base_url || '';
    const model = (this.model || '').trim() || preset.model || '';
    // 只有访客自己填了 Key 时才带地址与模型：否则会让"本站 Key + 访客预设地址"
    // 混搭，那可能不是站主预期的组合。
    if (this.key) {
      if (base) h['X-LLM-Base'] = base;
      if (model) h['X-LLM-Model'] = model;
    }
    return h;
  },
  get ready() { return !!(this.key.trim() || this.serverSide); },
  get usingServer() { return !this.key.trim() && this.serverSide; },
  label() {
    const preset = this.providers[this.provider] || {};
    return preset.label || this.provider || '未选服务商';
  },
  paint() {
    const badge = $('keyBadge');
    const box = $('keybox');
    if (this.serverSide) {
      // 本站出 Key：收起输入框，只留一行说明。
      if (box) box.classList.add('server-side');
      badge.className = 'keybadge on';
      badge.textContent = this.key.trim()
        ? '使用你自己的 Key'
        : '本站已配置模型，直接可用';
      badge.title = this.key.trim()
        ? '你填了 Key，将优先使用你自己的'
        : `本站凭据 ${this.serverMasked || ''}；填自己的 Key 可优先使用你自己的`;
      return;
    }
    // 本站没有 Key：保持原来的 BYOK 行为
    if (box) box.classList.remove('server-side');
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
  { id: 'topic',     n: '1', name: '定题目',          desc: '出题 + 建可检索的知识库', panel: 'topics' },
  { id: 'proposal',  n: '2', name: '参考文献与知识库', desc: '推荐文献、导资料入库、读文献', panel: 'gather' },
  { id: 'report',    n: '3', name: '生成开题报告',    desc: '据知识库逐节成稿，参考文献机器汇总', panel: 'proposal' },
  { id: 'draft',     n: '4', name: '写初稿',          desc: '依据三层来源逐章生成',      panel: 'writing' },
  { id: 'revise',    n: '5', name: '修改与评估',      desc: '53 个检查器 + 按意见改写',  panel: 'review' },
  { id: 'integrity', n: '6', name: '查重与降 AIGC 率', desc: '内部重复自查 + AI 味体检',  panel: 'integrity' },
  { id: 'defense',   n: '7', name: '答辩',            desc: '答辩稿与演示幻灯片',        panel: 'defense' },
];

let CURRENT_STAGE = 'topic';

/* 把阶段清单暴露给 index.html 末尾的内联脚本（它负责写"当前阶段：xxx"）。
   为什么不让它自己再存一份：同一个概念存两处，改一处漏一处就会漂移——
   实测就是这样，"当前阶段"一直显示旧名字。这里只暴露，不改变任何行为。 */
window.SUITE_STAGES = STAGES;

/** 板块是否可用。
 *
 *  现在六个板块都已实现，所以恒为 true——但**保留这个函数**而不是删掉：
 *  它是"某个板块尚未就绪时如何呈现"的唯一开关，下一个板块做之前会再用到。
 *  直接写死 true 会让下次加板块时忘了处理这种情况。
 */
function stageEnabled(id) {
  return STAGES.some((s) => s.id === id);
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
        const noteHost = (host && host.querySelector('.note')) || $('topicNote');
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

  // 进入「开题报告」时刷新 PaperQA2 的状态与知识库清单。为什么要挂在这里而不是
  // 像 AgentPanel 那样轮询：这两个值只在"用户要看它"的时候才需要准确，
  // 而轮询会给每次切阶段都带来两个没人看的请求；进面板这一个点上拉一次就够。
  if (id === 'proposal') refreshPqaOnEnter();
  // 进入「生成开题报告」时刷新知识库规模：它直接决定这一阶段的依据够不够。
  if (id === 'report') { proposalOptions().catch(() => {}); }

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
  topics: { to: 'proposal', label: '下一步：找文献、把资料导入知识库' },
  gather: { to: 'report', label: '下一步：生成开题报告' },
  proposal: { to: 'draft', label: '下一步：逐章写初稿' },
  writing: { to: 'revise', label: '下一步：跑检查、出意见书（修改与评估）' },
  review: { to: 'integrity', label: '下一步：查重自查与降 AIGC 率' },
  integrity: { to: 'defense', label: '下一步：生成答辩稿与演示幻灯片' },
  defense: null,                      // 最后一站，没有下一步
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

/* ---- 文件导入 ----
   解析在后端完成，**不需要 API Key**，所以这里用 fetch + FormData 直接传，
   不走 api() 那个 JSON 封装（它会给请求加 Content-Type: application/json，
   那会让 multipart 的 boundary 失效）。 */
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
  // 查重结果渲染到⑤板块的容器里。原先它渲染在「修改与评估」的 dupBox，
  // 但查重已经独立成板块——结果必须出现在用户点按钮的那个板块，
  // 否则他点完看不到任何变化，会以为没生效。
  const host = $('integrityResultCard') ? $('integrityStat') : null;
  const container = $('integrityNote');
  const riskLabel = { high: '偏高', medium: '需注意', low: '未发现明显问题' };
  const riskCls = { high: 'bad', medium: 'warn', low: 'ok' };
  const internal = data.internal || {};
  const refs = data.references || {};
  const html = `
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
    <h3 style="font-size:14px;margin:16px 0 6px">参考文献可核验性</h3>
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

  if (container) {
    container.innerHTML = html;
    $('integrityResultCard').classList.remove('hidden');
    $('integrityResultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  return host;
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

/* ====================================================== ⑤ 查重与降 AIGC 率 */
const Integrity = { revised: '', slides: null, script: '' };

/** 统一在浏览器里落盘一个 base64 二进制。 */
function downloadBinary(base64, filename, contentType) {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: contentType }));
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function integrityText() {
  return $('iText').value.trim();
}

async function runIntegrityDup() {
  const btn = $('btnIntegrityDup');
  btn.disabled = true; btn.textContent = '自查中…';
  note($('integrityNote'), '', '');
  setProgress($('integrityBar'), $('integrityMsg'), $('integrityProg'), 0.4, '正在做内部重复检测与文献核验…');
  try {
    const data = await callAgent('integrity', 'duplication',
      { text: integrityText(), ref_limit: 8 });
    if (!data.ok) throw new Error(data.error || '自查失败');
    renderDuplication(data);
    note($('integrityNote'), 'ok', '自查完成，结果见下方。');
  } catch (err) {
    note($('integrityNote'), 'err', '自查失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '查重风险自查';
    hideProgress($('integrityProg'));
  }
}

async function runAigcScore() {
  const btn = $('btnAigcScore');
  btn.disabled = true; btn.textContent = '体检中…';
  note($('integrityNote'), '', '');
  setProgress($('integrityBar'), $('integrityMsg'), $('integrityProg'), 0.4, '正在做本地风格体检…');
  try {
    const data = await callAgent('integrity', 'aigc_score', { text: integrityText() });
    if (data.ok === false) throw new Error(data.error || '体检失败');
    renderAigc(data, 'score');
    // 体检通过后才允许改写：先看清问题再改，避免"一键降重"式盲改
    $('btnAigcReduce').disabled = false;
  } catch (err) {
    note($('integrityNote'), 'err', '体检失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = 'AI 味体检';
    hideProgress($('integrityProg'));
  }
}

async function runAigcReduce() {
  const btn = $('btnAigcReduce');
  btn.disabled = true; btn.textContent = '改写中…';
  setProgress($('integrityBar'), $('integrityMsg'), $('integrityProg'), 0.5,
    '分段改写 + 保真校验 + 复检，可能要一会儿…');
  try {
    const data = await callAgent('integrity', 'aigc_reduce', { text: integrityText() });
    if (data.ok === false) throw new Error(data.error || '改写失败');
    renderAigc(data, 'reduce');
    Integrity.revised = data.text || data.revised || '';
    if (Integrity.revised) {
      $('btnAigcDocx').disabled = false;
      $('aigcRevised').textContent = Integrity.revised;
      $('aigcRawWrap').classList.remove('hidden');
    }
  } catch (err) {
    note($('integrityNote'), 'err', '改写失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '降 AIGC 率改写';
    hideProgress($('integrityProg'));
  }
}

async function exportAigcDocx() {
  const btn = $('btnAigcDocx');
  btn.disabled = true; btn.textContent = '导出中…';
  try {
    const data = await callAgent('integrity', 'aigc_export_docx',
      { text: Integrity.revised, title: $('wTitle').value.trim() || '改写稿' });
    if (!data.ok) throw new Error(data.error || '导出失败');
    downloadBinary(data.content_base64, data.filename, data.content_type);
    note($('integrityNote'), 'ok', `已导出 Word（${Math.round((data.bytes || 0) / 1024)} KB）。`);
  } catch (err) {
    note($('integrityNote'), 'err', '导出失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '导出改写稿 Word';
  }
}

/** 渲染体检/改写结果。**免责声明必须显示出来**，不能只放在返回里。 */
function renderAigc(data, kind) {
  $('integrityResultCard').classList.remove('hidden');
  const before = data.before ?? data.before_index ?? data.index ?? data.ai_index;
  const after = data.after ?? data.after_index;
  $('integrityStat').innerHTML = [
    ['AI 味指数', before ?? '—'],
    ...(kind === 'reduce' ? [['改写后', after ?? '—']] : []),
    ['改动段落', (data.changed ?? data.changed_count ?? '—')],
    ['退回原文', (data.reverted ?? data.reverted_count ?? '—')],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  const disc = data.disclaimer || data.limits || '';
  const metrics = data.metrics || data.items || [];
  $('aigcMetrics').innerHTML =
    (disc ? `<div class="note warn">${esc(disc)}</div>` : '') +
    (Array.isArray(metrics) && metrics.length
      ? `<table class="list"><tr><th>指标</th><th style="width:90px">数值</th><th>说明</th></tr>
         ${metrics.map((m) => `<tr>
            <td>${esc(m.name || m.label || '')}</td>
            <td>${esc(String(m.value ?? m.score ?? '—'))}</td>
            <td style="font-size:12.5px;color:var(--ink-2)">${esc(m.desc || m.note || '')}</td>
         </tr>`).join('')}</table>`
      : '');
  $('integrityResultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ================================================================ ⑥ 答辩 */
async function runDefenseScript() {
  const btn = $('btnDefenseScript');
  btn.disabled = true; btn.textContent = '生成中…';
  note($('defenseNote'), '', '');
  setProgress($('defenseBar'), $('defenseMsg'), $('defenseProg'), 0.4, '正在写答辩稿…');
  try {
    const data = await callAgent('defense', 'script', {});
    if (!data.ok) throw new Error(data.error || '生成失败');
    Integrity.script = data.markdown || '';
    $('defenseScriptBody').innerHTML = md(data.markdown || '');
    $('defenseScriptCard').classList.remove('hidden');
    note($('defenseNote'), 'ok', '答辩稿已生成。陈述稿用于口播，预计提问用于准备应答。');
  } catch (err) {
    note($('defenseNote'), 'err', '生成答辩稿失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '生成答辩稿';
    hideProgress($('defenseProg'));
  }
}

async function runDefenseSlides() {
  const btn = $('btnDefenseSlides');
  btn.disabled = true; btn.textContent = '生成中…';
  setProgress($('defenseBar'), $('defenseMsg'), $('defenseProg'), 0.4, '正在生成幻灯片…');
  try {
    const data = await callAgent('defense', 'slides', {});
    if (!data.ok) throw new Error(data.error || '生成失败');
    Integrity.slides = data.slides;
    renderSlides(data.slides);
    $('btnDefensePptx').disabled = false;
    $('btnDefenseHtml').disabled = false;
    const pptxOk = data.pptx_available;
    note($('defenseNote'), pptxOk ? 'ok' : 'warn',
      pptxOk ? '幻灯片已生成，可导出 .pptx 或单文件 HTML。'
             : '幻灯片已生成。注意：本机没有 python-pptx，<strong>.pptx 导出不可用</strong>，请用 HTML 演示稿。');
    if (!pptxOk) $('btnDefensePptx').disabled = true;
  } catch (err) {
    note($('defenseNote'), 'err', '生成幻灯片失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '生成幻灯片';
    hideProgress($('defenseProg'));
  }
}

function renderSlides(slides) {
  const list = (slides || {}).slides || [];
  $('defenseSlidesCard').classList.remove('hidden');
  $('defenseSlidesList').innerHTML = list.map((s, i) => `
    <div class="topic">
      <h3><span class="idx">${i + 1}</span>${esc(s.title || '')}</h3>
      ${(s.bullets || []).length ? `<ul style="margin:6px 0 0 18px;font-size:13.5px">
        ${s.bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
      ${s.notes ? `<div style="margin-top:8px;font-size:12.5px;color:var(--ink-3)">讲稿备注：${esc(s.notes)}</div>` : ''}
    </div>`).join('') || '<div class="note">没有生成任何幻灯片。</div>';
}

async function exportDefense(kind) {
  const btn = $(kind === 'pptx' ? 'btnDefensePptx' : 'btnDefenseHtml');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '导出中…';
  try {
    const data = await callAgent('defense', kind, { slides: Integrity.slides });
    if (!data.ok) throw new Error(data.error || '导出失败');
    if (kind === 'pptx') {
      downloadBinary(data.content_base64, data.filename, data.content_type);
    } else {
      download(data.html, data.filename);
    }
    note($('defenseNote'), 'ok', `已导出 ${data.filename}（${Math.round((data.bytes || 0) / 1024)} KB）。`);
  } catch (err) {
    note($('defenseNote'), 'err', '导出失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
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
/* ============================================ ②-A 参考文献推荐（开题第 1 步）
   这一块与上面的「资料获取」是**两件不同的事**，所以刻意分成两个卡片：
     · 这里解决"还没有文献"——去公开库检索，产出一份可导出的清单；
     · 上面解决"手里已经有资料"——把 PDF / 粘贴文本抽成带页码的可引用证据。
   两者最后都汇进 RAG 知识库，但入口、失败模式与花费完全不同，合成一块只会互相拖累。

   一个关键取舍：**推荐全程不需要 API Key**（只查公开库）。所以这里没有把"没填 Key"
   做成前置门禁；只有勾了模型精排才与 Key 有关，而精排失败只降级不报错。 */
const Refs = { last: null };

/** 关键词输入 → 数组。顿号、分号也当分隔符：中文用户很自然会写"数据资产、会计确认"。 */
function refsKeywords() {
  return $('refsKeywords').value
    .split(/[,，、;；]/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 8);   // 与后端 keywords[:8] 的口径一致——多传的会被静默丢掉，不如这里就截断
}

/** refsNote 同时承载三件事：操作回执、后端的诚实提醒（notes）、空清单时的成功提示。
 *  **它们必须一起重建**：早先在导出成功后直接覆盖 refsNote，会把"中文核心刊未经知网核实"
 *  这类提醒冲掉——而那句话恰恰是最该一直留在屏幕上的。 */
function paintRefsNote(data, kind, html) {
  const host = $('refsNote');
  if (!host) return;
  const notes = (data && data.notes) || [];
  const blocks = [];
  if (html) blocks.push(`<div class="note ${kind || ''}">${html}</div>`);
  if (notes.length) {
    blocks.push('<div class="note warn"><strong>关于这份清单，有几件事必须说清楚：</strong><br>' +
      notes.map(esc).join('<br>') + '</div>');
  } else if (!html && data) {
    blocks.push(`<div class="note ok">已生成 ${esc(String(data.count ?? 0))} 条推荐` +
      `（候选 ${esc(String(data.candidates ?? 0))} 条）。` +
      '勾选条目可<strong>存入知识库</strong>供下一步 PaperQA2 使用，也可以直接导出成参考文献表。</div>');
  }
  host.innerHTML = blocks.join('');
}

async function runRefs() {
  const topic = $('refsTopic').value.trim();
  // 4 个字是后端的硬下限（不足直接 400）。前端先拦一次只是为了让用户在
  // 按下按钮的瞬间就得到反馈，而不是等一个往返再被拒绝。
  if (topic.length < 4) { note($('refsNote'), 'err', '请填写选题 / 研究方向（至少 4 个字）。'); return; }

  const btn = $('btnRefsRun');
  btn.disabled = true; btn.textContent = '检索中…';
  note($('refsNote'), '', '');
  setProgress($('refsBar'), $('refsMsg'), $('refsProg'), 0.08,
    '正在检索 OpenAlex / Crossref / arXiv…（多源串行，通常 10~30 秒）');

  const body = {
    topic,
    keywords: refsKeywords(),
    english: $('refsEnglish').value.trim(),
    total: parseInt($('refsTotal').value, 10) || 15,
    rerank: $('refsRerank').checked,
  };

  try {
    const data = await api('/api/refs/recommend', { method: 'POST', body });
    // 一条候选都没检索到时后端返回 **200 + ok:false**（这不是客户端的错），
    // 所以不能指望 catch 兜住它——必须显式判 ok，否则会渲染出一份空清单。
    if (data.ok === false) {
      note($('refsNote'), 'err', esc(data.error || '没有拿到推荐结果。'));
      // 失败时各检索源的状态与口径说明**照样显示**：那正是"为什么没结果"的答案，
      // 用户需要看出是"源挂了"还是"真的没有这篇文献"。
      renderRefsSources(data.sources || []);
      renderRefsLimits(data);
      hideProgress($('refsProg'));
      return;
    }
    Refs.last = data;
    renderRefs(data);
    setProgress($('refsBar'), $('refsMsg'), $('refsProg'), 1, '完成');
    setTimeout(() => hideProgress($('refsProg')), 1200);
  } catch (err) {
    // 失败时**不清空上一份清单**：把屏幕清干净会让用户以为结果丢了，
    // 而旧清单仍然可用（导出与入库都基于它）。
    note($('refsNote'), 'err', '检索失败：' + esc(err.message));
    hideProgress($('refsProg'));
  } finally {
    btn.disabled = false; btn.textContent = '生成推荐清单';
  }
}

function renderRefs(data) {
  $('refsResultCard').classList.remove('hidden');
  renderRefsStat(data);
  renderRefsSummary(data);
  renderRefsSources(data.sources || []);
  renderRefsList(data.items || []);
  renderRefsLimits(data);
  paintRefsNote(data);
  // 有清单之后导出与入库才可用。放在这里而不是"每次点按钮时再判断"：
  // 灰着的按钮本身就是"先点生成"的提示。
  ['btnRefsExportGbt', 'btnRefsExportBib', 'btnRefsExportMd', 'btnRefsToKb'].forEach((id) => {
    const b = $(id); if (b) b.disabled = false;
  });
  $('refsResultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** 统计条。**字段缺就显示 — ，不补默认值**——编一个"0"比留空更误导。 */
function renderRefsStat(data) {
  const s = data.summary || {};
  const span = (s.year_min != null && s.year_max != null)
    ? `${s.year_min}–${s.year_max}（${s.span_years ?? 0} 年）` : '—';
  $('refsStat').innerHTML = [
    ['入选项数', data.count ?? 0],
    ['候选', data.candidates ?? 0],
    ['时间跨度', span],
    ['开放获取', s.open_access ?? 0],
    ['有摘要', s.with_abstract ?? 0],
    ['有 DOI', s.with_doi ?? 0],
  ].map(([k, v]) => `<div>${esc(k)}<b>${esc(String(v))}</b></div>`).join('');
}

/** 清单画像：等级分布、国内外、以及**这一次到底精排了没有**。
 *  没有这张画像，一份"15 篇全是 2023 年英文刊"的清单看起来和一份均衡的清单一样正常。 */
function renderRefsSummary(data) {
  const s = data.summary || {};
  const tags = (rows) => rows.map(([name, n]) =>
    `<span class="tag">${esc(String(name))} ${esc(String(n))}</span>`).join(' ');
  $('refsSummary').innerHTML = `
    <div style="font-size:13px;color:var(--ink-2);line-height:2.1">
      <div>等级分布：${tags(Object.entries(s.tiers || {})) || '—'}</div>
      <div>国内外：${tags(Object.entries(s.languages || {})) || '—'}</div>
      <div>排序口径：${esc((data.rerank || {}).reason || '—')}</div>
    </div>`;
}

/** 各检索源的状态。**这张表是"源挂了 ≠ 没有这篇文献"的唯一凭据**：
 *  某个源报错时用户能看出是限流 / 网络的问题，而不会误判成"这个选题真的没有文献"。 */
function renderRefsSources(sources) {
  const host = $('refsSources');
  const rows = sources || [];
  if (!host) return;
  if (!rows.length) { host.innerHTML = ''; return; }
  host.innerHTML = `
    <h3 style="font-size:14px;margin:0 0 6px">检索源状态（某个源失败 ≠ 没有这篇文献）</h3>
    <table class="list">
      <tr><th style="width:110px">检索源</th><th>检索式</th><th style="width:190px">结果</th></tr>
      ${rows.map((s) => `<tr>
        <td>${esc(s.source || '')}</td>
        <td style="font-size:12.5px">${esc(s.query || '')}</td>
        <td style="font-size:12.5px">${s.ok
          ? `采用 ${esc(String(s.used ?? 0))} / 返回 ${esc(String(s.found ?? 0))}` +
            (s.seconds != null ? `（${esc(String(s.seconds))} 秒）` : '')
          : `<span class="tag bad">失败</span> ${esc(s.error || '')}`}</td>
      </tr>`).join('')}
    </table>`;
}

function renderRefsList(items) {
  const host = $('refsList');
  if (!host) return;
  if (!items.length) { host.innerHTML = '<div class="note">清单是空的。</div>'; return; }
  host.innerHTML = `<table class="list">
    <tr>
      <th style="width:30px"></th><th style="width:44px">序号</th><th>题名 / 作者 · 来源</th>
      <th style="width:62px">年份</th><th style="width:104px">文献等级</th>
      <th style="width:92px">被引（OpenAlex）</th><th style="width:52px">下载</th>
      <th style="width:84px">开放获取</th><th style="width:22%">排序依据</th>
    </tr>
    ${items.map(refsRowHtml).join('')}
  </table>`;
}

/** 一行清单。两处刻意的"丑但显眼"：
 *  1. 有 warnings 的条目等级标签涂成 bad——"同名不同刊"这类问题用普通灰字会被直接略过；
 *  2. 每条 warning 单独占一行 tag，而不是拼成一句话塞进小字里。
 *  「下载」列固定 — ：任何公开 API 都给不出下载量，写 0 或留空都会被读成"没人下载"。 */
function refsRowHtml(it, index) {
  const warnings = it.warnings || [];
  const tier = `<span class="tag ${warnings.length ? 'bad' : ''}">${esc(it.tier || '未收录/未知')}</span>`;
  const oa = it.open_access
    ? (it.oa_url
      ? `是（<a href="${esc(it.oa_url)}" target="_blank" rel="noopener">全文</a>）`
      : '是')
    : '';
  const cites = (it.citations === null || it.citations === undefined) ? '—' : String(it.citations);
  return `<tr>
    <td><input type="checkbox" class="refsPick" data-idx="${index}" style="width:auto"></td>
    <td>${esc(String(it.rank ?? index + 1))}</td>
    <td>
      <strong>${esc(it.title || '（无题名）')}</strong>
      <div style="color:var(--ink-3);font-size:12px">${esc(it.authors || '作者未标注')} ·
        ${esc(it.venue || it.source || '来源未标注')}${it.doi ? ' · DOI ' + esc(it.doi) : ''}</div>
      ${it.tier_basis ? `<div style="color:var(--ink-3);font-size:12px">等级依据：${esc(it.tier_basis)}</div>` : ''}
      ${it.url ? `<div style="font-size:12px"><a href="${esc(it.url)}" target="_blank" rel="noopener">打开题录页</a></div>` : ''}
      ${warnings.map((w) => `<div style="margin-top:4px"><span class="tag bad">${esc(w)}</span></div>`).join('')}
    </td>
    <td>${esc(String(it.year || '—'))}</td>
    <td>${tier}</td>
    <td>${esc(cites)}</td>
    <td>—</td>
    <td style="font-size:12.5px">${oa}</td>
    <td style="font-size:12px;color:var(--ink-3)">${esc(it.why || '')}${
      it.reason ? `<div>${esc(it.reason)}</div>` : ''}</td>
  </tr>`;
}

/** 排序权重的中文名。让"凭什么排在前面"可读——只给 tier/citations/recency
 *  这样的英文键名，用户没法自己判断这个排序合不合理。 */
const REFS_WEIGHT_LABEL = {
  tier: '文献等级', citations: '被引数', recency: '时效',
  open_access: '开放获取', query_hits: '检索词命中', relevance: '模型判定的相关性',
};

/** 「口径与限制」卡片。limits 与 weights.note 是后端写好的原话，**按纯文本逐字展示**：
 *  它们解释的是"下载量为什么是 —""等级为什么可能是未收录/未知"这类结论边界，
 *  前端改写或省略都会让清单看起来比实际更硬。
 *  这两段里带 ** 强调标记——沿用 gather 那边的做法按纯文本显示（换成 md() 会引入
 *  <p> 外边距把提示框撑松），强调的语气由整个 warn 样式承担。 */
function renderRefsLimits(data) {
  const card = $('refsWarnCard');
  const host = $('refsWarn');
  if (!card || !host) return;
  const limits = data.limits || '';
  const weights = data.weights || {};
  const rows = Object.entries(weights.weights || {}).map(([k, v]) =>
    `<tr><td>${esc(REFS_WEIGHT_LABEL[k] || k)}<span style="color:var(--ink-3)">（${esc(k)}）</span></td>
       <td>${esc(String(v))}</td></tr>`).join('');
  if (!limits && !weights.note && !rows) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  host.innerHTML =
    (limits ? `<div class="note warn">${esc(limits)}</div>` : '') +
    (weights.note ? `<div class="note">${esc(weights.note)}</div>` : '') +
    (rows ? `<h3 style="font-size:14px;margin:14px 0 6px">排序权重（工程默认值，不是实验结论）</h3>
      <table class="list">
        <tr><th style="width:220px">因子</th><th>权重</th></tr>${rows}
      </table>` : '');
}

/** 勾选了哪些条目。**从 DOM 读，而不是自己再存一份选中集合**：
 *  清单是整块 innerHTML 重渲染的，另存的状态就得在每个渲染点同步，
 *  一旦不同步，用户就会把"上一份清单里的第 5 条"存进知识库。 */
function refsPicked() {
  const items = (Refs.last || {}).items || [];
  return Array.from(document.querySelectorAll('#refsList .refsPick:checked'))
    .map((box) => items[Number(box.dataset.idx)])
    .filter(Boolean);
}

/** 导出格式 → 扩展名。与后端 FORMATS 表一致。 */
const REFS_EXPORT_EXT = { gbt: 'txt', bibtex: 'bib', markdown: 'md' };

async function exportRefs(format) {
  const last = Refs.last;
  if (!last) { paintRefsNote(null, 'warn', '还没有推荐清单：先点「生成推荐清单」。'); return; }
  const btnId = { gbt: 'btnRefsExportGbt', bibtex: 'btnRefsExportBib', markdown: 'btnRefsExportMd' }[format];
  const el = btnId ? $(btnId) : null;
  const label = el ? el.textContent : '';
  if (el) { el.disabled = true; el.textContent = '导出中…'; }
  try {
    // markdown 导出由后端**按整份结果重排文档**（表头、构成统计、限制说明都要用到），
    // 所以要把上一次的返回体整个带上；gbt / bibtex 只吃 items。
    const body = format === 'markdown'
      ? Object.assign({}, last, { format })
      : { format, items: last.items || [], topic: last.topic, summary: last.summary,
          limits: last.limits, notes: last.notes };
    const text = await api('/api/refs/export', { method: 'POST', body, raw: true });
    // 文件名去掉路径分隔符与空白：中文选题里常有"/"和空格，直接拼进 download 属性
    // 会被浏览器截断或改成下划线，出来的名字和预期对不上。
    const name = String(last.topic || '推荐清单').replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 40);
    download(text, `参考文献推荐-${name}.${REFS_EXPORT_EXT[format] || 'txt'}`);
    paintRefsNote(last, 'ok', `已导出 ${esc(format)} 格式（${esc(String(text.length))} 字）。`);
  } catch (err) {
    paintRefsNote(last, 'err', '导出失败：' + esc(err.message));
  } finally {
    if (el) { el.disabled = false; el.textContent = label; }
  }
}

async function refsToKb() {
  const items = refsPicked();
  if (!items.length) { paintRefsNote(Refs.last, 'warn', '请先在下面的清单里勾选要入库的文献。'); return; }
  const btn = $('btnRefsToKb');
  btn.disabled = true; btn.textContent = '入库中…';
  try {
    const data = await api('/api/refs/to-kb', { method: 'POST', body: { items } });
    const skipped = data.skipped || [];
    const failed = data.failed || [];
    let msg = `已入库 <strong>${esc(String(data.added_count ?? 0))} 条</strong>文献摘要。`;
    // 跳过与失败都要点名：后端跳过的是"公开库没给摘要"的条目——不列出来的话
    // 用户会以为"我勾了 10 条怎么只进去了 7 条"，而原因恰恰是他需要知道的。
    if (skipped.length) {
      msg += `<br>跳过 ${esc(String(skipped.length))} 条（公开库没有摘要，入库后无法作为依据引用）：` +
        skipped.map((s) => esc(s.title || '')).join('、');
    }
    if (failed.length) {
      msg += `<br>失败 ${esc(String(failed.length))} 条：` +
        failed.map((f) => esc(f.title || '') + '（' + esc(f.error || '') + '）').join('、');
    }
    if (data.note) msg += `<br>${esc(data.note)}`;
    paintRefsNote(Refs.last, (skipped.length || failed.length) ? 'warn' : 'ok', msg);
    // 知识库刚变了：顺手刷新第 2 步的文献清单，否则用户切过去看到的还是旧列表，
    // 会以为"入库没生效"，然后重复入库。
    await loadPqaDocs();
  } catch (err) {
    paintRefsNote(Refs.last, 'err', '入库失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '选中文献存入知识库';
  }
}

/* ====================================== ②-B PaperQA2 文献简报与问答（开题第 2 步）
   与第 1 步的关系：第 1 步"把文献找进来"，这一步"把文献读一遍"。

   三个约束决定了这里的交互形状：
     · PaperQA2 以**子进程**方式调用（numpy/pydantic 版本与主程序不同），因此它可能
       根本没装：状态不可用时把动作按钮全部置灰，而不是让用户点进一个必然失败的流程；
     · 建索引 = 对每篇文献调一次模型，慢且花钱，所以给它一个**独立按钮**，
       而不是藏在第一次提问里——那样用户会面对三分钟没有任何反馈的空白等待；
     · 索引会缓存复用，只有勾选的文献集合变化时才重建，所以"清空索引缓存"要单独给出。 */
const PaperQA = { available: false, status: null, docs: [] };

//: 需要 PaperQA2 可用才允许点的按钮。全选 / 清空选择不在其中——它们只动复选框，
//: 点错了也不会走进死路。
const PQA_ACTIONS = ['btnPqaIndex', 'btnPqaAsk', 'btnPqaBrief', 'btnPqaDigest', 'btnPqaClearIndex'];

//: 缺凭据的措辞与「资料获取」「写初稿」逐字一致——同一种毛病必须说同一句话，
//: 否则用户会以为遇到了两个不同的问题。
const PQA_NEED_KEY = '这一步需要模型：请先在右上角填入你自己的 API Key。';

function pqaSetActionsEnabled(ok) {
  PQA_ACTIONS.forEach((id) => { const b = $(id); if (b) b.disabled = !ok; });
}

/** 勾选的文献 id。**只认显式勾选，不给"默认全库"**：知识库里可能躺着几十篇无关文献，
 *  一次全索引既慢又贵，问答也会被无关内容带偏。 */
function pqaSelectedIds() {
  return Array.from(document.querySelectorAll('#pqaDocs .pqaDoc:checked'))
    .map((box) => box.dataset.docId)
    .filter(Boolean);
}

function pqaPaintCount() {
  const el = $('pqaCount');
  if (!el) return;
  el.textContent = `已选 ${pqaSelectedIds().length} / 共 ${PaperQA.docs.length} 篇`;
}

function pqaSelectAll(checked) {
  document.querySelectorAll('#pqaDocs .pqaDoc').forEach((box) => { box.checked = checked; });
  pqaPaintCount();
}

async function refreshPqaStatus() {
  const host = $('pqaStatus');
  if (!host) return;
  try {
    const data = await api('/api/pqa/status');
    PaperQA.status = data;
    PaperQA.available = !!data.available;
    const detail = data.detail || {};
    const index = data.index || {};
    // 附加提示只列"会影响使用"的项：版本号是给排查用的，缺 PDF 解析器和缺嵌入模型
    // 则直接决定能不能读全文 / 要不要联网下载模型。
    const extra = [];
    if (detail.version) extra.push(`版本 ${detail.version}`);
    if (detail.pymupdf_reader === false) extra.push('PDF 解析器缺失（paperqa_pymupdf 未安装）');
    if (detail.local_embedding === false) extra.push('本地嵌入模型缺失（首次运行会尝试联网下载）');
    else if (detail.embedding_cached === false) extra.push('嵌入模型尚未缓存（首次建索引会先下载）');

    host.innerHTML = `<div class="stat" style="margin-bottom:8px">
        <div>PaperQA2<b>${data.available ? '可用' : '不可用'}</b></div>
        <div>索引<b>${index.indexed ? '已建立' : '未建立'}</b></div>
        <div>索引语料<b>${esc(String(index.corpus_docs ?? 0))} 篇</b></div>
        <div>知识库文档<b>${esc(String(detail.kbdocs ?? 0))} 篇</b></div>
        <div>单次上限<b>${esc(String(detail.max_docs ?? '—'))} 篇</b></div>
      </div>` +
      (extra.length ? `<div style="font-size:12.5px;color:var(--ink-3);margin-bottom:8px">${esc(extra.join('；'))}</div>` : '') +
      (data.available ? '' :
        `<div class="note err"><strong>PaperQA2 现在不可用</strong>，缺少：` +
        `${(data.missing || []).map(esc).join('、') || '（后端未说明缺什么）'}。` +
        '下面这一步的所有动作按钮已禁用——先解决依赖，再回来用。</div>') +
      (data.note ? `<div class="note">${esc(data.note)}</div>` : '');
    pqaSetActionsEnabled(PaperQA.available);
  } catch (err) {
    // 读不到状态就当作"不可用"：宁可挡住，也不要让用户点进一个状态未知的重活。
    PaperQA.available = false;
    pqaSetActionsEnabled(false);
    host.innerHTML = `<div class="note err">读取 PaperQA2 状态失败：${esc(err.message)}</div>`;
  }
}

async function loadPqaDocs() {
  const host = $('pqaDocs');
  if (!host) return;
  try {
    const data = await api('/api/kb/docs');
    PaperQA.docs = data.documents || [];
    renderPqaDocs();
  } catch (err) {
    host.innerHTML = `<div class="note err">读取知识库清单失败：${esc(err.message)}</div>`;
  }
}

function renderPqaDocs() {
  const host = $('pqaDocs');
  if (!host) return;
  const docs = PaperQA.docs || [];
  if (!docs.length) {
    host.innerHTML = '<div class="note">知识库还是空的。在上面「① 把资料放进知识库」里拖入 ' +
      'PDF / Word，或在「按选题推荐参考文献」里勾选文献点「选中文献存入知识库」。</div>';
    pqaPaintCount();
    return;
  }
  // 用 <label> 包住整行，点文字也能勾上：清单可能很长，
  // 逼用户精确点那个小方块是没必要的摩擦。
  // **删除按钮不能放在 <label> 里面**——点在 label 里的任何元素都会连带切换复选框，
  // 于是"想删一篇"会变成"又勾上一遍"。所以每行是 div，勾选区自己套一层 label。
  host.innerHTML = docs.map((d) => {
    const chunks = d.chunks != null ? `${esc(String(d.chunks))} 段` : '';
    return `
    <div style="display:flex;gap:8px;align-items:flex-start;padding:6px 2px;border-bottom:1px solid var(--line);font-size:13px">
      <label style="display:flex;gap:8px;align-items:flex-start;flex:1;cursor:pointer">
        <input type="checkbox" class="pqaDoc" data-doc-id="${esc(d.doc_id)}" style="margin-top:4px;width:auto">
        <span>${esc(d.title || '（未命名资料）')}
          <span style="color:var(--ink-3)">· ${esc(d.origin || '来源未标注')} · ${esc(String(d.chars ?? 0))} 字${
            chunks ? ' · ' + chunks : ''} · ${esc(d.added_at || '')}</span>
        </span>
      </label>
      <button class="ghost" type="button" data-del-doc="${esc(d.doc_id)}"
              style="flex:none;padding:2px 8px;font-size:12px">删</button>
    </div>`;
  }).join('');
  // 删除用事件委托：清单每次重渲染，逐个绑定会随条数线性增长。
  host.querySelectorAll('[data-del-doc]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      deleteKbDoc(btn.dataset.delDoc, btn.closest('div').querySelector('span').textContent.trim());
    });
  });
  pqaPaintCount();
}

/** 删除一篇文献。**要确认**：它会连片段一起删掉，误点后没法撤销。 */
async function deleteKbDoc(docId, title) {
  if (!docId) return;
  const name = (title || docId).slice(0, 60);
  if (!confirm(`确定从知识库删除「${name}」？\n它会连同全部片段一起移除，无法撤销。`)) return;
  try {
    const data = await api(`/api/kb/doc?doc_id=${encodeURIComponent(docId)}`, { method: 'DELETE' });
    note($('pqaNote'), 'ok', `已删除「${esc(name)}」（移除 ${esc(String(data.removed_chunks ?? 0))} 个片段）。`);
    await loadPqaDocs();
  } catch (err) {
    note($('pqaNote'), 'err', '删除失败：' + esc(err.message));
  }
}

/* ---------------------------------------------------- ④ 四个功能页签 */
/** 页签切换。
 *
 *  为什么用 btn/ghost 两个已有类做选中态，而不是新写一套 tab 样式：
 *  本站的 `.btn` 与 `.ghost` 已经把主/次按钮的对比度、暗色与焦点环都调好了，
 *  再写一套 .tab 就是在维护第二份按钮视觉；而这里要的只是"选中的那个更显眼"。
 */
const RAG_PANES = ['search', 'sum', 'cmp', 'ask'];

//: 页签名 → 容器 id。**写成显式映射而不是拼字符串**：
//: 拼出来的 id（`ragPane` + 首字母大写）在源码里搜不到，改 HTML 时
//: 一旦把 id 改成别的写法，页签会静默地什么都不显示——而"点了没反应"
//: 是最难查的一类故障。写成表就能被 grep 到、也能被自检脚本核对。
const RAG_PANE_IDS = {
  search: 'ragPaneSearch',
  sum: 'ragPaneSum',
  cmp: 'ragPaneCmp',
  ask: 'ragPaneAsk',
};

function ragShowPane(which) {
  RAG_PANES.forEach((name) => {
    const pane = $(RAG_PANE_IDS[name]);
    if (pane) pane.classList.toggle('hidden', name !== which);
  });
  document.querySelectorAll('#ragTabs .rag-tab').forEach((btn) => {
    const on = btn.dataset.rag === which;
    btn.classList.toggle('btn', on);
    btn.classList.toggle('ghost', !on);
  });
}

function ragInitTabs() {
  document.querySelectorAll('#ragTabs .rag-tab').forEach((btn) => {
    btn.addEventListener('click', () => ragShowPane(btn.dataset.rag || 'search'));
  });
  const input = $('ragQuery');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runRagSearch(); }
    });
  }
  const btn = $('btnRagSearch');
  if (btn) btn.addEventListener('click', runRagSearch);
  const refresh = $('btnRagRefresh');
  if (refresh) refresh.addEventListener('click', () => { loadPqaDocs(); refreshPqaStatus(); });
}

/** 语义索引状态。**必须区分三种情况**，因为这直接决定用户该怎么写检索词：
 *  · hybrid → 可以说人话（查「组织支持」也能命中讲「职业倦怠」的段落）
 *  · 预热中 → 现在只吃字面，等 30 秒左右再试
 *  · 不可用 → 只吃字面，别再试"换个说法"
 *  把三种混成一句"支持语义检索"是最坏的做法：用户会按错的方式改查询词。 */
async function refreshEmbedStatus() {
  const host = $('ragEmbedStatus');
  const modeEl = $('ragSearchMode');
  if (!host && !modeEl) return;
  try {
    const data = await api('/api/kb/embed/status');
    const cov = data.coverage || {};
    const ready = !!data.ready;
    const warming = !!data.warming;
    const available = !!data.available;

    if (modeEl) {
      modeEl.innerHTML = ready
        ? '检索方式：<strong>混合</strong>（关键词 FTS5 + 语义向量 bge-small-zh，RRF 融合）'
        : (warming
          ? '检索方式：<strong>关键词</strong>（语义模型预热中…）'
          : '检索方式：<strong>关键词</strong>（FTS5）');
    }
    if (!host) return;

    const pending = Number(cov.pending || 0);
    const vectors = Number(cov.vectors || 0);
    let cls = ready ? 'ok' : (warming ? 'warn' : '');
    let msg = '';
    if (ready) {
      msg = `<strong>语义检索已就绪</strong>（${esc(data.model || '')}，${esc(String(data.dim || ''))} 维）。`
        + `已编码 <strong>${vectors}</strong> 个片段`
        + (pending ? `，还有 <strong>${pending}</strong> 个待编码` : '，全库已覆盖') + '。';
    } else if (warming) {
      msg = '<strong>语义模型正在后台预热</strong>（首次加载 torch 约 30 秒）。'
        + '这段时间检索走纯关键词，结果仍然可用；预热完成后自动升级，无需刷新。';
      cls = 'warn';
    } else if (!available) {
      msg = '<strong>语义检索不可用</strong>（本机缺少向量依赖）。检索走纯关键词：'
        + '能精确命中字面，但换个说法可能查不到。';
      cls = 'warn';
    } else {
      msg = '语义检索尚未启动。点下面的按钮启动（首次约 30 秒）。';
      cls = '';
    }
    host.innerHTML = `<div class="note ${cls}">${msg}</div>`
      + (available && !ready
        ? `<div class="row" style="margin-top:8px">
             <button class="ghost" id="btnEmbedWarm" type="button">启动语义索引</button>
           </div>` : '')
      + (ready && pending
        ? `<div class="row" style="margin-top:8px">
             <button class="ghost" id="btnEmbedBackfill" type="button">补齐 ${pending} 个片段的向量</button>
             <span style="font-size:12.5px;color:var(--ink-3)">本地编码，不花钱；一篇长论文约几秒</span>
           </div>` : '');

    const warm = $('btnEmbedWarm');
    if (warm) warm.addEventListener('click', async () => {
      warm.disabled = true; warm.textContent = '启动中…';
      try {
        await api('/api/kb/embed/warmup', { method: 'POST', body: {} });
        note($('pqaNote'), '', '语义索引正在后台预热，约 30 秒后自动生效；期间检索仍可用。');
        setTimeout(refreshEmbedStatus, 5000);
      } catch (err) {
        note($('pqaNote'), 'err', '启动语义索引失败：' + esc(err.message));
      } finally {
        warm.disabled = false; warm.textContent = '启动语义索引';
      }
    });

    const bf = $('btnEmbedBackfill');
    if (bf) bf.addEventListener('click', async () => {
      bf.disabled = true; bf.textContent = '编码中…';
      try {
        const r = await api('/api/kb/embed/backfill', { method: 'POST', body: {} });
        if (r.ok) {
          note($('pqaNote'), 'ok',
            `已补齐 ${esc(String(r.embedded ?? 0))} 个片段的向量。现在检索是混合模式。`);
        } else {
          note($('pqaNote'), 'err', '补齐失败：' + esc(r.error || '未知原因'));
        }
        await refreshEmbedStatus();
        await loadPqaDocs();
      } catch (err) {
        note($('pqaNote'), 'err', '补齐向量失败：' + esc(err.message));
      } finally {
        bf.disabled = false;
      }
    });
  } catch (err) {
    if (host) host.innerHTML = `<div class="note">读取语义索引状态失败：${esc(err.message)}</div>`;
    if (modeEl) modeEl.textContent = '检索方式：未知';
  }
}

/** 文档检索：**唯一不花钱的功能**。语义就绪时是混合检索（关键词 + 向量 + RRF），
 *  未就绪时退化为纯关键词——两种情况下都不调模型、不需要 Key。
 *  命中片段带来源与位置，可以直接当写作/开题的依据回查。 */
async function runRagSearch() {
  const query = ($('ragQuery') ? $('ragQuery').value : '').trim();
  if (!query) { note($('ragSearchNote'), 'warn', '请先填检索词。'); return; }
  const btn = $('btnRagSearch');
  btn.disabled = true; btn.textContent = '检索中…';
  note($('ragSearchNote'), '', '');
  $('ragSearchResult').innerHTML = '';
  try {
    const data = await api('/api/kb/search', {
      method: 'POST', body: { query, k: 8 },
    });
    renderRagSearch(data);
    const n = data.count || 0;
    const hybrid = data.mode === 'hybrid';
    note($('ragSearchNote'), n ? 'ok' : 'warn',
      n ? `命中 ${n} 个片段（${hybrid ? '混合检索：关键词 + 语义向量，RRF 融合' : '纯关键词检索：FTS5'}）。`
          + (hybrid
            ? '语义路能命中"说法不同但意思相同"的段落，所以换个说法也值得一试。'
            : '现在是关键词匹配，换个说法可能查不到——尽量用资料里真实出现的词。')
        : (hybrid
          ? '没有命中。换一个说法再试（混合模式对措辞更宽容），或确认资料已入库。'
          : '没有命中。关键词模式对措辞敏感，换个资料里真实出现的词再试。'));
  } catch (err) {
    note($('ragSearchNote'), 'err', '检索失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '检索';
  }
}

function renderRagSearch(data) {
  const hits = data.hits || [];
  const host = $('ragSearchResult');
  if (!hits.length) { host.innerHTML = ''; return; }
  host.innerHTML = `<table class="list" style="margin-top:10px">
    <tr><th style="width:34px">#</th><th>命中片段 · 出处</th><th style="width:70px">分数</th></tr>
    ${hits.map((h, i) => `<tr>
      <td>${i + 1}</td>
      <td style="font-size:12.5px">
        <strong>${esc(h.citation || h.title || '（未给出处）')}</strong>
        <div style="color:var(--ink-2);margin-top:3px">${esc(String(h.text || '').slice(0, 400))}${
          String(h.text || '').length > 400 ? '…' : ''}</div>
      </td>
      <td style="font-size:12px;color:var(--ink-3)">${esc(
        h.score != null ? Number(h.score).toFixed(2) : '—')}</td>
    </tr>`).join('')}
  </table>`;
}

/** 引用来源表。**这是回答里唯一能回查的部分**：citation 指向真实文献，
 *  key 是 PaperQA2 的上下文编号，片段截断显示（完整片段可能上千字）。 */
function pqaSourcesHtml(sources) {
  const rows = sources || [];
  if (!rows.length) return '';
  return `<h3 style="font-size:14px;margin:16px 0 6px">引用来源（${rows.length} 处）</h3>
    <table class="list">
      <tr><th style="width:38px">#</th><th>出处 · 片段</th></tr>
      ${rows.map((s) => `<tr>
        <td>${esc(String(s.index ?? ''))}</td>
        <td style="font-size:12.5px">
          <strong>${esc(s.citation || s.dockey || '（未给出处）')}</strong>
          <div style="color:var(--ink-2);margin-top:3px">${esc(String(s.text || '').slice(0, 300))}</div>
          <div style="color:var(--ink-3);font-size:11.5px;margin-top:3px">${esc(s.key || '')}${
            s.score != null ? ` · 相关度 ${esc(String(s.score))}` : ''}</div>
        </td>
      </tr>`).join('')}
    </table>`;
}

async function runPqaIndex() {
  const ids = pqaSelectedIds();
  if (!ids.length) { note($('pqaNote'), 'warn', '请先在上面的清单里勾选要处理的文献。'); return; }
  if (!LLM.ready) {
    note($('pqaNote'), 'err', '建立索引需要对每篇文献调用一次模型。' + PQA_NEED_KEY);
    return;
  }

  const btn = $('btnPqaIndex');
  btn.disabled = true; btn.textContent = '建立索引中…';
  note($('pqaNote'), '', '');
  // 建索引在后端是"跑完才返回"的（进度随结果一起回来），所以进度条只能表示
  // "在跑"而不是"跑到哪了"：给一个不到 1 的固定比例，完成后才推满。
  setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 0.2,
    `正在为 ${ids.length} 篇文献建立索引（每篇调一次模型，可能要几分钟）…`);

  try {
    const data = await api('/api/pqa/index', { method: 'POST', body: { doc_ids: ids, rebuild: false } });
    if (data.ok === false) {
      // 失败时把最后几条进度一并显示：子进程的输出是唯一能说明"卡在哪一篇"的线索。
      const tail = (data.progress || []).slice(-3).map(esc).join('<br>');
      note($('pqaNote'), 'err', esc(data.error || '建立索引失败。') +
        (tail ? `<br>最近的进度：<br>${tail}` : ''));
      hideProgress($('pqaProg'));
      return;
    }
    note($('pqaNote'), 'ok',
      `索引已建立：${esc(String(data.docs ?? ids.length))} 篇，用时 ${esc(String(data.seconds ?? '—'))} 秒。` +
      (data.note ? `<br>${esc(data.note)}` : ''));
    setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 1, '完成');
    setTimeout(() => hideProgress($('pqaProg')), 1200);
    await refreshPqaStatus();
  } catch (err) {
    note($('pqaNote'), 'err', '建立索引失败：' + esc(err.message));
    hideProgress($('pqaProg'));
  } finally {
    btn.disabled = false; btn.textContent = '建立索引（每篇调一次模型）';
    // 按钮要按"PaperQA2 是否可用"这个总开关恢复，而不是无条件启用——
    // 不可用时它本来就该是灰的。
    pqaSetActionsEnabled(PaperQA.available);
  }
}

async function runPqaAsk() {
  const ids = pqaSelectedIds();
  if (!ids.length) { note($('pqaNote'), 'warn', '请先在上面的清单里勾选要提问的文献。'); return; }
  const question = $('pqaQuestion').value.trim();
  if (question.length < 2) { note($('pqaNote'), 'err', '请填写要问的问题。'); return; }
  if (!LLM.ready) { note($('pqaNote'), 'err', PQA_NEED_KEY); return; }

  const btn = $('btnPqaAsk');
  const answerEl = $('pqaAnswer');
  btn.disabled = true; btn.textContent = '提问中…';
  note($('pqaNote'), '', '');
  $('pqaAnswerCard').classList.remove('hidden');
  answerEl.textContent = '';
  $('pqaAnswerSources').innerHTML = '';
  setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 0.05, '正在检索证据…');

  let streamed = '';
  let failed = '';
  let finished = false;
  try {
    // 用 sse() 而不是自己再写一遍 fetch+getReader：它就是这套解析，
    // 且已经带上了 LLM.headers()（含 Content-Type: application/json，后端正是这么解析的）。
    await sse('/api/pqa/ask', { question, doc_ids: ids }, (ev) => {
      if (ev.type === 'config') {
        // 用的是谁的凭据必须说清楚：本站出 Key 时，这次问答花的是站主而不是访客的钱。
        note($('pqaNote'), '', `模型：${esc(ev.model || '')}${ev.note ? `　${esc(ev.note)}` : ''}`);
      } else if (ev.type === 'progress') {
        // 进度事件只给文字（PaperQA2 的步数不可预知），所以比例仍然只是"在跑"。
        setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 0.5, ev.message || '');
      } else if (ev.type === 'delta') {
        // 逐字上屏——**这就是走 SSE 的全部意义**，等跑完再显示等于把 40 秒的
        // 空白等待原样还给用户。这里刻意不每帧跑 md()：一次问答有几百个 delta，
        // 每次都重新解析整篇 markdown 是纯浪费，结束时整体渲染一次即可。
        streamed += ev.text || '';
        answerEl.textContent = streamed;
      } else if (ev.type === 'index') {
        setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 0.35,
          `索引就绪（${ev.docs ?? '?'} 篇，${ev.seconds ?? '?'} 秒${ev.reused ? '，复用缓存' : ''}）`);
      } else if (ev.type === 'sources') {
        $('pqaAnswerSources').innerHTML = pqaSourcesHtml(ev.sources || []);
      } else if (ev.type === 'done') {
        finished = true;
        // done.answer 才是最终口径（可能带引用键），流式文本只是它的过程；
        // 两者不一致时以 done 为准，避免屏幕上留着一份半成品。
        answerEl.innerHTML = md(ev.answer || streamed || '');
        note($('pqaNote'), 'ok',
          `回答完成（${esc(String(ev.seconds ?? '—'))} 秒${ev.reused ? '，索引复用缓存' : ''}` +
          `${ev.cost ? `，费用约 $${esc(String(ev.cost))}` : ''}）。` +
          '结论请对着下面的引用来源逐条回查——来源为空时，这个答案就没有可回查的依据。');
        setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 1, '完成');
        setTimeout(() => hideProgress($('pqaProg')), 1200);
      } else if (ev.type === 'error') {
        failed = String(ev.message || '提问失败');
        note($('pqaNote'), 'err', esc(failed));
      }
    });
  } catch (err) {
    note($('pqaNote'), 'err', '提问失败：' + esc(err.message));
  } finally {
    // 流被中途掐断（网络抖动 / 服务重启）时 done 永远不会到达。此时屏幕上的文字
    // 可能只有一半，必须说清楚——否则用户会把半截答案当成完整答案用。
    if (!finished) {
      // 一个字都没上屏时把卡片收回去：一张标题写着「PaperQA2 回答」、正文空白的卡片
      // 看起来像是坏了，而真正的原因已经写在下面的提示里。
      if (!streamed) $('pqaAnswerCard').classList.add('hidden');
      if (!failed) note($('pqaNote'), 'warn', '这次流没有正常结束（可能网络中断），上面的回答**可能不完整**，建议重新提问。');
      hideProgress($('pqaProg'));
    }
    btn.disabled = false; btn.textContent = '提问';
    pqaSetActionsEnabled(PaperQA.available);
  }
}

async function runPqaBrief(mode) {
  const ids = pqaSelectedIds();
  if (!ids.length) { note($('pqaNote'), 'warn', '请先在上面的清单里勾选要处理的文献。'); return; }
  // 对比综述至少要两篇：一篇文献没有"对比"可言。后端也会拒，这里先拦是为了省一个往返。
  if (mode === 'digest' && ids.length < 2) {
    note($('pqaNote'), 'err', '多篇对比综述至少要勾选 2 篇文献。'); return;
  }
  if (!LLM.ready) {
    note($('pqaNote'), 'err', (mode === 'digest' ? '生成对比综述' : '生成文献简报') + '需要模型：请先在右上角填入你自己的 API Key。');
    return;
  }

  const btn = $(mode === 'digest' ? 'btnPqaDigest' : 'btnPqaBrief');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '生成中…';
  note($('pqaNote'), '', '');
  // 简报是**逐节独立提问**、跑完才整体返回的（后端刻意不流式：半份简报没法用），
  // 所以进度同样只能表示"在跑"。
  setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 0.15,
    mode === 'digest'
      ? '正在逐节生成对比综述（每个小节都要检索一次，可能要几分钟）…'
      : '正在逐节生成文献简报（每个小节都要检索一次，可能要几分钟）…');

  try {
    const data = await api('/api/pqa/brief', {
      method: 'POST',
      body: { mode, doc_ids: ids, focus: $('pqaFocus').value.trim() },
    });
    if (data.ok === false) {
      const tail = (data.progress || []).slice(-3).map(esc).join('<br>');
      note($('pqaNote'), 'err', esc(data.error || '生成失败。') + (tail ? `<br>最近的进度：<br>${tail}` : ''));
      hideProgress($('pqaProg'));
      return;
    }
    renderPqaBrief(data, mode);
    setProgress($('pqaBar'), $('pqaMsg'), $('pqaProg'), 1, '完成');
    setTimeout(() => hideProgress($('pqaProg')), 1200);
  } catch (err) {
    note($('pqaNote'), 'err', '生成失败：' + esc(err.message));
    hideProgress($('pqaProg'));
  } finally {
    btn.disabled = false; btn.textContent = label;
    pqaSetActionsEnabled(PaperQA.available);
  }
}

/** 渲染简报。**两种返回口径都认**：逐节结果在 brief.sections 里（能区分"这一节失败"），
 *  而只透出整段 answer 的返回体也存在。只认一种的话，另一种会渲染成一片空白——
 *  后端契约在前端部署之后不会同步升级，这个分支是廉价的保险。 */
function renderPqaBrief(data, mode) {
  const card = $('pqaBriefCard');
  const brief = data.brief || null;
  const sections = (brief && brief.sections) || [];
  card.classList.remove('hidden');
  $('pqaBriefTitle').textContent = (brief && brief.title) ||
    (mode === 'digest' ? '多篇文献对比综述' : '文献简报');

  const cfg = data.config || {};
  const meta = [
    ['小节', brief ? `${brief.ok_sections ?? 0} / ${brief.total_sections ?? sections.length}` : '—'],
    ['文献', (data.docs || []).length],
    ['用时(秒)', data.seconds ?? '—'],
    ['费用($)', data.cost != null ? Number(data.cost).toFixed(4) : '—'],
  ].map(([k, v]) => `<div>${esc(k)}<b>${esc(String(v))}</b></div>`).join('');

  let body = `<div class="stat">${meta}</div>`;
  if (cfg.model || cfg.note) {
    body += `<div class="note">模型：${esc(cfg.model || '')}${cfg.note ? `　${esc(cfg.note)}` : ''}</div>`;
  }
  if (sections.length) {
    body += sections.map((s) => `
      <h3 style="font-size:14.5px;margin:16px 0 6px">${esc(s.section || '')}</h3>
      ${String(s.error || '').trim()
        // 单节失败必须显式写出来：后端逐节独立提问，某节失败时其余节照常返回。
        // 把失败节当空文本跳过，用户会以为这一节本来就"没什么可写"。
        ? `<div class="note err">这一节生成失败：${esc(s.error)}</div>`
        // 每一节也是同一个 formatted_answer 口径，所以同样要剥掉 Question 回声。
        : `<div>${md(s.text || '')}</div>`}`).join('');
  } else if (String(data.answer || '').trim()) {
    body += `<div>${md(data.answer)}</div>`;
  } else {
    body += '<div class="note warn">这次没有返回任何小节内容。</div>';
  }
  $('pqaBriefBody').innerHTML = body;
  $('pqaBriefSources').innerHTML = pqaSourcesHtml(data.sources || []);

  const okCount = sections.filter((s) => !String(s.error || '').trim() && String(s.text || '').trim()).length;
  note($('pqaNote'), 'ok', `简报已生成（${sections.length ? `${okCount}/${sections.length} 个小节有内容` : '整段渲染'}）。` +
    '每个小节都是模型读你勾选的文献写的，请对着下面的引用来源核对。');
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function clearPqaIndex() {
  if (!confirm('确定清空 PaperQA2 的索引缓存？（下次提问或建索引会重新对每篇文献调用一次模型）')) return;
  const btn = $('btnPqaClearIndex');
  btn.disabled = true; btn.textContent = '清空中…';
  try {
    const data = await api('/api/pqa/index', { method: 'DELETE' });
    note($('pqaNote'), 'ok', esc(data.note || `已清空索引缓存（${(data.removed || []).length} 项）。`));
    await refreshPqaStatus();
  } catch (err) {
    note($('pqaNote'), 'err', '清空索引失败：' + esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '清空索引缓存';
    pqaSetActionsEnabled(PaperQA.available);
  }
}

/** 进入「开题报告」面板时的刷新入口。知识库会在别处被改动（第 1 步的入库、
 *  「定题目」阶段的资料入库、别的会话），在这一处统一刷新，比在每个可能的
 *  改动点都埋一次刷新更不容易漏。 */
async function refreshPqaOnEnter() {
  if (!$('pqaStatus')) return;
  await refreshPqaStatus();
  await loadPqaDocs();
  // 语义索引状态也一起刷：它决定"检索能不能换个说法"，用户一进面板就该看到。
  refreshEmbedStatus().catch(() => {});
}

/** 第 1 / 2 步的接线。**集中在一个函数里、由 boot() 调用**，
 *  而不是像早期那样在文件末尾裸写 $('x').addEventListener(...)：
 *  裸写的绑定在脚本解析时就执行，只要有一个 id 被删掉就是一句 TypeError，
 *  后面的绑定全都装不上。这里每个都判存在，坏一个不影响其余的。 */
function initRefsPqa() {
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };

  on('btnRefsRun', 'click', runRefs);
  on('btnRefsExportGbt', 'click', () => exportRefs('gbt'));
  on('btnRefsExportBib', 'click', () => exportRefs('bibtex'));
  on('btnRefsExportMd', 'click', () => exportRefs('markdown'));
  on('btnRefsToKb', 'click', refsToKb);

  on('btnPqaAll', 'click', () => pqaSelectAll(true));
  on('btnPqaNone', 'click', () => pqaSelectAll(false));
  // 复选框是整块 innerHTML 重渲染出来的，所以计数用事件委托挂在容器上；
  // 逐个 checkbox 绑监听的话，一次重渲染就让那些引用全部失效。
  const docsHost = $('pqaDocs');
  if (docsHost) {
    docsHost.addEventListener('change', (e) => {
      if (e.target && e.target.classList && e.target.classList.contains('pqaDoc')) pqaPaintCount();
    });
  }
  on('btnPqaIndex', 'click', runPqaIndex);
  on('btnPqaAsk', 'click', runPqaAsk);
  on('btnPqaBrief', 'click', () => runPqaBrief('brief'));
  on('btnPqaDigest', 'click', () => runPqaBrief('digest'));
  on('btnPqaClearIndex', 'click', clearPqaIndex);

  // 论文RAG知识库的四个功能页签 + 文档检索 + 刷新。
  ragInitTabs();
}

/* ================================================================== ③ 写作 */
const Write = { draft: null, plan: null, material: null, importedFiles: [], importing: false };

/* ---- 入口 A：导入 Word / PDF，直接生成初稿 ----
   与「开题报告」阶段的上传是**两条不同的路**，刻意不合并：
     · 那边导入的是**别人的文献**，目标是抽成可引用、带 DOI 的证据；
     · 这边导入的是**作者自己的稿子/要求**，目标是读出题目与结构，并把正文当依据。
   两件事的数据流与失败模式都不同，共用一个入口只会让两边都别扭。 */
async function importWritingFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const zone = $('wDrop');
  zone.classList.add('busy');
  Write.importing = true;
  note($('writeNote'), '', '');
  setProgress($('importBar'), $('importMsg'), $('importProg'), 0.15,
    `正在读取 ${files.length} 个文件…`);

  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));
  // sid 走查询参数：handler 那里用同一个约定（也便于 agent 侧复用）。
  const qs = SID.get() ? '?sid=' + encodeURIComponent(SID.get()) : '';

  try {
    const resp = await fetch('/api/writing/import-plan' + qs, { method: 'POST', body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));

    SID.set(data.sid);
    Write.plan = data.plan || {};
    Write.material = data.material || {};
    Write.importedFiles = data.files || [];
    renderWriteImport(data);

    // 把读出来的计划**填进输入框**，而不是只显示一遍。计划是给人改的：
    // 填进去作者就能直接编辑，不用对着只读结果重新手打一遍。
    if (Write.plan.title) $('wTitle').value = Write.plan.title;
    if ((Write.plan.sections || []).length) $('wSections').value = Write.plan.sections.join('\n');
    if (Write.plan.requirements) $('wRequirements').value = Write.plan.requirements;
    if (Write.plan.genre) {
      const opt = Array.from($('wGenre').options).find((o) => o.value === Write.plan.genre);
      if (opt) $('wGenre').value = Write.plan.genre;
    }
    $('outlineBox').innerHTML = '';

    const m = Write.material || {};
    const bits = [
      `解析成功 ${data.parsed} 个文件`,
      m.blocks ? `正文切成 ${m.blocks} 段依据（约 ${m.chars} 字）` : '没有可用的正文依据',
      (Write.plan.sections || []).length ? `${Write.plan.sections.length} 章结构` : '没读出章节结构，请手工填',
    ];
    note($('writeNote'), data.failed ? 'warn' : 'ok',
      bits.join('；') + '。<br>' +
      (Write.plan.source === 'model'
        ? '计划是<strong>模型读文档</strong>得出的，已填入下方——<strong>请核对后再生成</strong>。'
        : '<strong>模型没有参与</strong>：这份计划由程序按文档结构推导，题名与结构务必自己核对。') +
      '<br>改完直接点 <strong>「逐章生成」</strong>。');
    setProgress($('importBar'), $('importMsg'), $('importProg'), 1, '解析完成');
    setTimeout(() => hideProgress($('importProg')), 1200);
    Progress.markDone('proposal');
  } catch (err) {
    note($('writeNote'), 'err', '导入失败：' + esc(err.message));
    hideProgress($('importProg'));
  } finally {
    zone.classList.remove('busy');
    Write.importing = false;
    $('wFiles').value = '';       // 允许重复选择同一个文件
  }
}

function renderWriteImport(data) {
  const el = $('wFileList');
  const rows = data.files || [];
  const plan = data.plan || {};
  const m = data.material || {};
  if (!rows.length) { el.innerHTML = ''; return; }

  const notes = (data.notes || []).length
    ? `<div class="note warn" style="margin-top:10px">${data.notes.map(esc).join('<br>')}</div>`
    : '';
  const planNotes = (plan.notes || []).length
    ? `<div class="note" style="margin-top:10px">模型自述的推断：${plan.notes.map(esc).join('；')}</div>`
    : '';

  el.innerHTML = `<div class="stat" style="margin-top:14px">
      <div>收到文件<b>${data.parsed + (data.failed || 0)}</b></div>
      <div>解析成功<b>${data.parsed}</b></div>
      <div>失败<b>${data.failed || 0}</b></div>
      <div>依据块<b>${m.blocks || 0}</b></div>
      <div>依据字数<b>${m.chars || 0}</b></div>
    </div>
    <table class="list">
      <tr><th>文件</th><th style="width:88px">类型</th><th style="width:74px">字数</th>
          <th style="width:60px">页数</th><th>状态</th></tr>
      ${rows.map((f) => {
        let cls = '就绪', kls = '';
        if (f.error) { cls = esc(f.error); kls = 'bad'; }
        else if (f.warning) { cls = esc(f.warning); kls = 'warn'; }
        else if (!f.ok) { cls = '没有取到文字'; kls = 'warn'; }
        return `<tr>
          <td><strong>${esc(f.filename)}</strong></td>
          <td><span class="fkind ${kls}">${esc(f.kind || '?')}</span></td>
          <td>${f.chars || 0}</td>
          <td>${f.pages || '—'}</td>
          <td style="font-size:12.5px">${cls}</td>
        </tr>`;
      }).join('')}
    </table>${notes}${planNotes}`;
}

function bindWriteDropzone() {
  const zone = $('wDrop');
  const input = $('wFiles');
  if (!zone || !input) return;

  input.addEventListener('change', (e) => importWritingFiles(e.target.files));
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) importWritingFiles(e.dataTransfer.files);
  });
  // 点击拖拽区就打开文件选择框（原生 input 视觉上被藏起来了）。
  // 要判 e.target：input 自己就在 zone 里面，它的 click 会冒泡上来，
  // 不挡一下就会变成"递归打开选择框"。
  zone.addEventListener('click', (e) => { if (e.target !== input) input.click(); });
}

/* 从上一次导入里取写作要求（模型从文档里读出来的那份），供提示词使用。 */
function importRequirements() {
  return ($('wRequirements') ? $('wRequirements').value.trim() : '');
}

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
  // 导入还在解析时点"逐章生成"，如果不拦，会在"计划还没填进输入框"的状态下开写——
  // 结果是按旧题目写一篇必然要返工的稿子。明确挡住并说清原因。
  if (Write.importing) {
    note($('writeNote'), 'warn', '文件还在解析中，请等它读完（计划填进上方输入框后）再生成。');
    return;
  }
  const title = $('wTitle').value.trim();
  if (!title) { note($('writeNote'), 'err', '请先填写论文题目。'); return; }
  if (!LLM.ready) { note($('writeNote'), 'err', '写作需要模型：请先在右上角填入你自己的 API Key。'); return; }

  const btn = $('btnDraft');
  btn.disabled = true; btn.textContent = '生成中…';
  note($('writeNote'), '', '');
  $('writeResultCard').classList.add('hidden');
  setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 0.02, '正在检查依据库…');

  const sections = $('wSections').value.split('\n').map((s) => s.trim()).filter(Boolean);
  const body = {
    sid: SID.get(), title, genre: $('wGenre').value, sections, provider: LLM.provider, mode: 'auto',
    use_web: $('wUseWeb').checked,
    requirements: importRequirements(),
  };

  try {
    await sse('/api/writing/draft', body, (ev) => {
      if (ev.type === 'stage') {
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), ev.progress || 0, ev.message || '');
        // 联网检索与降级提示要显眼：前者说明依据从哪来，后者说明依据变薄了。
        if (ev.stage === 'warn') note($('writeNote'), 'warn', esc(ev.message));
        else if (ev.stage === 'search') note($('writeNote'), '', esc(ev.message));
      } else if (ev.type === 'section' && ev.partial) {
        // 逐章流式预览。注意要判 ev.partial：单章重写也会发 section 事件，
        // 那个载荷里没有 partial，误当成整篇渲染会把别的章节一起冲掉。
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
   而是**作者以为每一句都有依据**。所以把"用了多少 D/A/B 层、有没有编造引用、
   有没有找不到依据的数字"直接摆在稿件上方，而不是埋在日志里。

   D 层（导入的文档）单独列一行而不是并进"你的资料"：作者需要一眼看出
   "这一章是照着我上传的稿子写的，还是助手自己找的文献写的"——两者的可信度
   与需要核对的程度完全不同。 */
function renderCoverage(cov, notice) {
  const el = $('writeCoverage');
  if (!cov) { el.innerHTML = ''; return; }
  const bad = (cov.citations_invalid || 0) + (cov.ungrounded_numbers || 0);
  // "用了材料但没挂编号"是另一类问题：内容有依据，但读者无从回查。
  // 它既不该算进"编造"（不是编造），也不能不提（提示词里明确要求挂编号）。
  const uncited = cov.uncited_evidence_sections || 0;
  let head = bad
    ? `<div class="note warn"><strong>有 ${bad} 处需要人工核对</strong>——见下方明细与生成提示。</div>`
    : `<div class="note ok">引用编号与数字都能在依据池里找到，未发现编造痕迹。</div>`;
  if (uncited) {
    head += `<div class="note warn"><strong>有 ${uncited} 章用了材料里的数据但没挂引用编号</strong>——
      内容不是凭空写的，但读者无法回查。请在稿件里补上 [n]，或点该章的「重写本章」并要求"标注出处"。</div>`;
  }

  const docTotal = cov.doc_blocks_total ?? cov.doc_blocks ?? 0;
  const docUsed = cov.doc_blocks ?? 0;
  const rows = [
    ['引用总数', cov.citations_total ?? 0],
    ['D 导入文档', cov.citations_doc ?? 0],
    ['A 你的资料', cov.citations_user ?? 0],
    ['B 联网文献', cov.citations_web ?? 0],
    ['编造的引用编号', cov.citations_invalid ?? 0],
    ['找不到依据的数字', cov.ungrounded_numbers ?? 0],
    // 用了几条 / 文档总共有几条。只报一个数会让作者以为整篇文档都参与了写作。
    ['文档依据块', docTotal > docUsed ? `${docUsed} / ${docTotal}` : docUsed],
    ['检索到的真实文献', cov.web_records_retrieved ?? 0],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  const sources = (cov.web_sources || []).join('、') || '无';
  const partial = docTotal > docUsed
    ? `<div class="note warn">导入文档共 ${docTotal} 段依据，材料表只放进了前 ${docUsed} 段。
         要覆盖更多内容，可把关键章节单独导一次，或用下面的「重写本章」逐章补写。</div>`
    : '';
  el.innerHTML = head +
    `<div class="stat">${rows}</div>` +
    `<div style="font-size:13px;color:var(--ink-2);margin-bottom:6px">
       依据构成：<strong>${esc(cov.verdict || '未知')}</strong>
       ${cov.web_records_retrieved ? `（检索源：${esc(sources)}）` : ''}
     </div>` +
    partial +
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
    ['导入依据块', (draft.scope || {}).doc_blocks ?? 0],
    ['联网文献', draft.web_records_total ?? 0],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join('');

  renderCoverage(coverage || draft.coverage, notice || draft.notice);

  $('writeBody').innerHTML = sectionHeading(draft.title) +
    sections.map((s, i) => headingWithCite(s, i) + `<div>${esc(s.body || '')}</div>`).join('');
}

/* 每章标题后面挂两个东西：
   1. 一个小徽标，显示这一章各自用了多少依据——整篇的覆盖度看不出"哪一章是凭记忆写的"；
   2. 一个「重写本章」按钮（按需单独生成某一章就是从这里进去的）。 */
function headingWithCite(section, index) {
  const c = section.citations;
  let tags = '';
  if (c) {
    const bits = [];
    if (c.doc) bits.push(`文档 ${c.doc}`);
    if (c.user) bits.push(`资料 ${c.user}`);
    if (c.web) bits.push(`联网 ${c.web}`);
    if (!bits.length) bits.push('无引用依据');
    const cls = c.invalid ? 'bad' : (c.total ? 'ok' : 'warn');
    tags = `<div class="tags"><span class="tag ${cls}">${esc(bits.join(' · '))}</span>` +
      (c.invalid ? `<span class="tag bad">编造引用 ${c.invalid}</span>` : '') +
      ((section.grounding && section.grounding.ungrounded_count)
        ? `<span class="tag warn">无依据数字 ${section.grounding.ungrounded_count}</span>` : '') +
      ((section.uncited_evidence && section.uncited_evidence.suspected_uncited)
        ? '<span class="tag warn">用了材料但没挂编号</span>' : '') +
      (section.regenerated ? '<span class="tag ok">已单独重写</span>' : '') +
      '</div>';
  }
  const tools = `<div class="section-tools">
      <button class="ghost mini" data-sr="open" data-si="${index}" type="button">重写本章</button>
    </div>
    <div class="rewrite-box hidden" id="rwBox${index}">
      <label class="f" for="rwTask${index}">这一章要怎么改（可留空：留空表示按原要求重写一遍）</label>
      <textarea id="rwTask${index}" rows="2"
        placeholder="例如：补上抽样方法的说明；把第 2 段那三个百分比改掉，我在材料里找不到出处；结论不要引入新数据"></textarea>
      <div class="rw-row">
        <label class="rw-check">
          <input type="checkbox" id="rwWeb${index}" checked> 允许联网检索真实文献
        </label>
        <button class="btn mini" data-sr="run" data-si="${index}" type="button">重写这一章</button>
        <button class="ghost mini" data-sr="close" data-si="${index}" type="button">收起</button>
        <span class="rw-msg" id="rwMsg${index}"></span>
      </div>
    </div>`;
  return sectionHeading(section.heading) + tags + tools;
}

/* ---- 按需重写单章 ----
   只重写被点名的那一章，其余章节原样保留；结果会推成一个新版本（旧版可回退）。
   引用编号与数字接地检查与整篇生成走同一套代码，所以局部重写不是"绕过检查的捷径"。 */
async function runSectionRewrite(index) {
  if (!LLM.ready) { note($('writeNote'), 'err', '重写章节需要模型：请先填入 API Key。'); return; }
  const draft = Write.draft;
  if (!draft || !(draft.sections || [])[index]) {
    note($('writeNote'), 'err', '还没有这一章的内容，请先生成初稿。'); return;
  }
  const target = draft.sections[index];
  const heading = String(target.heading || '');
  const task = ($('rwTask' + index) || {}).value || '';
  const useWeb = !!($('rwWeb' + index) || {}).checked;
  const msgEl = $('rwMsg' + index);

  const buttons = document.querySelectorAll(`[data-sr="run"][data-si="${index}"]`);
  buttons.forEach((b) => { b.disabled = true; b.textContent = '重写中…'; });
  if (msgEl) msgEl.textContent = '正在重写…';
  note($('writeNote'), '', '');
  setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 0.05, `正在重写「${heading}」…`);

  try {
    await sse('/api/writing/section', {
      sid: SID.get(), title: $('wTitle').value.trim() || draft.title || '',
      index, heading, task, use_web: useWeb,
      requirements: $('wRequirements') ? $('wRequirements').value.trim() : '',
    }, (ev) => {
      if (ev.type === 'stage') {
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), ev.progress || 0, ev.message || '');
        if (msgEl) msgEl.textContent = ev.message || '';
      } else if (ev.type === 'result') {
        SID.set(ev.sid);
        // 只替换被重写的那一章，其余章节保持原样：这才是"单独生成某一部分"的含义。
        const sections = (draft.sections || []).map((s) => Object.assign({}, s));
        const next = (ev.draft || {}).sections || [];
        if (next[index]) sections[index] = next[index];
        Write.draft = Object.assign({}, draft, {
          sections, words: sections.reduce((n, s) => n + String(s.body || '').length, 0),
        });
        renderDraft(Write.draft, ev.coverage, ev.notice);
        setProgress($('writeBar'), $('writeMsg'), $('writeProg'), 1, '完成');
        setTimeout(() => hideProgress($('writeProg')), 1200);
        const cov = ev.coverage || {};
        const bad = (cov.citations_invalid || 0) + (cov.ungrounded_numbers || 0);
        note($('writeNote'), bad ? 'warn' : 'ok',
          `「${esc(ev.rewritten || heading)}」已重写（第 ${ev.version_count} 版，旧版可回退）。` +
          (bad ? `全篇仍有 <strong>${bad} 处</strong>引用或数字需要人工核对，明细见稿件上方。` : ''));
      } else if (ev.type === 'error') {
        note($('writeNote'), 'err', esc(ev.message));
        if (msgEl) msgEl.textContent = '失败';
      }
    });
  } catch (err) {
    note($('writeNote'), 'err', '重写失败：' + esc(err.message));
    if (msgEl) msgEl.textContent = '失败';
  } finally {
    buttons.forEach((b) => { b.disabled = false; b.textContent = '重写这一章'; });
  }
}

/* 章节工具条的点击委托。渲染是整块 innerHTML 替换的，所以监听必须挂在容器上。 */
function bindSectionTools() {
  const host = $('writeBody');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const el = e.target.closest('[data-sr]');
    if (!el) return;
    const i = el.dataset.si;
    const kind = el.dataset.sr;
    if (kind === 'open') {
      const box = $('rwBox' + i);
      if (box) box.classList.toggle('hidden');
      if (box && !box.classList.contains('hidden')) {
        const t = $('rwTask' + i);
        if (t) t.focus();
      }
    } else if (kind === 'close') {
      const box = $('rwBox' + i);
      if (box) box.classList.add('hidden');
    } else if (kind === 'run') {
      runSectionRewrite(Number(i));
    }
  });
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

  bindWriteDropzone();
  bindSectionTools();
  injectNextButtons();
  // 「开题报告」阶段新增的两步（参考文献推荐 / PaperQA2）的接线。
  initRefsPqa();
  // 知识库导入（阶段 2）与开题报告生成（阶段 3）的接线。
  initProposalStage();
  // agent 状态轮询：页面一打开就启动，让"谁在干活"这件事始终可见。
  AgentPanel.start();

  LLM.load();
  // 先问部署形态：本站有没有配 Key、要不要访客自填。
  // 必须**先于** paint()，否则会先按"需要填 Key"渲染一次再跳变。
  try {
    const dep = await api('/api/deployment');
    LLM.serverSide = !!(dep.server_key || {}).enabled;
    LLM.serverMasked = (dep.server_key || {}).masked || '';
    LLM.billingNote = dep.billing_note || '';
    if (LLM.serverSide) {
      const k = $('apiKey');
      if (k) k.placeholder = '想用自己的 Key？填在这里（可选）';
    }
  } catch (_) { /* 读不到就按 BYOK 处理，功能不受影响 */ }

  try {
    await topicOptions();
  } catch (err) {
    note($('topicNote'), 'err', '无法读取服务配置：' + esc(err.message));
  }
  $('provider').value = LLM.provider;
  LLM.paint();
  try { await writingOptions(); } catch (_) { /* 写作选项失败不阻塞其它页签 */ }


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

// ⑤ 查重与降 AIGC 率
$('btnIntegrityDup') && $('btnIntegrityDup').addEventListener('click', runIntegrityDup);
$('btnAigcScore') && $('btnAigcScore').addEventListener('click', runAigcScore);
$('btnAigcReduce') && $('btnAigcReduce').addEventListener('click', runAigcReduce);
$('btnAigcDocx') && $('btnAigcDocx').addEventListener('click', exportAigcDocx);

// ⑥ 答辩
$('btnDefenseScript') && $('btnDefenseScript').addEventListener('click', runDefenseScript);
$('btnDefenseSlides') && $('btnDefenseSlides').addEventListener('click', runDefenseSlides);
$('btnDefensePptx') && $('btnDefensePptx').addEventListener('click', () => exportDefense('pptx'));
$('btnDefenseHtml') && $('btnDefenseHtml').addEventListener('click', () => exportDefense('html'));
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
  const topicNote = $('p-gather') ? $('p-gather').querySelector('.note') : null;
  if (topicNote) {
    note(topicNote, 'ok',
      `已选定题目：<strong>${esc(title)}</strong>` +
      '<br>这一步先找文献、并把 PDF / Word 资料导入知识库；' +
      '备齐后到左侧「生成开题报告」阶段成稿。');
  }
});

/* ============================================ 知识库 · 导入资料（阶段 2）
   与「写初稿」阶段的导入是**两条不同的路**，刻意不合并：
     · 这边入库的是"别人的文献/资料"，目标是可检索、可被引用，供开题报告与简报取用；
     · 那边导入的是"作者自己的稿子"，目标是读出结构与要求，只服务这一次写作。

   **一次能导入多少**：后端单批上限 25 个文件，但前端会把用户选的任意多个文件
   拆成每批 8 个依次上传。所以"一次最多 10 个"那种硬上限没有了——
   整批 200 篇也能一次选、一次拖，进度条按"已完成 / 总数"走。 */

//: 每批上传几个文件。8 是个折中：批太小则请求数多（200 篇 = 25 个请求），
//: 批太大则单个请求在慢盘 / 大 PDF 上容易顶到网关超时，且中途失败要重传很多。
const KB_BATCH_SIZE = 8;

//: 前端接受的文件后缀（与后端 fileintake 支持的对齐）。
const KB_ACCEPT = '.pdf,.docx,.doc,.md,.markdown,.txt,.text,.html,.htm,.csv,.tsv,.json,.bib';

async function importKbFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const zone = $('kbDrop');
  const input = $('kbFiles');
  if (!zone || !input) return;

  zone.classList.add('busy');
  note($('kbNote'), '', '');
  $('kbImportResult').innerHTML = '';

  const allAdded = [];
  const allFailed = [];
  let done = 0;
  let lastStats = null;

  const batches = [];
  for (let i = 0; i < files.length; i += KB_BATCH_SIZE) {
    batches.push(files.slice(i, i + KB_BATCH_SIZE));
  }

  try {
    for (let b = 0; b < batches.length; b += 1) {
      const batch = batches[b];
      setProgress($('kbBar'), $('kbMsg'), $('kbProg'),
        Math.max(0.02, done / files.length),
        `正在入库第 ${b + 1}/${batches.length} 批（已完成 ${done}/${files.length} 个文件）…`);

      const fd = new FormData();
      batch.forEach((f) => fd.append('files', f, f.name));

      let data = null;
      try {
        const resp = await fetch('/api/kb/upload', { method: 'POST', body: fd });
        data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(data.detail || ('HTTP ' + resp.status));
        }
      } catch (err) {
        // 整批被拒（本批累计过大 / 某个文件读取失败）时把这一批都记为失败，
        // 但**不中断后面的批次**：用户选了 200 篇，不该因为其中一批出问题就全丢。
        batch.forEach((f) => allFailed.push({ filename: f.name, error: err.message }));
        done += batch.length;
        continue;
      }

      (data.added || []).forEach((x) => allAdded.push(x));
      (data.failed || []).forEach((x) => allFailed.push(x));
      lastStats = data.stats || lastStats;
      done += batch.length;
    }

    setProgress($('kbBar'), $('kbMsg'), $('kbProg'), 1, '入库完成');
    renderKbImport({ added: allAdded, failed: allFailed });

    const okCount = allAdded.length;
    const failCount = allFailed.length;
    const stats = lastStats || {};
    let msg = `本次共选 ${files.length} 个文件`;
    if (batches.length > 1) msg += `（分 ${batches.length} 批上传）`;
    msg += `：成功入库 ${okCount} 篇`;
    if (failCount) msg += `，失败 ${failCount} 个（见下表）`;
    msg += `。知识库现有 ${stats.documents ?? '—'} 篇文献。`;
    if (!failCount) msg += ' 现在可以在「② 文档库」里勾选它们做检索、简报、对比或问答。';
    note($('kbNote'), failCount ? 'warn' : 'ok', msg);

    await loadPqaDocs();            // 清单立即刷新，用户能马上看到刚导入的东西
    setTimeout(() => hideProgress($('kbProg')), 1500);
  } finally {
    zone.classList.remove('busy');
    input.value = '';               // 允许重复选同一个文件
  }
}

function renderKbImport(data) {
  const el = $('kbImportResult');
  const added = data.added || [];
  const failed = data.failed || [];
  if (!added.length && !failed.length) { el.innerHTML = ''; return; }

  // 只画前 40 行：一次导入 200 篇时把全部行都塞进 DOM 会让页面卡住，
  // 而剩下的已经进了文档库，那里能看到全量。
  const LIMIT = 40;
  const rows = [
    ...added.map((a) => ({
      f: a.filename, kind: a.kind, chars: a.chars, chunks: a.chunks,
      pages: a.page_map || 0, err: '',
    })),
    ...failed.map((f) => ({
      f: f.filename, kind: '—', chars: '', chunks: '', pages: 0, err: f.error,
    })),
  ];
  const shown = rows.slice(0, LIMIT);
  el.innerHTML = `<table class="list" style="margin-top:12px">
      <tr><th>文件</th><th style="width:86px">类型</th><th style="width:82px">字数</th>
          <th style="width:70px">片段</th><th style="width:74px">页映射</th><th>状态</th></tr>
      ${shown.map((r) => `<tr>
        <td><strong>${esc(r.f)}</strong></td>
        <td><span class="fkind${r.err ? ' bad' : ''}">${esc(String(r.kind || '?'))}</span></td>
        <td>${esc(String(r.chars ?? ''))}</td>
        <td>${esc(String(r.chunks ?? ''))}</td>
        <td>${r.err ? '' : esc(String(r.pages || '—'))}</td>
        <td style="font-size:12.5px">${r.err ? esc(r.err) : '已入库'}</td>
      </tr>`).join('')}
    </table>
    ${rows.length > LIMIT
      ? `<div style="font-size:12.5px;color:var(--ink-3);margin-top:6px">
           仅显示前 ${LIMIT} 条；其余 ${rows.length - LIMIT} 条已入库，可在「② 文档库」查看全量。</div>`
      : ''}`;
}

/** 批量删除选中的文献。**删之前确认并报出篇数**——它连片段一起删、无法撤销。 */
async function deleteSelectedKbDocs() {
  const ids = pqaSelectedIds();
  if (!ids.length) {
    note($('pqaNote'), 'warn', '先在「② 文档库」里勾选要删除的文献。');
    return;
  }
  if (!confirm(`确定从知识库删除选中的 ${ids.length} 篇文献？\n会连同全部片段一起移除，无法撤销。`)) return;
  await deleteKbDocsByIds(ids);
}

/** 清空整个知识库。**要求手输「删除」二字**，避免手滑把几个月的入库清空。 */
async function clearKbAll() {
  const total = (PaperQA.docs || []).length;
  if (!total) { note($('pqaNote'), 'warn', '知识库已经是空的。'); return; }
  const typed = prompt(
    `这会删除知识库里全部 ${total} 篇文献（连同全部片段），无法撤销。\n` +
    '如果只是想删掉几篇，请用「删除选中」。\n\n确认请回复：删除');
  if (typed === null) return;
  if (String(typed).trim() !== '删除') {
    note($('pqaNote'), 'warn', '已取消：确认文字不匹配。');
    return;
  }
  try {
    const data = await api('/api/kb/all', { method: 'DELETE' });
    note($('pqaNote'), 'ok', `已清空知识库（移除 ${data.removed_documents ?? 0} 篇）。`);
    await loadPqaDocs();
    await refreshPqaStatus();
  } catch (err) {
    note($('pqaNote'), 'err', '清空失败：' + esc(err.message));
  }
}

/** 批量删除的执行体。与单篇删除分两个函数：一个走幂等的批量接口，
 *  一个走单篇接口，**不要混用**——批量接口会如实回报 missing，单篇接口不会。 */
async function deleteKbDocsByIds(ids) {
  const btn = $('btnRagDeleteSel');
  if (btn) { btn.disabled = true; btn.textContent = '删除中…'; }
  try {
    const data = await api('/api/kb/delete-batch', {
      method: 'POST', body: { doc_ids: ids },
    });
    const removed = (data.removed || []).length;
    const missing = (data.missing || []).length;
    let msg = `已删除 ${removed} 篇（移除 ${data.removed_chunks ?? 0} 个片段）。`;
    if (missing) msg += ` 另有 ${missing} 篇在库里找不到（可能已被删除）。`;
    note($('pqaNote'), 'ok', msg);
    await loadPqaDocs();
    await refreshPqaStatus();
  } catch (err) {
    note($('pqaNote'), 'err', '批量删除失败：' + esc(err.message));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '删除选中'; }
  }
}

function bindKbDropzone() {
  const zone = $('kbDrop');
  const input = $('kbFiles');
  if (!zone || !input) return;

  input.addEventListener('change', (e) => importKbFiles(e.target.files));
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files) importKbFiles(e.dataTransfer.files);
  });
  zone.addEventListener('click', (e) => { if (e.target !== input) input.click(); });

  // 「选择文件夹」：webkitdirectory 让浏览器把整个目录下的文件一次交上来，
  // 再走同一套分批上传。收论文目录时这才是真省事（一次几百个 PDF）。
  const folder = $('kbPickFolder');
  if (folder) folder.addEventListener('click', () => {
    const fp = $('kbFilesDir');
    if (fp) fp.click();
  });
  const dirInput = $('kbFilesDir');
  if (dirInput) dirInput.addEventListener('change', (e) => {
    // 目录里常混着 .DS_Store、缩略图、.caj 之类，先按后缀过一遍。
    const ok = Array.from(e.target.files || []).filter((f) => {
      const n = (f.name || '').toLowerCase();
      return KB_ACCEPT.split(',').some((ext) => n.endsWith(ext));
    });
    if (!ok.length) {
      note($('kbNote'), 'warn',
        '这个文件夹里没有可入库的文件（支持 PDF / Word / Markdown / 文本 / HTML / CSV）。');
      return;
    }
    importKbFiles(ok);
  });

  const sel = $('btnRagDeleteSel');
  if (sel) sel.addEventListener('click', deleteSelectedKbDocs);
  const clr = $('btnRagClearAll');
  if (clr) clr.addEventListener('click', clearKbAll);
}
/* ============================================ 生成开题报告（阶段 3）
   依据来自知识库（上个阶段导入的资料）+ 已录入证据。
   参考文献**不由模型写**：后端按正文实际引用机械汇总，所以列表里不会有编造的条目。 */

const Prop = { draft: null, sections: [], running: false };

async function proposalOptions() {
  try {
    const data = await api('/api/proposal/options');
    Prop.sections = data.sections || [];
  } catch (_) {
    Prop.sections = [];
  }
  const ta = $('propSections');
  if (ta && !ta.value.trim() && Prop.sections.length) ta.value = Prop.sections.join('\n');
  if ($('propTitle') && !$('propTitle').value.trim()) {
    // 题目优先沿用「写初稿」里已填的，其次是最近一次选中的题目。
    const fromWrite = $('wTitle') ? $('wTitle').value.trim() : '';
    if (fromWrite) $('propTitle').value = fromWrite;
    else if (Write && Write.chosenTitle) $('propTitle').value = Write.chosenTitle;
  }
  await refreshProposalStatus();
}

async function refreshProposalStatus() {
  const host = $('propStatus');
  if (!host) return;
  try {
    const data = await api('/api/proposal/options');
    const kb = data.kb || {};
    const docs = kb.documents || 0;
    const chunks = kb.chunks || 0;
    // 提示里必须点明"还有联网检索这一层"：知识库为空**不等于**只能靠模型瞎写，
    // 早先那句"只能依据模型自身知识成稿"会让用户以为这一篇没有任何可回查的依据，
    // 于是去导一堆 PDF——而其实开着联网检索就已经有真实文献了。
    const webOn = $('propUseWeb') ? $('propUseWeb').checked : true;
    if (docs) {
      host.innerHTML = `<div class="note ok">知识库里有 <strong>${docs}</strong> 篇资料、<strong>${chunks}</strong> 条片段，
           本次生成会以它们为主要依据${webOn ? '，并按小节联网补充真实文献' : ''}。</div>`;
    } else if (webOn) {
      host.innerHTML = `<div class="note warn">知识库还是空的。这一层没有你的资料可用，
           但<strong>联网检索仍然开着</strong>——「国内外研究现状」等小节会去公开文献库取真实记录，
           带 DOI 可回查。想以自己手上的文献为主，就到「参考文献与知识库」阶段导入 PDF / Word。</div>`;
    } else {
      host.innerHTML = `<div class="note warn">知识库为空，联网检索也关着：本次只能依据模型自身知识成稿，
           正文不会有引用依据。建议先到「参考文献与知识库」阶段导入几篇 PDF / Word 资料。</div>`;
    }
  } catch (err) {
    host.innerHTML = `<div class="note err">读取知识库状态失败：${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------- 开题报告 · 导入任务书/模板
   与「写初稿」阶段的上传是两条不同的路，刻意不合并：
     · 这里读出的是**框架与要求**（哪几节、字数、格式），填进下面的表单由人确认；
       正文同时入库，成为生成时的 K 层依据；
     · 写初稿那里读的是"本次要写成初稿的稿件"。
   共同点是都不需要 API Key——解析与入库都在本地。 */

async function importProposalFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const btn = $('btnPropImport');
  const zone = $('propDrop');
  zone.classList.add('busy');
  btn.disabled = true; btn.textContent = '读取中…';
  note($('propImpNote'), '', '');
  $('propImpResult').innerHTML = '';
  setProgress($('propImpBar'), $('propImpMsg'), $('propImpProg'), 0.25,
    `正在解析并入库 ${files.length} 个文件…`);

  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));

  try {
    // 解析 + 入库在后端一次完成，解析不需要 Key，所以直接传 multipart，
    // 不走 api() 那个 JSON 封装（它加的 Content-Type 会破坏 multipart 的 boundary）。
    const resp = await fetch('/api/proposal/import-plan', { method: 'POST', body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));

    applyPropPlan(data.plan || {});
    renderPropImport(data);

    const n = (data.added || []).length;
    let msg = `已解析并入库 ${n} 个文件。`;
    if (data.plan && (data.plan.sections || []).length) {
      msg += `从文档里认出 <strong>${data.plan.sections.length}</strong> 个小节，已填入下面的表单（可直接改）。`;
    } else {
      msg += ' 文档里没认出开题报告的框架小节，保留了默认七节，请按你学校的模板自行修改。';
    }
    note($('propImpNote'), 'ok', msg);
    setProgress($('propImpBar'), $('propImpMsg'), $('propImpProg'), 1, '完成');
    await refreshProposalStatus();
    setTimeout(() => hideProgress($('propImpProg')), 1200);
  } catch (err) {
    note($('propImpNote'), 'err', '导入失败：' + esc(err.message));
    hideProgress($('propImpProg'));
  } finally {
    zone.classList.remove('busy');
    btn.disabled = false; btn.textContent = '读取框架与要求';
    $('propFiles').value = '';        // 允许重复选同一个文件
  }
}

/** 把读出来的计划填进表单。**只填空的、不覆盖用户已经敲过的东西**：
 *  用户先手填了题目再上传任务书时，把题目冲掉是最让人恼火的一种"贴心"。 */
function applyPropPlan(plan) {
  const titleEl = $('propTitle');
  if (titleEl && !titleEl.value.trim() && plan.title) titleEl.value = plan.title;

  const ta = $('propSections');
  if (ta && plan.sections && plan.sections.length) ta.value = plan.sections.join('\n');

  const reqEl = $('propRequirements');
  if (reqEl && plan.requirements) reqEl.value = plan.requirements;
}

function renderPropImport(data) {
  const el = $('propImpResult');
  const added = data.added || [];
  const failed = data.failed || [];
  if (!added.length && !failed.length) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <div class="stat" style="margin-top:12px">
      <div>入库文件<b>${added.length}</b></div>
      <div>失败<b>${failed.length}</b></div>
      <div>知识库现有<b>${(data.kb || {}).documents || 0} 篇</b></div>
    </div>
    <table class="list">
      <tr><th>文件</th><th style="width:74px">类型</th><th style="width:74px">字数</th>
          <th style="width:88px">入库片段</th><th>状态</th></tr>
      ${added.map((a) => `<tr>
        <td><strong>${esc(a.filename)}</strong></td>
        <td><span class="fkind">${esc(a.kind || '?')}</span></td>
        <td>${a.chars || 0}</td>
        <td>${a.chunks || 0}</td>
        <td style="font-size:12.5px">已入库，可被检索</td></tr>`).join('')}
      ${failed.map((f) => `<tr>
        <td><strong>${esc(f.filename)}</strong></td><td colspan="3">—</td>
        <td style="font-size:12.5px;color:var(--tm-danger, #c00)">${esc(f.error)}</td></tr>`).join('')}
    </table>
    ${(data.notes || []).length
      ? `<div class="note warn" style="margin-top:10px">${data.notes.map(esc).join('<br>')}</div>`
      : ''}`;
}

function bindPropDropzone() {
  const zone = $('propDrop');
  const input = $('propFiles');
  if (!zone || !input) return;

  input.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length) {
      $('btnPropImport').disabled = false;
      importProposalFiles(e.target.files);
    }
  });
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      $('btnPropImport').disabled = false;
      importProposalFiles(e.dataTransfer.files);
    }
  });

  const btn = $('btnPropImport');
  if (btn) {
    btn.addEventListener('click', () => {
      const f = input.files;
      if (!f || !f.length) {
        // 直接点按钮但没选文件时，把文件选择框打开——而不是只报一句"请先选择文件"。
        input.click();
        return;
      }
      importProposalFiles(f);
    });
  }

  // 联网开关会改变状态提示的措辞（知识库为空时是否还能有依据），所以要联动刷新。
  const web = $('propUseWeb');
  if (web) web.addEventListener('change', () => { refreshProposalStatus(); });
}

async function runProposal() {
  const title = ($('propTitle') ? $('propTitle').value.trim() : '');
  if (!title) { note($('propNote'), 'err', '请先填写论文题目。'); return; }
  if (!LLM.ready) { note($('propNote'), 'err', '生成开题报告需要模型：请先在右上角填入你自己的 API Key。'); return; }

  let sections = ($('propSections').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
  const cap = parseInt($('propSectionCount').value, 10);
  if (cap > 0) sections = sections.slice(0, cap);
  if (!sections.length) sections = Prop.sections.slice();

  const btn = $('btnProposalRun');
  btn.disabled = true; btn.textContent = '生成中…';
  Prop.running = true;
  note($('propNote'), '', '');
  $('propResultCard').classList.add('hidden');
  setProgress($('propBar'), $('propMsg'), $('propProg'), 0.02, '正在读取知识库…');

  const body = {
    sid: SID.get(), title, sections,
    use_kb: $('propUseKb').checked,
    // 联网检索默认开；显式传上去，而不是留给后端默认——
    // 用户关掉这一层时，后端必须知道，否则"关掉了还在联网"是最难发现的一类错。
    use_web: $('propUseWeb') ? $('propUseWeb').checked : true,
    // 任务书里读出的写作要求。它进的是提示词，不是依据，所以不参与引用校验。
    requirements: $('propRequirements') ? $('propRequirements').value.trim() : '',
    provider: LLM.provider,
  };

  try {
    await sse('/api/proposal/generate', body, (ev) => {
      if (ev.type === 'stage') {
        setProgress($('propBar'), $('propMsg'), $('propProg'), ev.progress || 0, ev.message || '');
        if (ev.stage === 'warn') note($('propNote'), 'warn', esc(ev.message));
        else if (ev.stage === 'search' || ev.stage === 'writing') note($('propNote'), '', esc(ev.message));
      } else if (ev.type === 'section' && ev.partial) {
        renderProposal(ev.partial, null);      // 逐节流式预览
      } else if (ev.type === 'result') {
        SID.set(ev.sid);
        Prop.draft = ev.draft;
        renderProposal(ev.draft, ev.coverage);
        Progress.markDone('report');
        setProgress($('propBar'), $('propMsg'), $('propProg'), 1, '完成');
        $('btnProposalMd').disabled = false;
        const cov = ev.coverage || {};
        const bad = (cov.citations_invalid || 0) + (cov.ungrounded_numbers || 0);
        if (bad) {
          note($('propNote'), 'warn',
            `生成完成，但有 <strong>${cov.citations_invalid || 0} 处编造的引用编号</strong>、` +
            `<strong>${cov.ungrounded_numbers || 0} 处找不到依据的数字</strong>，请人工核对。`);
        } else {
          note($('propNote'), 'ok',
            `开题报告已生成（${cov.references || 0} 条参考文献，全部来自正文实际引用）。` +
            '<br><strong>下一步：到「写初稿」阶段继续写正文。</strong>');
        }
        setTimeout(() => hideProgress($('propProg')), 1200);
      } else if (ev.type === 'error') {
        note($('propNote'), 'err', esc(ev.message));
      }
    });
  } catch (err) {
    note($('propNote'), 'err', esc(err.message));
  } finally {
    btn.disabled = false; btn.textContent = '逐节生成';
    Prop.running = false;
  }
}

function renderProposal(draft, coverage) {
  if (!draft) return;
  const card = $('propResultCard');
  card.classList.remove('hidden');
  $('propBody').innerHTML = (draft.sections || [])
    .map((s) => `<h2>${esc(s.heading || '')}</h2>${mdToHtml(s.body || '')}`).join('');

  if (coverage) {
    const tiers = coverage.material_tiers || {};
    // **分层单独列出来**，而不是只报一个"用到多少条材料"。
    // 作者真正要知道的是"这篇开题报告里有多少条是能点回原文的（B 层带 DOI）"，
    // 而 K/A/D 三层都是他自己导入的东西，可核验性完全不同。
    const tierText = Object.keys(tiers).length
      ? Object.entries(tiers).map(([k, v]) => `${k} ${v}`).join('、')
      : '无';
    const rows = [
      ['知识库资料', `${coverage.documents ?? 0} 篇 / ${coverage.chunks ?? 0} 条片段`],
      ['材料分层', tierText],
      ['联网检索到的文献', `${coverage.web_found ?? 0} 条（去重后 ${coverage.web_unique ?? 0} 篇）`],
      ['检索源', (coverage.web_sources || []).join('、') || '—'],
      ['引用总数', coverage.citations_total ?? 0],
      ['编造的引用编号', coverage.citations_invalid ?? 0],
      ['找不到依据的数字', coverage.ungrounded_numbers ?? 0],
      ['参考文献条数', coverage.references ?? 0],
      ['用时', `${coverage.elapsed_seconds ?? 0} 秒`],
    ];
    $('propCoverage').innerHTML = `<div class="stat">` +
      rows.map(([k, v]) => `<div>${esc(k)}<b>${esc(String(v))}</b></div>`).join('') + '</div>' +
      // 依据分层这件事必须在成稿页面上再说一次：用户翻到结果页时已经看不到开关了。
      `<div class="note" style="margin-top:10px">本次依据：`
      + `${coverage.use_kb === false ? '<strong>未</strong>检索' : '检索'}本地知识库；`
      + `${coverage.use_web === false ? '<strong>未</strong>联网检索' : '联网检索公开文献库'}；`
      + `模型自身知识始终参与（这部分<strong>不带引用编号</strong>，并已按数字接地逐条复核）。`
      + `${coverage.has_requirements ? '写作要求来自你导入的任务书。' : ''}</div>`;
  }

  const refs = draft.references || [];
  $('propRefs').innerHTML = refs.length
    ? `<h3 style="font-size:14px;margin:0 0 6px">参考文献（${refs.length} 条 · 由系统按正文引用汇总）</h3>
       <div class="md">${refs.map((r) => `<div>${esc(r)}</div>`).join('')}</div>`
    : '<div class="note warn">正文没有引用任何材料，因此参考文献为空。</div>';
}

/* Markdown → 简单 HTML。只处理开题报告里真正会出现的几种：标题、加粗、段落。 */
function mdToHtml(text) {
  return String(text || '')
    .split(/\n{2,}/)
    .map((para) => {
      const t = para.trim();
      if (!t) return '';
      if (/^###\s+/.test(t)) return `<h3>${esc(t.replace(/^###\s+/, ''))}</h3>`;
      if (/^##\s+/.test(t)) return `<h3>${esc(t.replace(/^##\s+/, ''))}</h3>`;
      if (/^#\s+/.test(t)) return `<h3>${esc(t.replace(/^#\s+/, ''))}</h3>`;
      return `<p>${esc(t).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>')}</p>`;
    })
    .join('');
}

function exportProposalMd() {
  if (!Prop.draft) return;
  const lines = [`# ${Prop.draft.title || ''}`, ''];
  (Prop.draft.sections || []).forEach((s) => {
    lines.push(`## ${s.heading || ''}`, '', s.body || '', '');
  });
  if ((Prop.draft.references || []).length) {
    lines.push('## 参考文献', '', ...Prop.draft.references, '');
  }
  download(lines.join('\n'), `${Prop.draft.title || '开题报告'}.md`);
}

function initProposalStage() {
  bindKbDropzone();
  // 开题报告自己的上传入口（任务书/模板 → 框架与要求）。
  // 它与 bindKbDropzone 是**两个不同的入口、两条不同的后端路径**：
  // 这边的会读出框架与要求并填表，那边只入库不读框架。
  bindPropDropzone();
  const toProposal = $('btnToProposal');
  if (toProposal) toProposal.addEventListener('click', () => selectStage('report'));
  const run = $('btnProposalRun');
  if (run) run.addEventListener('click', runProposal);
  const md = $('btnProposalMd');
  if (md) md.addEventListener('click', exportProposalMd);
  const secs = $('propSections');
  if (secs) secs.value = (secs.value || '').trim();
}

boot();
