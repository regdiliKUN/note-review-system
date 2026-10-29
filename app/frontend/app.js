/* ══════════════════════════════════════════════════════════════
   笔记审查总结系统 — 前端逻辑
   纯函数（转义/格式化/diff/过滤/分页）在 lib.js，这里只做 DOM 与状态。
   ══════════════════════════════════════════════════════════════ */
'use strict';

const PAGE_SIZE = 60;          // 批注列表首屏条数，几百条时避免一次性渲染

const S = {
  p: null,
  view: 'annotated',
  filter: 'all',
  sev: 'all',
  q: '',
  page: 1,
  active: null,
  models: [],
  provider: 'ollama',
  busy: false,
  scrollMem: {},
  relayout: false,
  hist: null,
  histKind: null,
  cmp: [],
  undo: [],
  ctxBid: null,
  noteBid: null,
  fontSize: 0,          // -2..+4，作用在 .paper 的字号上
  startedAt: 0,
  lastMsg: '',
  imgPage: 1,
  find: '',
  findIdx: 0,
  groupBy: false,
  paperTheme: 0,
  level: 'normal',
  scenario: 'general',
  numberStyle: 'cn'
};

const PAPER_THEMES = [
  { key: '', label: '白' },
  { key: 'sepia', label: '米黄' },
  { key: 'night', label: '夜间' }
];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* ────────────────────────────── 启动 ────────────────────────────── */
async function init() {
  wire();
  try {
    const boot = await window.pywebview.api.boot();
    S.models = boot.models || [];
    S.provider = boot.provider || 'ollama';
    fillModels(boot.model, S.models);
    setHealth(boot.health);
    fillSettings(boot);
    S.hist = boot.history || {};
    renderRecent(boot.recent || []);
    if (!(boot.health && boot.health.ok)) showGuide(boot);
    if (boot.pending) {
      const r = await window.pywebview.api.load(boot.pending);
      handlePayload(r);
    }
  } catch (e) {
    toast('初始化失败：' + e, 'err');
  }
}

/* 首次运行 / 模型服务没起来时的引导 —— 别让用户对着一堆禁用按钮猜 */
function showGuide(boot) {
  const box = $('#guideBox');
  if (!box) return;
  const provider = (boot && boot.provider) || 'ollama';
  const base = (boot && boot.base) || 'http://127.0.0.1:11434';
  const steps = provider === 'openai'
    ? [
      `确认「设置 → 服务地址」填的是可访问的 OpenAI 兼容端点（当前：${esc(base)}）`,
      '确认 API Key 正确、账户有余额',
      '点下面的「重新检测」'
    ]
    : [
      '打开终端，运行 <code>ollama serve</code> 启动本地模型服务',
      '第一次用需要拉模型：<code>ollama pull qwen3:8b</code>（图像理解再加 <code>ollama pull qwen2.5vl:7b</code>）',
      `确认服务地址是 <code>${esc(base)}</code>（可在设置里改）`,
      '点下面的「重新检测」'
    ];
  box.style.display = '';
  box.innerHTML = `<div class="guide-head">
      <b>还差一步：模型服务未连接</b>
      <span class="guide-sub">笔记本身已经可以导入，审查需要模型服务先跑起来。</span>
    </div>
    <ol class="guide-list">${steps.map(s => `<li>${s}</li>`).join('')}</ol>
    <div class="guide-acts">
      <button class="btn btn-ghost btn-sm" id="btnGuideRetry">重新检测</button>
      <button class="btn btn-ghost btn-sm" id="btnGuideDiag">环境自检</button>
    </div>`;
  $('#btnGuideRetry').onclick = async () => {
    const r = await window.pywebview.api.refresh_models();
    if (r && r.health && r.health.ok) {
      S.models = r.models || [];
      fillModels($('#inpModel') ? $('#inpModel').value : '', S.models);
      setHealth(r.health);
      box.style.display = 'none';
      toast(`已连接，可用模型 ${S.models.length} 个`, 'ok');
    } else {
      toast((r && r.health && r.health.msg) || '仍然连不上，请检查服务是否启动', 'err');
    }
  };
  $('#btnGuideDiag').onclick = () => $('#settingsMask').classList.add('on');
}

function fillModels(cur, models) {
  const sel = $('#modelSelect');
  sel.innerHTML = '';
  if (!models.length) {
    sel.innerHTML = '<option value="">未检测到模型</option>';
    return;
  }
  models.forEach(m => {
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === cur) o.selected = true;
    sel.appendChild(o);
  });
  if (cur && !models.includes(cur)) {
    const o = document.createElement('option');
    o.value = cur; o.textContent = cur + '（自定义）'; o.selected = true;
    sel.appendChild(o);
  }
}

function setHealth(h) {
  const dot = $('#healthDot');
  dot.className = 'dot ' + (h && h.ok ? 'on' : 'off');
  dot.title = h && h.ok ? '模型服务正常' : (h && h.msg) || '未连接';
}

function fillSettings(boot) {
  $('#inpBase').value = boot.base || 'http://127.0.0.1:11434';
  $('#inpModel').value = boot.model || '';
  if ($('#inpVision')) $('#inpVision').value = boot.visionModel || '';
  S.provider = boot.provider || 'ollama';
  $$('#provSeg button').forEach(b => b.classList.toggle('active', b.dataset.p === S.provider));
  $('#fieldKey').style.display = S.provider === 'openai' ? '' : 'none';
  const opts = boot.relayoutOpts || {};
  $$('.checks input').forEach(c => { c.checked = opts[c.dataset.opt] !== false; });
  S.numberStyle = opts.number_style || 'cn';
  $$('#styleSeg button').forEach(b => b.classList.toggle('active', b.dataset.s === S.numberStyle));
  $('#modelHint').textContent = S.models.length
    ? '本机可用：' + S.models.join('、')
    : '未检测到 Ollama 模型';

  fillMediaSettings(boot.media || {}, boot.vision);

  // 审校力度与术语白名单
  S.level = boot.reviewLevel || 'normal';
  S.scenario = boot.reviewScenario || 'general';
  if (S._renderScenarios) S._renderScenarios(boot.scenarios || []);
  $$('#levelSeg button').forEach(b => b.classList.toggle('active', b.dataset.l === S.level));
  if ($('#inpGlossary')) $('#inpGlossary').value = (boot.glossary || []).join('、');
  if ($('#selConcurrency')) $('#selConcurrency').value = String(boot.reviewConcurrency || 1);
  if ($('#selMaxPerBlock')) $('#selMaxPerBlock').value = String(boot.reviewMaxPerBlock || 0);
  if ($('#levelHint')) {
    $('#levelHint').textContent = {
      light: '轻：只挑错别字与事实错误，措辞风格差异一律不提。适合已经很成熟的稿子。',
      normal: '标准：硬错误 + 确实值得改的表达与结构问题。适合大多数笔记。',
      strict: '严格：连措辞一起优化，并主动补充缺失的定义与前提。适合初稿。'
    }[S.level];
  }

  const h = boot.history || {};
  if ($('#inpHistDir')) $('#inpHistDir').value = h.dir || '';
  if ($('#histDirHint')) {
    $('#histDirHint').textContent = h.source
      ? `${h.source} · 已存 ${h.entries || 0} 条 / 占用 ${fmtBytes(h.bytes)}`
        + (h.warning ? '　⚠ ' + h.warning : '')
      : '';
  }
}

/* 多模态（视觉 / 语音 / 视频 / 图片）能力展示与表单回填 */
function fillMediaSettings(md, vision) {
  const c = md.config || {};
  if ($('#inpAsrBase')) $('#inpAsrBase').value = c.asr_base || '';
  if ($('#inpAsrModel')) $('#inpAsrModel').value = c.asr_model || '';
  if ($('#inpFfmpeg')) $('#inpFfmpeg').value = c.ffmpeg_path || '';
  if ($('#inpFrames')) $('#inpFrames').value = c.video_max_frames || 12;
  renderMediaCaps(md, vision);
}

function renderMediaCaps(md, vision) {
  md = md || {};
  vision = vision || md.vision || {};
  const asr = md.asr || {};
  const chips = [
    vision.enabled
      ? { on: true, t: '视觉识别：' + (vision.model || '已启用') }
      : { on: false, t: '视觉识别：未启用' },
    asr.ok ? { on: true, t: '语音转写：' + (asr.engine === 'http' ? '接口' : '本机') }
           : { on: false, t: '语音转写：未配置' },
    md.ffmpeg ? { on: true, t: 'ffmpeg 已就绪' }
              : { on: false, t: 'ffmpeg 未找到' },
  ];
  const html = chips.map(x =>
    `<span class="cap-chip${x.on ? ' on' : ' off'}"><i></i>${esc(x.t)}</span>`).join('');
  if ($('#visionHint')) {
    $('#visionHint').innerHTML = html + (vision.reason
      ? `<div class="hint" style="margin-top:6px">${esc(vision.reason)}</div>` : '');
  }
  if ($('#asrHint')) {
    $('#asrHint').textContent = asr.detail || '';
  }
  if ($('#ffmpegHint')) {
    $('#ffmpegHint').textContent = md.ffmpeg
      ? '已找到：' + md.ffmpeg
      : '未找到 ffmpeg —— 语音/视频笔记需要它抽取音轨和关键帧，可到 ffmpeg.org 下载后填写路径';
  }
}

function renderRecent(list) {
  const box = $('#recentBox');
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="rl-title">最近打开</div>' + list.map(p =>
    `<span class="rl-item">
       <button data-p="${esc(p)}" title="${esc(p)}">${esc(p.split(/[\\/]/).pop())}</button>
       <button class="rl-x" data-rm="${esc(p)}" title="从列表移除">✕</button>
     </span>`
  ).join('');
  $$('#recentBox [data-p]').forEach(b => b.onclick = async () => {
    const r = await window.pywebview.api.load_recent(b.dataset.p);
    handlePayload(r);
  });
  $$('#recentBox [data-rm]').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const r = await window.pywebview.api.recent_remove(b.dataset.rm);
    if (r && r.ok) {
      renderRecent(r.recent || []);
      toast('已从最近列表移除', '');
    }
  });
}

/* ────────────────────────── 审查历史 ────────────────────────── */
function openHistory() {
  S.cmp = [];
  $('#histCmp').style.display = 'none';
  renderHistory();
  $('#historyMask').classList.add('on');
}

function renderHistory() {
  const h = S.hist || {};
  $('#histPath').textContent = h.dir || '—';
  $('#histPath').title = h.dir || '';
  $('#histMeta').textContent = h.source
    ? `${h.source} · 共 ${h.files || 0} 个文件 / ${h.entries || 0} 条记录 · 占用 ${fmtBytes(h.bytes)} · 每个文件保留最近 ${h.keep || 8} 次`
    : '';
  const warn = $('#histWarn');
  if (h.warning) { warn.style.display = ''; warn.textContent = h.warning; }
  else warn.style.display = 'none';

  const list = $('#histList');
  const doc = S.p && S.p.doc;
  if (!doc) {
    $('#histCount').textContent = '0';
    list.innerHTML = '<div class="empty-mini pad">打开一个文件后，这里会列出它的历次审查。</div>';
    return;
  }
  const hb = S.p.history || {};
  const ents = hb.entries || [];
  $('#histCount').textContent = ents.length;
  if (!ents.length) {
    list.innerHTML = '<div class="empty-mini pad">这个文件还没有历史记录。完成一次审查后会自动保存。</div>';
    return;
  }
  const curId = (hb.current && hb.current.id) || null;
  list.innerHTML = ents.map(e => {
    const isCur = e.id === curId;
    const picked = (S.cmp || []).includes(e.id);
    const note = e.note
      ? `<div class="hi-note">📝 ${esc(e.note)}</div>`
      : '';
    return `<div class="hist-item${isCur ? ' cur' : ''}${picked ? ' picked' : ''}" data-hid="${esc(e.id)}">
      <div class="hi-main">
        <div class="hi-top"><b>${esc(e.ts || e.id)}</b>${isCur ? '<span class="hi-tag">当前展示</span>' : ''}</div>
        <div class="hi-sub">批注 ${e.anns || 0} 条 · 图片 ${e.images || 0} 张${e.analyzed ? `（已识别 ${e.analyzed}）` : ''} · 用时 ${e.elapsed || 0}s</div>
        <div class="hi-sub dim">模型 ${esc(e.model || '—')}${e.vision ? ' · 视觉 ' + esc(e.vision) : ''}</div>
        ${note}
      </div>
      <div class="hi-acts">
        <button class="mini-btn ok" data-hload="${esc(e.id)}">${isCur ? '重新载入' : '载入'}</button>
        <button class="mini-btn" data-hcmp="${esc(e.id)}" title="选中两条即可对比">对比</button>
        <button class="mini-btn" data-hnote="${esc(e.id)}" title="加一句备注">备注</button>
        <button class="mini-btn no" data-hdel="${esc(e.id)}">删除</button>
      </div>
    </div>`;
  }).join('');
}

/* 备注编辑：行内输入，避免依赖 WebView 里不可靠的 prompt() */
function editNote(entryId) {
  const row = $(`.hist-item[data-hid="${entryId}"]`);
  if (!row) return;
  const old = (S.p.history.entries.find(e => e.id === entryId) || {}).note || '';
  const box = document.createElement('div');
  box.className = 'hi-note-edit';
  box.innerHTML = `<input type="text" maxlength="200" value="${esc(old)}"
      placeholder="例如：改完错别字后的版本">
    <button class="mini-btn ok" data-save="1">保存</button>
    <button class="mini-btn" data-cancel="1">取消</button>`;
  row.appendChild(box);
  const inp = box.querySelector('input');
  inp.focus(); inp.select();
  const close = () => box.remove();
  box.querySelector('[data-cancel]').onclick = close;
  box.querySelector('[data-save]').onclick = async () => {
    const r = await window.pywebview.api.history_set_note(entryId, inp.value);
    if (r.ok === false) return toast(r.error || '保存失败', 'err');
    handlePayload(r);
    renderHistory();
    toast('备注已保存', 'ok');
  };
  inp.onkeydown = e => {
    if (e.key === 'Enter') box.querySelector('[data-save]').click();
    if (e.key === 'Escape') close();
    e.stopPropagation();
  };
}

async function compareSelected() {
  const ids = S.cmp || [];
  if (ids.length !== 2) return toast('请选中两条记录再对比', 'err');
  const r = await window.pywebview.api.history_compare(ids[0], ids[1]);
  if (r.ok === false) return toast(r.error || '对比失败', 'err');
  const box = $('#histCmp');
  const annHtml = (a, cls) => `<div class="cmp-row ${cls}">
      <span class="cmp-type">${esc(a.type || '')}</span>
      <span class="cmp-txt">${esc((a.replacement || a.reason || '').slice(0, 90))}</span>
    </div>`;
  box.innerHTML = `
    <div class="cmp-head">
      <b>对比结果</b>
      <span class="cmp-meta">${esc(r.a.ts)} → ${esc(r.b.ts)}</span>
      <button class="icon-btn" id="btnCloseCmp">✕</button>
    </div>
    <div class="cmp-stats">
      <span class="c-add">新增 ${r.added.length}</span>
      <span class="c-del">消失 ${r.removed.length}</span>
      <span class="c-same">相同 ${r.same}</span>
      <span class="c-meta">批注 ${r.a.anns} → ${r.b.anns}</span>
    </div>
    ${r.added.length ? `<div class="cmp-sec">这次新挑出来的</div>${r.added.map(a => annHtml(a, 'add')).join('')}` : ''}
    ${r.removed.length ? `<div class="cmp-sec">上次有、这次没有的</div>${r.removed.map(a => annHtml(a, 'del')).join('')}` : ''}
    ${!r.added.length && !r.removed.length ? '<div class="empty-mini pad">两次审查的批注完全一致。</div>' : ''}`;
  box.style.display = '';
  $('#btnCloseCmp').onclick = () => { box.style.display = 'none'; };
}

/* 跨文件搜索结果 */
function renderHistorySearch(items, q) {
  const list = $('#histList');
  $('#histCount').textContent = items.length;
  $('#histCmp').style.display = 'none';
  if (!items.length) {
    list.innerHTML = `<div class="empty-mini pad">没有找到包含「${esc(q)}」的审查记录。</div>`;
    return;
  }
  list.innerHTML = `<div class="hist-search-hint">跨文件搜索结果（点「打开」会先导入该文件）</div>`
    + items.map(e => `<div class="hist-item">
      <div class="hi-main">
        <div class="hi-top"><b>${esc(e.name || '')}</b><span class="hi-tag">${esc(e.ts || '')}</span></div>
        <div class="hi-sub">批注 ${e.anns || 0} 条 · 用时 ${e.elapsed || 0}s · 模型 ${esc(e.model || '—')}</div>
        ${e.note ? `<div class="hi-note">📝 ${esc(e.note)}</div>` : ''}
        <div class="hi-sub dim" title="${esc(e.file || '')}">${esc((e.file || '').slice(0, 80))}</div>
      </div>
      <div class="hi-acts">
        <button class="mini-btn ok" data-hopen="${esc(e.file || '')}">打开</button>
      </div>
    </div>`).join('');
  $$('#histList [data-hopen]').forEach(b => b.onclick = async () => {
    const p = b.dataset.hopen;
    if (!p) return toast('这条记录缺少文件路径', 'err');
    const r = await window.pywebview.api.load_recent(p);
    handlePayload(r);
    if (r && r.ok !== false) $('#historyMask').classList.remove('on');
  });
}

/* 顶部提示条：自动载入 / 刚保存 / 文件已改动 */
function updateHistBar(r) {
  const bar = $('#histBar');
  const hb = (r && r.history) || {};
  let kind = null;
  if (r && r.historySaved) kind = 'saved';
  else if (r && r.historyLoaded) kind = 'viewing';
  else if (r && hb.autoLoaded) kind = 'auto';
  else if (r && hb.staleAvailable) kind = 'stale';
  else if (S.histKind && (hb.current || (S.p && S.p.history && S.p.history.current))) kind = S.histKind;
  S.histKind = kind;

  let html = '';
  const fb = (r && r.failedBlocks) || (S.p && S.p.failedBlocks) || [];
  if (fb.length && !(r && r.historyLoaded)) {
    // 有段落没审到：这是最要紧的信息，优先于「已保存历史」之类的提示
    html = `<span class="hb-ico warn">!</span>
      <span class="hb-txt">上次审查有 <b>${fb.length}</b> 个段落没能完成（模型超时或中断）。
        其余段落的结果已经保留，可以只补审这几段。</span>
      <button class="hb-btn primary" data-act="retry">补审这几段</button>
      <button class="hb-btn" data-act="review">整篇重审</button>
      <button class="hb-x" data-act="close">✕</button>`;
    bar.innerHTML = html;
    bar.style.display = '';
    return;
  }
  if (kind === 'saved') {
    const e = r.historySaved;
    html = `<span class="hb-ico ok">✓</span>
      <span class="hb-txt">本次审查已存入历史 <b>${esc(e.ts)}</b>（${e.anns} 条批注${e.images ? `、${e.images} 张图` : ''}）。
        下次打开这个文件会自动载入，不必重跑模型。</span>
      <button class="hb-btn" data-act="hist">查看历史</button>
      <button class="hb-btn" data-act="dir">打开文件夹</button>
      <button class="hb-x" data-act="close">✕</button>`;
  } else if (kind === 'auto') {
    const cur = hb.current || {};
    const n = (r.anns || []).length;
    html = `<span class="hb-ico ok">↺</span>
      <span class="hb-txt">已自动载入 <b>${esc(cur.ts || '')}</b> 的审查结果（${n} 条批注），没有重复调用模型。</span>
      <button class="hb-btn" data-act="hist">查看历史</button>
      <button class="hb-btn primary" data-act="review">重新审查</button>
      <button class="hb-x" data-act="close">✕</button>`;
  } else if (kind === 'viewing') {
    const cur = (r.history && r.history.current) || (S.p && S.p.history && S.p.history.current) || {};
    html = `<span class="hb-ico">◷</span>
      <span class="hb-txt">正在查看 <b>${esc(cur.ts || '')}</b> 的历史审查结果。</span>
      <button class="hb-btn" data-act="hist">切换记录</button>
      <button class="hb-btn primary" data-act="review">重新审查</button>
      <button class="hb-x" data-act="close">✕</button>`;
  } else if (kind === 'stale') {
    html = `<span class="hb-ico warn">!</span>
      <span class="hb-txt">这个文件有 <b>${hb.count}</b> 次历史审查${hb.staleTs ? `（最近 ${esc(hb.staleTs)}）` : ''}，
        但文件内容已在审查之后修改过，为避免套用过期结果没有自动载入。</span>
      <button class="hb-btn" data-act="hist">查看历史</button>
      <button class="hb-btn primary" data-act="review">重新审查</button>
      <button class="hb-x" data-act="close">✕</button>`;
  }
  bar.innerHTML = html;
  bar.style.display = html ? '' : 'none';
}

/* ────────────────────────────── 事件 ────────────────────────────── */
function wire() {
  $('#btnImport').onclick = doImport;
  $('#btnImport2').onclick = doImport;
  $('#btnImportImages').onclick = doImportImages;
  $('#btnReview').onclick = doReview;

  $$('#viewTabs .tab').forEach(t => t.onclick = () => switchView(t.dataset.view));

  $$('#rbFilter .chip').forEach(c => c.onclick = () => {
    S.filter = c.dataset.f;
    S.page = 1;
    $$('#rbFilter .chip').forEach(x => x.classList.toggle('active', x === c));
    renderAnns();
  });

  $$('#rbSev .chip[data-s]').forEach(c => c.onclick = () => {
    S.sev = c.dataset.s;
    S.page = 1;
    $$('#rbSev .chip[data-s]').forEach(x => x.classList.toggle('active', x === c));
    renderAnns();
  });

  $('#btnGroupBy').onclick = () => {
    S.groupBy = !S.groupBy;
    $('#btnGroupBy').classList.toggle('active', S.groupBy);
    S.page = 1;
    renderAnns();
  };

  let qTimer = null;
  $('#annSearch').oninput = e => {
    const v = e.target.value;
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { S.q = v; S.page = 1; renderAnns(); }, 140);
  };

  // 字号调节（记忆在 localStorage，下次打开保持）
  const applyFont = d => {
    S.fontSize = Math.max(-3, Math.min(6, S.fontSize + d));
    document.documentElement.style.setProperty('--paper-fs', (15.5 + S.fontSize) + 'px');
    try { localStorage.setItem('nr.fontSize', String(S.fontSize)); } catch (e) {}
    toast(`正文字号 ${15.5 + S.fontSize}px`, '');
  };
  $('#btnFontPlus').onclick = () => applyFont(1);
  $('#btnFontMinus').onclick = () => applyFont(-1);
  try {
    const saved = parseInt(localStorage.getItem('nr.fontSize') || '0', 10);
    if (!isNaN(saved) && saved) {
      S.fontSize = 0;
      applyFont(saved);
    }
  } catch (e) {}

  // 纸张主题
  $('#btnPaperTheme').onclick = () => {
    S.paperTheme = (S.paperTheme + 1) % PAPER_THEMES.length;
    applyPaperTheme();
    toast('纸张主题：' + PAPER_THEMES[S.paperTheme].label, '');
  };
  try {
    const pt = parseInt(localStorage.getItem('nr.paperTheme') || '0', 10);
    if (!isNaN(pt) && pt) { S.paperTheme = pt % PAPER_THEMES.length; applyPaperTheme(); }
  } catch (e) {}

  // 页内查找
  let fTimer = null;
  $('#findInput').oninput = e => {
    clearTimeout(fTimer);
    const v = e.target.value;
    fTimer = setTimeout(() => {
      S.find = v.trim();
      S.findIdx = 0;
      renderDoc();
      updateFindCount();
    }, 160);
  };
  $('#findInput').onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1); }
    if (e.key === 'Escape') {
      $('#findInput').value = '';
      S.find = ''; S.findIdx = 0;
      renderDoc(); updateFindCount();
      $('#findInput').blur();
    }
    e.stopPropagation();
  };

  // 快捷键面板
  const openKeys = () => $('#keysMask').classList.add('on');
  $('#btnKeys').onclick = openKeys;
  $('#btnCloseKeys').onclick = () => $('#keysMask').classList.remove('on');
  $('#btnCloseKeys2').onclick = () => $('#keysMask').classList.remove('on');
  $('#keysMask').onclick = e => { if (e.target.id === 'keysMask') e.currentTarget.classList.remove('on'); };

  // 图片灯箱
  const closeLb = () => $('#lightbox').classList.remove('on');
  $('#btnLbClose').onclick = closeLb;
  $('#lightbox').onclick = e => { if (e.target.id === 'lightbox' || e.target.id === 'lbStage') closeLb(); };

  $('#btnAcceptAll').onclick = () => bulk('accept');
  $('#btnRejectAll').onclick = () => bulk('reject');

  $('#chkRelayout').onchange = async e => {
    if (!S.p || !S.p.doc) { e.target.checked = false; return; }
    const r = await window.pywebview.api.set_relayout(e.target.checked);
    S.relayout = e.target.checked;
    handlePayload(r);
    if (e.target.checked) switchView('relayout');
    else if (S.view === 'relayout') switchView('revised');
    toast(e.target.checked ? '已生成重新排版版本' : '已关闭重新排版', 'ok');
  };

  // 增量审查开关：不可用时自动禁用并说明原因
  $('#chkIncremental').onchange = async e => {
    if (!e.target.checked) return;
    const r = await window.pywebview.api.can_incremental();
    if (!r || !r.possible) {
      e.target.checked = false;
      toast((r && r.reason) || '当前无法增量审查', 'err');
      return;
    }
    toast(`增量审查：将只重审 ${r.changed}/${r.total} 段有改动的内容`, 'ok');
  };

  $('#modelSelect').onchange = async e => {
    await window.pywebview.api.set_model(e.target.value);
    toast('已切换模型：' + e.target.value, 'ok');
  };

  // ── 审查历史 ──
  $('#btnHistory').onclick = openHistory;
  $('#btnCloseHistory').onclick = () => $('#historyMask').classList.remove('on');
  $('#btnCloseHistory2').onclick = () => $('#historyMask').classList.remove('on');
  $('#historyMask').onclick = e => { if (e.target.id === 'historyMask') e.currentTarget.classList.remove('on'); };
  $('#btnOpenHistDir').onclick = async () => {
    const r = await window.pywebview.api.open_history_dir();
    if (r.ok === false) toast(r.error || '无法打开文件夹', 'err');
    else toast('已打开：' + r.dir, 'ok');
  };
  if ($('#btnOpenHistDir2')) $('#btnOpenHistDir2').onclick = async () => {
    const r = await window.pywebview.api.open_history_dir();
    if (r.ok === false) toast(r.error || '无法打开文件夹', 'err');
  };
  $('#btnChooseHistDir').onclick = async () => {
    const r = await window.pywebview.api.choose_history_dir();
    if (!r || r.ok === false) { if (r && !r.canceled) toast(r.error || '切换失败', 'err'); return; }
    S.hist = r.history;
    renderHistory();
    toast('历史目录已切换：' + r.history.dir, 'ok');
  };
  $('#btnClearHist').onclick = async () => {
    if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
    const n = (S.p.history && S.p.history.count) || 0;
    if (!n) return toast('没有可清空的历史', 'err');
    if (!confirm(`确定清空「${S.p.doc.name}」的 ${n} 条历史记录？此操作不可撤销。`)) return;
    const r = await window.pywebview.api.history_clear();
    if (r.ok === false) return toast(r.error || '清空失败', 'err');
    handlePayload(r);
    renderHistory();
    toast('已清空本文件历史', 'ok');
  };
  $('#histList').addEventListener('click', async e => {
    const ld = e.target.closest('[data-hload]');
    const dl = e.target.closest('[data-hdel]');
    const cp = e.target.closest('[data-hcmp]');
    const nt = e.target.closest('[data-hnote]');
    if (nt) { editNote(nt.dataset.hnote); return; }
    if (cp) {
      const id = cp.dataset.hcmp;
      const i = S.cmp.indexOf(id);
      if (i >= 0) S.cmp.splice(i, 1);
      else {
        if (S.cmp.length >= 2) S.cmp.shift();
        S.cmp.push(id);
      }
      renderHistory();
      if (S.cmp.length === 2) compareSelected();
      return;
    }
    if (ld) {
      const r = await window.pywebview.api.history_load(ld.dataset.hload);
      if (r.ok === false) return toast(r.error || '载入失败', 'err');
      handlePayload(r);
      $('#historyMask').classList.remove('on');
      const ts = (r.history && r.history.current && r.history.current.ts) || '';
      toast(`已载入 ${ts} 的审查结果`, 'ok');
      if (r.dropped) toast(`有 ${r.dropped} 条批注因段落已变化被跳过`, 'err');
      return;
    }
    if (dl) {
      if (!confirm('确定删除这条历史记录？')) return;
      const r = await window.pywebview.api.history_delete(dl.dataset.hdel);
      if (r.ok === false) return toast(r.error || '删除失败', 'err');
      handlePayload(r);
      renderHistory();
      toast('已删除', 'ok');
    }
  });

  // 跨文件搜索历史
  let hTimer = null;
  $('#histSearch').oninput = e => {
    clearTimeout(hTimer);
    const q = e.target.value;
    hTimer = setTimeout(async () => {
      if (!q.trim()) { renderHistory(); return; }
      const r = await window.pywebview.api.history_search(q);
      if (!r || r.ok === false) return;
      renderHistorySearch(r.items || [], q);
    }, 200);
  };
  $('#histBar').addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'close') { $('#histBar').style.display = 'none'; return; }
    if (act === 'review') { $('#histBar').style.display = 'none'; doReview(); return; }
    if (act === 'retry') { $('#histBar').style.display = 'none'; doRetryFailed(); return; }
    if (act === 'hist') { openHistory(); return; }
    if (act === 'dir') { window.pywebview.api.open_history_dir(); return; }
  });

  // 设置
  $('#btnSettings').onclick = () => $('#settingsMask').classList.add('on');  $('#btnCloseSettings').onclick = () => $('#settingsMask').classList.remove('on');
  $('#settingsMask').onclick = e => { if (e.target.id === 'settingsMask') e.currentTarget.classList.remove('on'); };
  $$('#provSeg button').forEach(b => b.onclick = () => {
    S.provider = b.dataset.p;
    $$('#provSeg button').forEach(x => x.classList.toggle('active', x === b));
    $('#fieldKey').style.display = S.provider === 'openai' ? '' : 'none';
  });
  // 笔记场景
  const renderScenarios = list => {
    const box = $('#scenarioSeg');
    if (!box) return;
    box.innerHTML = (list || []).map(s =>
      `<button data-s="${esc(s.key)}" class="${s.key === S.scenario ? 'active' : ''}">${esc(s.label)}</button>`
    ).join('');
    $$('#scenarioSeg button').forEach(b => b.onclick = () => {
      S.scenario = b.dataset.s;
      $$('#scenarioSeg button').forEach(x => x.classList.toggle('active', x === b));
    });
  };
  S._renderScenarios = renderScenarios;

  $$('#levelSeg button').forEach(b => b.onclick = () => {
    S.level = b.dataset.l;
    $$('#levelSeg button').forEach(x => x.classList.toggle('active', x === b));
    if ($('#levelHint')) {
      $('#levelHint').textContent = {
        light: '轻：只挑错别字与事实错误，措辞风格差异一律不提。适合已经很成熟的稿子。',
        normal: '标准：硬错误 + 确实值得改的表达与结构问题。适合大多数笔记。',
        strict: '严格：连措辞一起优化，并主动补充缺失的定义与前提。适合初稿。'
      }[S.level];
    }
  });
  $$('#styleSeg button').forEach(b => b.onclick = () => {
    S.numberStyle = b.dataset.s;
    $$('#styleSeg button').forEach(x => x.classList.toggle('active', x === b));
  });
  $('#btnTestConn').onclick = async () => {
    const r = await window.pywebview.api.set_provider(
      S.provider, $('#inpBase').value.trim(), $('#inpKey').value, $('#inpModel').value.trim(),
      $('#inpVision') ? $('#inpVision').value.trim() : '');
    if (r.health && r.health.ok) {
      S.models = r.health.models || [];
      fillModels($('#inpModel').value, S.models);
      setHealth(r.health);
      toast('连接成功，可用模型 ' + S.models.length + ' 个', 'ok');
    } else {
      toast((r.health && r.health.msg) || '连接失败', 'err');
    }
    const caps = await window.pywebview.api.media_caps();
    if (caps && caps.media) { S.media = caps.media; renderMediaCaps(caps.media, caps.vision); }
  };
  $('#btnSaveSettings').onclick = async () => {
    await window.pywebview.api.set_provider(
      S.provider, $('#inpBase').value.trim(), $('#inpKey').value, $('#inpModel').value.trim(),
      $('#inpVision') ? $('#inpVision').value.trim() : '');
    // 语音识别 / 视频抽帧配置
    let md = null;
    if ($('#inpAsrBase')) {
      md = await window.pywebview.api.set_asr(
        $('#inpAsrBase').value.trim(),
        $('#inpAsrModel').value.trim(),
        $('#inpAsrKey').value,
        '',
        $('#inpFfmpeg').value.trim(),
        parseInt($('#inpFrames').value, 10) || 0);
    }
    if (md) { renderMediaCaps(md.media, md.vision); S.media = md.media; }
    // 审校力度与术语白名单
    if ($('#inpGlossary')) {
      await window.pywebview.api.set_review_opts(
        S.level || 'normal', $('#inpGlossary').value, S.scenario || 'general',
        $('#selConcurrency') ? parseInt($('#selConcurrency').value, 10) : -1,
        $('#selMaxPerBlock') ? parseInt($('#selMaxPerBlock').value, 10) : -1);
    }
    const opts = {};
    $$('.checks input').forEach(c => opts[c.dataset.opt] = c.checked);
    if (S.numberStyle) opts.number_style = S.numberStyle;
    if (S.p && S.p.doc) {
      const r = await window.pywebview.api.set_relayout(S.relayout, opts);
      handlePayload(r);
    }
    $('#settingsMask').classList.remove('on');
    toast('设置已保存', 'ok');
  };

  // 环境自检
  $('#btnDiag').onclick = async () => {
    const box = $('#diagBox');
    box.innerHTML = '<div class="hint">正在检查…</div>';
    try {
      const r = await window.pywebview.api.diagnose();
      if (!r || r.ok === false) { box.innerHTML = '<div class="hint">自检失败</div>'; return; }
      box.innerHTML = r.checks.map(c =>
        `<div class="diag-row ${c.ok ? 'ok' : 'bad'}">
           <i></i>
           <div class="dr-main"><b>${esc(c.name)}</b><span>${esc(c.detail || '')}</span></div>
         </div>` + (c.hint && !c.ok ? `<div class="diag-hint">${esc(c.hint)}</div>` : '')
      ).join('') + `<div class="hint" style="margin-top:8px">
        日志：${esc(r.logPath || '')}　共 ${r.problems || 0} 项待处理</div>`;
    } catch (e) {
      box.innerHTML = `<div class="hint">自检失败：${esc(e)}</div>`;
    }
  };
  $('#btnOpenLog').onclick = async () => {
    const r = await window.pywebview.api.open_log();
    if (r && r.ok) toast('已打开日志文件夹', 'ok');
    else toast((r && r.error) || '无法打开', 'err');
  };

  // 术语白名单导入 / 导出
  if ($('#btnImportGlossary')) $('#btnImportGlossary').onclick = async () => {
    const r = await window.pywebview.api.import_glossary();
    if (!r || r.ok === false) {
      if (r && !r.canceled) toast(r.error || '导入失败', 'err');
      return;
    }
    if ($('#inpGlossary')) $('#inpGlossary').value = (r.glossary || []).join('、');
    toast(`已导入 ${r.added} 个术语（共 ${r.total} 个）`,
      r.added ? 'ok' : '');
  };
  if ($('#btnExportGlossary')) $('#btnExportGlossary').onclick = async () => {
    const r = await window.pywebview.api.export_glossary();
    if (!r || r.ok === false) {
      if (r && !r.canceled) toast(r.error || '导出失败', 'err');
      return;
    }
    toast(`已导出 ${r.count} 个术语`, 'ok');
  };

  // 导出
  $('#btnExport').onclick = e => { e.stopPropagation(); $('#exportMenu').classList.toggle('on'); };
  document.addEventListener('click', () => $('#exportMenu').classList.remove('on'));
  $$('#exportMenu button').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    $('#exportMenu').classList.remove('on');
    if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
    const kind = b.dataset.kind, fmt = b.dataset.fmt;
    if (kind === 'relayout' && !S.p.views.relayout) return toast('请先开启「重新排版」', 'err');
    if ((kind === 'report' || kind === 'csv') && !S.p.anns.length) {
      return toast('还没有批注，先跑一次 AI 审查再导出报告', 'err');
    }
    if (kind === 'summary') {
      const sm = S.p.summary || {};
      if (!sm.summary && !(sm.key_points || []).length) {
        return toast('还没有内容归纳，先跑一次 AI 审查', 'err');
      }
    }
    const r = await window.pywebview.api.choose_export_path(kind);
    if (!r.ok) return;
    const res = await window.pywebview.api.export(kind, fmt, r.path);
    if (res.ok) {
      toast('已导出：' + res.path.split(/[\\/]/).pop(), 'ok');
      window.pywebview.api.open_folder(res.path);
    } else {
      toast(res.error || '导出失败', 'err');
    }
  });

  // 文档内点击批注
  $('#paper').addEventListener('click', e => {
    const loc = e.target.closest('.img-locate');
    if (loc) {
      switchView('annotated');
      setTimeout(() => jumpToBlock(loc.dataset.bid), 80);
      return;
    }
    const im = e.target.closest('img');
    if (im && im.src) { openLightbox(im.src, im.alt || ''); return; }
    const el = e.target.closest('[data-op]');
    if (el) { activate(el.dataset.op, true); return; }
    const blk = e.target.closest('.blk');
    if (blk && !e.target.closest('[data-op]')) highlightBlockAnns(blk.id);
  });

  // 批注卡片点击
  $('#annList').addEventListener('click', e => {
    const card = e.target.closest('.ann-card');
    if (!card) return;
    const id = card.dataset.id;
    if (e.target.dataset.act) {
      const act = e.target.dataset.act;
      if (act === 'edit') { editAnn(id); return; }
      setStatus(id, act === 'yes' ? 'accepted' : 'rejected');
      return;
    }
    activate(id, false);
  });

  // 段落右键菜单：加批注 / 复制原文 / 只看本段
  $('#paper').addEventListener('contextmenu', e => {
    const blk = e.target.closest('.blk');
    if (!blk || !S.p || !S.p.doc) return;
    e.preventDefault();
    S.ctxBid = blk.id;
    const m = $('#ctxMenu');
    m.style.left = Math.min(e.clientX, window.innerWidth - 190) + 'px';
    m.style.top = Math.min(e.clientY, window.innerHeight - 140) + 'px';
    m.classList.add('on');
  });
  document.addEventListener('click', () => $('#ctxMenu').classList.remove('on'));
  $('#ctxMenu').addEventListener('click', async e => {
    const b = e.target.closest('[data-ctx]');
    if (!b) return;
    $('#ctxMenu').classList.remove('on');
    const bid = S.ctxBid;
    const act = b.dataset.ctx;
    if (act === 'note') return openNoteDialog(bid);
    if (act === 'copy') {
      const text = blockPlain(bid);
      try { await navigator.clipboard.writeText(text); toast('已复制本段原文', 'ok'); }
      catch (err) { toast('复制失败：' + err, 'err'); }
      return;
    }
    if (act === 'filter') {
      S.q = '';
      const anns = S.p.anns.filter(a => a.block_id === bid);
      if (!anns.length) return toast('这一段还没有批注', '');
      S.filter = 'all'; S.sev = 'all';
      S.page = 1;
      renderAnns();
      activate(anns[0].id, true);
      return;
    }
    if (act === 'jump') {
      const anns = S.p.anns.filter(a => a.block_id === bid);
      if (!anns.length) return toast('这一段还没有批注', '');
      activate(anns[0].id, false);
    }
  });

  // 手写批注
  const closeNote = () => $('#noteMask').classList.remove('on');
  $('#btnCloseNote').onclick = closeNote;
  $('#btnCancelNote').onclick = closeNote;
  $('#noteMask').onclick = e => { if (e.target.id === 'noteMask') closeNote(); };
  $$('#noteTypeSeg button').forEach(b => b.onclick = () => {
    $$('#noteTypeSeg button').forEach(x => x.classList.toggle('active', x === b));
  });
  $('#btnSaveNote').onclick = async () => {
    const text = $('#noteText').value.trim();
    if (!text) return toast('批注内容不能为空', 'err');
    const typ = ($('#noteTypeSeg button.active') || {}).dataset?.t || '存疑待核';
    const r = await window.pywebview.api.add_manual_note(S.noteBid, text, typ);
    if (r.ok === false) return toast(r.error || '保存失败', 'err');
    S.p = r;
    renderAll();
    closeNote();
    toast('批注已添加', 'ok');
  };

  // 大纲跳转
  $('#tocList').addEventListener('click', e => {
    const b = e.target.closest('.toc-item');
    if (!b) return;
    jumpToBlock(b.dataset.id);
  });

  // 右侧批注密度条：点一下就跳到对应段落
  $('#density').addEventListener('click', e => {
    const b = e.target.closest('.dns');
    if (!b) return;
    if (S.view === 'compare' || S.view === 'original' || S.view === 'revised') {
      jumpToBlock(b.dataset.bid);
      return;
    }
    switchView('annotated');
    setTimeout(() => jumpToBlock(b.dataset.bid), 80);
  });

  // 快捷键
  document.addEventListener('keydown', e => {
    const tag = (e.target.tagName || '').toUpperCase();
    const typing = tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';

    // Esc：关掉所有浮层（输入框里也生效）
    if (e.key === 'Escape') {
      $('#lightbox').classList.remove('on');
      $('#keysMask').classList.remove('on');
      ['#settingsMask', '#historyMask'].forEach(sel => $(sel).classList.remove('on'));
      if (typing) e.target.blur();
      return;
    }
    if (typing) return;

    if (e.ctrlKey || e.metaKey) {
      if (e.key.toLowerCase() === 'f') { e.preventDefault(); $('#findInput').focus(); return; }
      if (e.key === '=' || e.key === '+') { e.preventDefault(); $('#btnFontPlus').click(); return; }
      if (e.key === '-') { e.preventDefault(); $('#btnFontMinus').click(); return; }
      if (e.key.toLowerCase() === 'z') { e.preventDefault(); undoLast(); return; }
      return;
    }

    if (e.key === '1') switchView('annotated');
    if (e.key === '2') switchView('original');
    if (e.key === '3') switchView('revised');
    if (e.key === '4') switchView('relayout');
    if (e.key === '5') switchView('images');
    if (e.key === '6') switchView('compare');
    if (e.key === '6') switchView('summary');
    if (e.key === '/') { e.preventDefault(); $('#annSearch').focus(); return; }
    if (e.key === '?') { e.preventDefault(); $('#keysMask').classList.add('on'); return; }

    const k = e.key.toLowerCase();
    if (k === 'j') { e.preventDefault(); stepAnn(1); return; }
    if (k === 'k') { e.preventDefault(); stepAnn(-1); return; }
    if (k === 'a' && S.active) { e.preventDefault(); setStatus(S.active, 'accepted'); return; }
    if (k === 'r' && S.active) { e.preventDefault(); setStatus(S.active, 'rejected'); return; }
    if (e.key === 'Enter' && S.active) setStatus(S.active, 'accepted');
    if (e.key === 'Delete' && S.active) setStatus(S.active, 'rejected');
  });
}

/* 在「当前可见的批注」里上下移动选中项 */
function stepAnn(dir) {
  if (!S.p || !S.p.anns || !S.p.anns.length) return;
  const list = S.p.anns.filter(a => matchAnn(a, { status: S.filter, sev: S.sev, q: S.q }));
  if (!list.length) return;
  let i = list.findIndex(a => a.id === S.active);
  i = i < 0 ? (dir > 0 ? 0 : list.length - 1) : (i + dir + list.length) % list.length;
  const target = list[i];
  // 目标可能落在「还没加载」的那一页，先把页码调到它所在的页
  const pos = S.p.anns.findIndex(a => a.id === target.id);
  const visibleIdx = S.p.anns
    .filter(a => matchAnn(a, { status: S.filter, sev: S.sev, q: S.q }))
    .findIndex(a => a.id === target.id);
  S.page = Math.max(1, Math.ceil((visibleIdx + 1) / PAGE_SIZE));
  renderAnns();
  activate(target.id, true);
  void pos;
}

function openLightbox(src, cap) {
  $('#lbImg').src = src;
  $('#lbCap').textContent = cap || '';
  $('#lightbox').classList.add('on');
}

/* ────────────────────────────── 导入 ────────────────────────────── */
async function doImport() {
  if (S.busy) return toast('正在审查中，请稍候', 'err');
  const r = await window.pywebview.api.choose_file();
  handlePayload(r);
}

async function doImportImages() {
  if (S.busy) return toast('正在审查中，请稍候', 'err');
  const r = await window.pywebview.api.choose_images();
  handlePayload(r);
  if (r && r.ok && r.imageCount) toast(`已导入 ${r.imageCount} 张图片，合成一篇图片笔记`, 'ok');
}

function handlePayload(r) {
  if (!r) return;
  if (r.ok === false) { toast(r.error || '操作失败', 'err'); return; }
  if (!r.doc) return;
  const first = !S.p || !S.p.doc || (S.p.doc.path !== r.doc.path);
  S.p = r;
  S.relayout = !!r.relayoutOn;
  $('#chkRelayout').checked = S.relayout;
  $('#welcome').style.display = 'none';
  $('#paper').style.display = '';
  $('#btnReview').disabled = false;
  $('#btnReviewTxt').textContent = (r.anns && r.anns.length) ? '重新审查' : '开始 AI 审查';
  if (first) {
    // 换了一份文档：清掉上一份的筛选与选中，避免「搜到的其实是旧批注」
    S.histKind = null;
    S.page = 1;
    S.active = null;
    S.q = '';
    S.filter = 'all';
    S.sev = 'all';
    S.imgPage = 1;
    if ($('#annSearch')) $('#annSearch').value = '';
    $$('#rbFilter .chip').forEach(x => x.classList.toggle('active', x.dataset.f === 'all'));
    $$('#rbSev .chip[data-s]').forEach(x => x.classList.toggle('active', x.dataset.s === 'all'));
  }
  renderAll();
  updateHistBar(r);
  if (first) switchView('annotated');
  if (r.error) toast(r.error, 'err');
  if (r.reviewed) {
    const extra = r.reusedCount ? `（其中 ${r.reusedCount} 条沿用上次）` : '';
    toast(r.canceled
      ? `已停止审查，保留 ${r.anns.length} 条批注`
      : `审查完成，共 ${r.anns.length} 条批注${extra}，用时 ${r.elapsed}s`,
      r.canceled ? '' : 'ok');
  }
  (r.warnings || []).forEach(w => toast(w, 'err'));
}

/* ────────────────────────────── 渲染 ────────────────────────────── */
function renderAll() {
  const p = S.p;
  if (!p || !p.doc) return;
  $('#sbFile').textContent = p.doc.name;
  $('#sbFile').title = p.doc.path;
  $('#sbImg').textContent = p.doc.images;
  $('#sbBlocks').textContent = p.doc.blocks + ' 段';
  $('#annTotal').textContent = p.stats.total;
  const pend = p.stats.pending || 0;
  $('#tabAnnCount').textContent = pend;
  $('#tabAnnCount').style.display = pend ? '' : 'none';
  const ir = p.imageReport || { total: 0 };
  const imgEl = $('#tabImgCount');
  if (imgEl) { imgEl.textContent = ir.total || 0; imgEl.style.display = ir.total ? '' : 'none'; }

  renderDocInfo();
  renderToc();
  renderTypeStats();
  renderStatChips();
  renderAnns();
  renderDoc();
  const hasLayout = !!p.views.relayout;
  $('#tabLayoutOff').style.display = hasLayout ? 'none' : '';
  const sm = p.summary || {};
  const hasSum = !!(sm.summary || (sm.key_points || []).length
    || (sm.outline || []).length || (sm.gaps || []).length);
  const sumEl = $('#tabSumState');
  if (sumEl) {
    sumEl.style.display = hasSum ? 'none' : '';
    sumEl.textContent = '未生成';
  }
}

function renderDocInfo() {
  const d = S.p.doc;
  const md = d.media || {};
  const srcMap = { audio: '语音', video: '视频', image: '图片', doc: '文档' };
  let src = srcMap[d.source_type || 'doc'] || '文档';
  if (md.duration) src += ' · ' + fmtDur(md.duration);
  if (md.frames) src += ' · ' + md.frames + ' 帧';
  if (d.source_type && d.source_type !== 'doc' && md.asr_ok) src += ' · 已转写';
  if (d.source_type && d.source_type !== 'doc' && md.asr_ok === false && md.reason) src += ' · 未转写';
  $('#docInfo').innerHTML = `
    <span class="fname" title="${esc(d.path)}">${esc(d.name)}</span>
    <div class="row"><span>标题</span><span>${esc(S.p.doc.title || '—')}</span></div>
    <div class="row"><span>来源</span><span>${esc(src)}</span></div>
    <div class="row"><span>段落</span><span>${d.blocks}</span></div>
    <div class="row"><span>图片</span><span>${d.images}</span></div>
    <div class="row"><span>批注</span><span>${S.p.stats.total}</span></div>`;
}

function renderToc() {
  const list = (S.view === 'relayout' && S.p.views.relayout) ? S.p.views.relayout.toc : S.p.toc;
  $('#tocCount').textContent = list.length;
  if (!list.length) { $('#tocList').innerHTML = '<div class="empty-mini">无标题结构</div>'; return; }
  const counts = annCountByHeading();
  $('#tocList').innerHTML = list.map(t => {
    const n = counts[t.id] || 0;
    return `<button class="toc-item l${Math.min(t.level || 1, 3)}" data-id="${t.id}"
             title="${esc(t.full || t.text)}${n ? `（${n} 条批注）` : ''}">
             <span class="toc-txt">${esc(t.full || t.text)}</span>
             ${n ? `<em class="toc-n">${n}</em>` : ''}
           </button>`;
  }).join('');
}

/* 把每条批注归到它所在小节（最近的前一个标题）下 */
function annCountByHeading() {
  const view = (S.p && S.p.views && S.p.views.annotated) || [];
  const byBlock = {};
  ((S.p && S.p.anns) || []).forEach(a => {
    byBlock[a.block_id] = (byBlock[a.block_id] || 0) + 1;
  });
  const out = {};
  let cur = null;
  for (const b of view) {
    if (b.kind === 'heading') cur = b.id;
    if (cur && byBlock[b.id]) out[cur] = (out[cur] || 0) + byBlock[b.id];
  }
  return out;
}

function renderTypeStats() {
  const bt = S.p.stats.by_type || {};
  const keys = Object.keys(bt);
  if (!keys.length) { $('#typeStats').innerHTML = '<div class="empty-mini">—</div>'; return; }
  const max = Math.max(...keys.map(k => bt[k]));
  keys.sort((a, b) => bt[b] - bt[a]);
  $('#typeStats').innerHTML = keys.map(k => {
    const cls = TYPE_CLS[k] || 'expr';
    const c = TYPE_COLOR[cls];
    return `<div class="ts-row">
      <i class="sw" style="background:${c}"></i>
      <span class="nm">${esc(k)}</span>
      <span class="ct">${bt[k]}</span>
    </div>
    <div class="ts-bar"><i style="width:${Math.round(bt[k] / max * 100)}%;background:${c}"></i></div>`;
  }).join('');
}

function renderStatChips() {
  const s = S.p.stats;
  $('#statChips').innerHTML = `
    <span class="st pd">待定 <b>${s.pending}</b></span>
    <span class="st ok">保留 <b>${s.accepted}</b></span>
    <span class="st no">丢弃 <b>${s.rejected}</b></span>`;
}

function annMap() {
  const m = {};
  (S.p.anns || []).forEach(a => m[a.id] = a);
  return m;
}

function tokHtml(tokens, docId, am) {
  let n = 0;
  const q = S.find || '';
  return (tokens || []).map(t => {
    if (t.k === 't') return hi(t.v, q).replace(/\n/g, '<br>');
    if (t.k === 'i') {
      const w = t.img.w ? ` width="${t.img.w}"` : '';
      return `<img src="/assets/${docId}/${encodeURIComponent(t.img.file)}"${w} loading="lazy" alt="">`;
    }
    if (t.k === 'mark') {
      const a = am[t.op] || {};
      const cls = TYPE_CLS[t.type] || 'expr';
      const st = a.status === 'accepted' ? ' acc' : a.status === 'rejected' ? ' rej' : '';
      return `<span class="mk t-${cls}${st}" data-op="${t.op}" title="${esc(a.reason || '')}">${hi(t.v, q)}</span>`;
    }
    if (t.k === 'anchor') {
      n++;
      const a = am[t.op] || {};
      const cls = TYPE_CLS[t.type] || 'expr';
      const st = a.status === 'accepted' ? ' acc' : a.status === 'rejected' ? ' rej' : '';
      return `<span class="anchor-chip${st}" data-op="${t.op}" style="background:${TYPE_COLOR[cls]}" title="${esc(a.reason || '')}">${n}</span>`;
    }
    if (t.k === 'ins') {
      const cls = TYPE_CLS[t.type] || 'expr';
      return `<span class="ins" data-op="${t.op}" style="background:${TYPE_COLOR[cls]}22;border-bottom-color:${TYPE_COLOR[cls]}" title="新增内容">${hi(t.v, q)}</span>`;
    }
    return '';
  }).join('');
}

/* 页内查找高亮：先转义再加 mark，避免把标签本身也匹配掉 */
function hi(text, q) {
  const e = esc(text);
  if (!q) return e;
  const eq = esc(q);
  if (!eq) return e;
  const re = new RegExp(eq.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  return e.replace(re, m => `<mark class="find-hit">${m}</mark>`);
}

function blockHtml(b, docId, am) {
  if (b.kind === 'table') {
    const rows = (b.rows || []).map(r =>
      '<tr>' + r.map(c => `<td>${tokHtml(c.tokens, docId, am)}</td>`).join('') + '</tr>').join('');
    return `<div class="blk" id="${b.id}"><table>${rows}</table></div>`;
  }
  const inner = tokHtml(b.tokens, docId, am);
  const no = b.no ? `<span style="color:#4f8cff;margin-right:.35em">${esc(b.no)}</span>` : '';
  if (b.kind === 'heading') {
    const lv = Math.min(Math.max(b.level || 1, 1), 4);
    return `<h${lv} class="blk" id="${b.id}">${no}${inner}</h${lv}>`;
  }
  if (b.kind === 'image') return `<div class="blk" id="${b.id}">${inner}</div>`;
  if (b.kind === 'list') return `<p class="blk" id="${b.id}" style="padding-left:1.1em;text-indent:-1.1em">${no}${inner}</p>`;
  return `<p class="blk" id="${b.id}">${no}${inner}</p>`;
}

function renderDoc() {
  const wrap = $('#paperWrap');
  if (S.view && wrap) S.scrollMem[S.view] = wrap.scrollTop;
  const p = S.p;
  if (!p || !p.doc) return;
  const docId = p.doc.doc_id;
  const am = annMap();
  const paper = $('#paper');
  renderDensity();

  if (S.view === 'images') {
    renderImages(paper, docId, am);
    renderToc();
    wrap.scrollTop = S.scrollMem[S.view] || 0;
    return;
  }

  if (S.view === 'summary') {
    renderSummary(paper);
    renderToc();
    wrap.scrollTop = S.scrollMem[S.view] || 0;
    return;
  }

  if (S.view === 'compare') {
    renderCompare(paper, docId, am);
    renderToc();
    wrap.scrollTop = S.scrollMem[S.view] || 0;
    applyPaperTheme();
    return;
  }

  if (S.view === 'relayout') {
    const rl = p.views.relayout;
    if (!rl) {
      paper.innerHTML = `<div style="text-align:center;padding:80px 20px;color:#8b95a8">
        <p style="font-size:15px;margin-bottom:10px">还没有重排版本</p>
        <p style="font-size:13px">打开右上角的「重新排版」开关即可生成</p></div>`;
      return;
    }
    let html = '';
    const rs = rl.stats || {};
    const rsBits = [
      rs.merged_lines ? `合并断行 ${rs.merged_lines} 处` : '',
      rs.split_paras ? `拆分长段 ${rs.split_paras} 处` : '',
      rs.dropped_empty ? `删除空段 ${rs.dropped_empty} 个` : '',
      rs.dedup_headings ? `合并重复标题 ${rs.dedup_headings} 个` : '',
      rs.promoted ? `补齐标题 ${rs.promoted} 个` : '',
      rs.renumbered ? `编号 ${rs.renumbered} 个标题` : ''
    ].filter(Boolean);
    if (rsBits.length) {
      html += `<div class="rl-stats">本次重排：${esc(rsBits.join('　·　'))}</div>`;
    }
    if (rl.summary || (rl.key_points && rl.key_points.length)) {
      html += '<div class="rl-summary" style="background:#f7f9fc;border:1px solid #e5e9f0;' +
        'border-radius:9px;padding:18px 22px;margin-bottom:26px">';
      if (rl.summary) {
        html += `<div style="font-size:12px;color:#4f8cff;font-weight:700;letter-spacing:.5px;margin-bottom:7px">内容提要</div>
                 <div style="font-size:14.5px;line-height:1.9;color:#2b3341">${esc(rl.summary)}</div>`;
      }
      if (rl.key_points && rl.key_points.length) {
        html += `<div style="font-size:12px;color:#4f8cff;font-weight:700;letter-spacing:.5px;margin:14px 0 7px">要点</div>
                 <ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.95;color:#2b3341">` +
          rl.key_points.map(k => `<li>${esc(k)}</li>`).join('') + '</ul>';
      }
      html += '</div>';
    }
    if (rl.toc && rl.toc.length) {
      html += `<div style="border:1px solid #e5e9f0;border-radius:9px;padding:16px 22px;margin-bottom:30px;background:#fcfdff">
        <div style="font-size:12px;color:#8b95a8;font-weight:700;letter-spacing:.5px;margin-bottom:9px">目 录</div>`;
      html += rl.toc.map(t => {
        const pad = ((t.level || 1) - 1) * 18;
        const no = t.no ? `<span style="color:#4f8cff;font-weight:600;margin-right:.3em">${esc(t.no)}</span>` : '';
        return `<div style="font-size:13.5px;line-height:2;padding-left:${pad}px;color:#3b4351">${no}${esc(t.text)}</div>`;
      }).join('');
      html += '</div>';
    }
    html += rl.blocks.map(b => blockHtml(b, docId, am)).join('');
    paper.innerHTML = html;
  } else {
    const blocks = p.views[S.view] || p.views.annotated;
    paper.innerHTML = blocks.map(b => blockHtml(b, docId, am)).join('');
  }
  renderToc();
  wrap.scrollTop = S.scrollMem[S.view] || 0;
  applyPaperTheme();
}

/* ────────────────────────── 原文 / 修正版对比 ────────────────────────── */
function renderCompare(paper, docId, am) {
  const p = S.p;
  const rows = lib.diffBlocks(p.views.original, p.views.revised);
  const changed = rows.filter(r => r.changed);
  const onlyDiff = !!S.onlyDiff;
  const shown = onlyDiff ? changed : rows;

  let html = `<div class="cmp-bar">
    <b>${changed.length}</b> 处段落有改动　·　共 ${rows.length} 段
    <label><input type="checkbox" id="cmpOnlyDiff"${onlyDiff ? ' checked' : ''}> 只看改动</label>
  </div>`;
  if (!shown.length) {
    html += `<div style="text-align:center;padding:60px 20px;color:#8b95a8;font-size:13px">
      ${onlyDiff ? '没有任何段落被改动。可以切回「全部批注」逐条处理建议。' : '还没有内容。'}</div>`;
    paper.innerHTML = html;
    bindCompare();
    return;
  }
  html += '<div class="cmp-head"><div>原文</div><div>修正后</div></div><div class="cmp-grid">';
  shown.forEach(r => {
    const lv = Math.min(Math.max(r.level || 1, 1), 4);
    const tagL = r.kind === 'heading' ? `h${lv}` : 'div';
    html += `<div class="cmp-row${r.changed ? ' chg' : ''}" data-bid="${esc(r.id)}">
      <${tagL} class="cmp-cell old">${tokHtml(r.leftTokens, docId, am)}</${tagL}>
      <${tagL} class="cmp-cell">${tokHtml(r.rightTokens, docId, am)}</${tagL}>
    </div>`;
  });
  html += '</div>';
  paper.innerHTML = html;
  bindCompare();
}

function bindCompare() {
  const box = $('#cmpOnlyDiff');
  if (box) box.onchange = () => { S.onlyDiff = box.checked; renderDoc(); };
  $$('#paper .cmp-row').forEach(el => {
    el.onclick = e => {
      if (e.target.closest('[data-op]')) return;
      $$('#paper .cmp-row').forEach(x => x.classList.toggle('hl', x === el));
    };
  });
}

/* ────────────────────────── 右侧批注密度导航条 ────────────────────────── */
function renderDensity() {
  const box = $('#density');
  if (!box) return;
  const p = S.p;
  const skip = S.view === 'images' || S.view === 'summary' || S.view === 'relayout';
  if (!p || !p.doc || !p.anns.length || skip) {
    box.style.display = 'none';
    box.innerHTML = '';
    return;
  }
  const buckets = lib.densityBuckets(p.views.original, p.anns, 90);
  if (!buckets.length) { box.style.display = 'none'; return; }
  const max = Math.max(1, ...buckets.map(b => b.count));
  box.style.display = '';
  box.innerHTML = buckets.map(b => {
    const h = b.count ? Math.max(6, Math.round(b.count / max * 100)) : 0;
    const cls = b.high ? 'hi' : b.medium ? 'md' : 'lo';
    const tip = b.count
      ? `本段附近 ${b.count} 条批注（高 ${b.high} / 中 ${b.medium} / 低 ${b.low}）`
      : '这一段没有批注';
    return `<button class="dns ${cls}" data-bid="${esc(b.blockId)}"
      style="--h:${h}%" title="${esc(tip)}" aria-label="${esc(tip)}"></button>`;
  }).join('');
}

/* ────────────────────────── 内容归纳视图 ────────────────────────── */
function summaryMarkdown() {
  const s = (S.p && S.p.summary) || {};
  const L = [`# ${S.p.doc.title}`, ''];
  if (s.summary) L.push('## 摘要', '', s.summary, '');
  const sec = (t, arr) => {
    if (!arr || !arr.length) return;
    L.push(`## ${t}`, '');
    arr.forEach(x => L.push(`- ${x}`));
    L.push('');
  };
  sec('要点', s.key_points);
  sec('建议结构', s.outline);
  sec('建议补充', s.gaps);
  return L.join('\n');
}

function renderSummary(paper) {
  const s = (S.p && S.p.summary) || {};
  const has = s.summary || (s.key_points && s.key_points.length)
    || (s.outline && s.outline.length) || (s.gaps && s.gaps.length);
  if (!has) {
    const reviewed = !!(S.p && S.p.anns && S.p.anns.length);
    paper.innerHTML = `<div class="sum-empty">
      <div class="sum-empty-ico">◈</div>
      <p class="sum-empty-t">还没有内容归纳</p>
      <p class="sum-empty-s">${reviewed
        ? '本次审查没有生成归纳结果（可能是模型未返回，或审查被中途取消）。重新审查一次即可补齐。'
        : '点击「开始 AI 审查」，系统会在逐条批注之外，再给出一份整篇的内容归纳。'}</p>
    </div>`;
    return;
  }
  const ir = S.p.imageReport || {};
  const flagged = (ir.items || []).filter(it => it.sensitive || it.inconsistent || it.needs_check);
  const stat = S.p.stats || {};

  let html = '<div class="sum-head">';
  html += `<div class="sum-title">内容归纳</div>`;
  html += `<div class="sum-sub">基于 ${S.p.doc.blocks} 段 / ${S.p.doc.images} 张图片`
    + (s.map_reduce ? ` · 长文档分段提炼（覆盖 ${s.covered || 0} 字要点）` : '')
    + `　·　本次共 ${stat.total || 0} 条批注</div>`;
  html += `<button class="mini-btn" id="btnSumCopy">复制为 Markdown</button>`;
  html += '</div>';

  if (s.summary) {
    html += `<div class="sum-card sum-lead">
      <div class="sum-label">摘要</div>
      <div class="sum-body">${esc(s.summary)}</div>
    </div>`;
  }

  const list = (title, arr, cls, empty) => {
    if (!arr || !arr.length) return '';
    return `<div class="sum-card">
      <div class="sum-label ${cls}">${title}</div>
      <ul class="sum-list">${arr.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
    </div>`;
  };
  html += list('要点', s.key_points, 'k');
  html += list('建议结构', s.outline, 'o');
  html += list('建议补充', s.gaps, 'g');

  if (flagged.length) {
    html += `<div class="sum-card">
      <div class="sum-label g">需关注的图片（${flagged.length}）</div>
      <ul class="sum-list">${flagged.slice(0, 12).map(it =>
        `<li>图${it.index}：${esc(it.caption || '无说明')}
           <span class="sum-dim">${esc(it.position || '')}</span></li>`).join('')}</ul>
    </div>`;
  }

  html += `<div class="sum-foot">
    <button class="btn btn-ghost btn-sm" id="btnSumToRelayout">在重排版中带上归纳</button>
  </div>`;
  paper.innerHTML = html;

  const copy = $('#btnSumCopy');
  if (copy) copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(summaryMarkdown());
      toast('归纳内容已复制为 Markdown', 'ok');
    } catch (e) { toast('复制失败：' + e, 'err'); }
  };
  const toRl = $('#btnSumToRelayout');
  if (toRl) toRl.onclick = () => {
    if (S.relayout) { switchView('relayout'); return; }
    $('#chkRelayout').checked = true;
    $('#chkRelayout').dispatchEvent(new Event('change'));
  };
}
const IMG_PAGE = 40;

/* ────────────────────────── 图像审查视图 ────────────────────────── */

function renderImages(paper, docId, am) {
  const ir = S.p.imageReport || { items: [] };
  const items = ir.items || [];
  if (!items.length) {
    paper.innerHTML = `<div style="text-align:center;padding:80px 20px;color:#8b95a8">
      <p style="font-size:15px;margin-bottom:10px">还没有图像审查结果</p>
      <p style="font-size:13px">导入文档后点击「开始 AI 审查」，系统会逐图生成说明并标注问题</p></div>`;
    return;
  }
  const flagged = items.filter(it => it.sensitive || it.inconsistent || it.needs_check);
  const pg = paginate(items, S.imgPage, IMG_PAGE);
  S.imgPage = pg.page;
  let html = `<div class="img-report-head">
    <div class="irh-title">图像审查 <span class="badge-soft">${ir.total || items.length}</span></div>
    <div class="irh-sub">${esc(ir.note || '')}　待核对 ${flagged.length} 张　显示 ${pg.shown}/${pg.total}</div>
  </div><div class="img-grid">`;
  pg.items.forEach(it => {
    const flags = [];
    if (it.sensitive) flags.push(['含敏感信息', '#ef4444']);
    if (it.inconsistent) flags.push(['与正文不一致', '#f97316']);
    if (it.needs_check) flags.push(['需人工核对', '#f59e0b']);
    const badges = flags.map(([t, c]) =>
      `<span class="img-flag" style="background:${c}22;color:${c}">${esc(t)}</span>`).join('');
    const note = [it.sensitive_note, it.inconsistent_note, it.check_note]
      .filter(Boolean).map(esc).join('；');
    const src = `/assets/${docId}/${encodeURIComponent(it.file)}`;
    html += `<div class="img-card${flags.length ? ' flagged' : ''}">
      <div class="img-thumb"><img src="${src}" loading="lazy" alt="${esc(it.caption || '')}"></div>
      <div class="img-meta">
        <div class="img-cap"><b>图${it.index}</b> ${esc(it.caption || '')}</div>
        <div class="img-pos">${esc(it.position || '')}${it.kind ? ' · ' + esc(it.kind) : ''}</div>
        ${badges ? `<div class="img-flags">${badges}</div>` : ''}
        ${note ? `<div class="img-note">${note}</div>` : ''}
        <button class="img-locate" data-bid="${esc(it.block_id)}">查看原文位置</button>
      </div>
    </div>`;
  });
  html += '</div>';
  if (pg.hasMore) {
    html += `<button class="load-more" id="btnMoreImg">加载更多图片（已显示 ${pg.shown}/${pg.total}）</button>`;
  }
  paper.innerHTML = html;
  const more = $('#btnMoreImg');
  if (more) more.onclick = () => { S.imgPage += 1; renderDoc(); };
}

/* 字符级 diff 与格式化都在 lib.js：esc / fmtBytes / fmtDur / lcsOps / diffHtml */

function renderAnns() {
  const p = S.p;
  const box = $('#annList');
  if (!p || !p.anns || !p.anns.length) {
    box.innerHTML = '<div class="empty-mini pad">还没有批注。点击「开始 AI 审查」。</div>';
    updateAnnFoot(0, 0);
    return;
  }
  const all = p.anns;
  const list = all.filter(a => matchAnn(a, { status: S.filter, sev: S.sev, q: S.q }));
  if (!list.length) {
    const why = (S.q || S.sev !== 'all')
      ? '没有匹配的批注，试试换个关键词或清空筛选。'
      : '该分类下没有批注。';
    box.innerHTML = `<div class="empty-mini pad">${esc(why)}</div>`;
    updateAnnFoot(0, all.length);
    return;
  }
  const pg = paginate(list, S.page, PAGE_SIZE);
  S.page = pg.page;
  const idxOf = id => all.findIndex(x => x.id === id) + 1;
  if (S.groupBy) {
    // 按段落分组：同一段的几条意见挨在一起，改起来更连贯
    const groups = [];
    const seen = new Map();
    for (const a of pg.items) {
      let g = seen.get(a.block_id);
      if (!g) { g = { bid: a.block_id, items: [] }; seen.set(a.block_id, g); groups.push(g); }
      g.items.push(a);
    }
    box.innerHTML = groups.map(g => {
      const t = blockPlain(g.bid) || '（空段落）';
      return `<div class="ann-group">
        <div class="ag-head" data-bid="${esc(g.bid)}" title="点击跳到原文">
          <span class="ag-txt">${esc(t.slice(0, 42))}</span>
          <span class="ag-n">${g.items.length}</span>
        </div>
        ${g.items.map(a => annCardHtml(a, idxOf)).join('')}
      </div>`;
    }).join('') + (pg.hasMore
      ? `<button class="load-more" id="btnLoadMore">加载更多（已显示 ${pg.shown}/${pg.total}）</button>`
      : '');
    $$('.ag-head').forEach(h => h.onclick = () => jumpToBlock(h.dataset.bid));
  } else {
    box.innerHTML = pg.items.map(a => annCardHtml(a, idxOf)).join('')
      + (pg.hasMore
        ? `<button class="load-more" id="btnLoadMore">加载更多（已显示 ${pg.shown}/${pg.total}）</button>`
        : '');
  }
  updateAnnFoot(pg.shown, all.length);
  const more = $('#btnLoadMore');
  if (more) more.onclick = () => { S.page += 1; renderAnns(); };
}

function updateAnnFoot(shown, total) {
  const el = $('#annFoot');
  if (!el) return;
  el.textContent = total ? `显示 ${shown} / ${total} 条` : '';
}

function annCardHtml(a, idxOf) {
  const cls = TYPE_CLS[a.type] || 'expr';
  const c = TYPE_COLOR[cls];
  let diff = '';
  if (a.op === 'comment') {
    diff = `<div style="color:var(--txt-2)">${esc((a.target || '').slice(0, 120))}</div>`;
  } else if (a.op === 'insert_after') {
    diff = `<div class="d-plus">＋ ${esc(a.replacement)}</div>`;
  } else {
    diff = diffHtml(a.target || '', a.replacement || '');
  }
  return `<div class="ann-card ${a.status} ${S.active === a.id ? 'active' : ''}" data-id="${a.id}"
               style="border-left-color:${c}">
    <div class="ac-top">
      <span class="type-badge" style="background:${c}22;color:${c}">${esc(a.type)}</span>
      <span class="ann-idx">#${idxOf(a.id)}</span>
      ${a.reused ? '<span class="reuse-tag" title="段落未改动，沿用上次审查结果">沿用</span>' : ''}
      ${a.edited ? '<span class="edit-tag" title="替换文本已手动修改">已改</span>' : ''}
      <i class="sev-dot ${a.severity}" title="${esc(a.severity)}"></i>
    </div>
    <div class="ac-diff">${diff}</div>
    ${a.reason ? `<div class="ac-reason">${esc(a.reason)}</div>` : ''}
    <div class="ac-btns">
      <button class="yes ${a.status === 'accepted' ? 'on-yes' : ''}" data-act="yes">
        ${a.status === 'accepted' ? '✓ 已保留' : '保留'}
      </button>
      <button class="no ${a.status === 'rejected' ? 'on-no' : ''}" data-act="no">
        ${a.status === 'rejected' ? '✕ 已丢弃' : '丢弃'}
      </button>
      ${a.op !== 'comment'
        ? `<button class="mini-ghost" data-act="edit" title="手动微调替换文本">编辑</button>`
        : ''}
    </div>
  </div>`;
}

/* 页内查找：计数与上下跳转 */
function updateFindCount() {
  const el = $('#findCount');
  if (!el) return;
  if (!S.find) { el.textContent = ''; return; }
  const hits = $$('.find-hit');
  el.textContent = hits.length ? `${Math.min(S.findIdx + 1, hits.length)}/${hits.length}` : '无匹配';
  hits.forEach((h, i) => h.classList.toggle('cur', i === S.findIdx));
}

function stepFind(d) {
  const hits = $$('.find-hit');
  if (!hits.length) return;
  S.findIdx = (S.findIdx + d + hits.length) % hits.length;
  updateFindCount();
  const el = hits[S.findIdx];
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/* 纸张主题：白 / 米黄 / 夜间 */
function applyPaperTheme() {
  const p = $('#paper');
  const t = PAPER_THEMES[S.paperTheme] || PAPER_THEMES[0];
  if (p) p.className = 'paper' + (t.key ? ' paper-' + t.key : '');
  try { localStorage.setItem('nr.paperTheme', String(S.paperTheme)); } catch (e) {}
}

/* 段落纯文本（右键复制用） */
function tokText(tokens) {
  return (tokens || []).map(t => (t.k === 'i' ? '［图片］' : (t.v || ''))).join('');
}

function blockPlain(bid) {
  const views = (S.p && S.p.views) || {};
  const view = (views[S.view] && views[S.view].length) ? views[S.view] : views.annotated;
  const b = (view || []).find(x => x.id === bid);
  if (!b) return '';
  if (b.kind === 'table') {
    return (b.rows || []).map(r => r.map(c => tokText(c.tokens)).join(' | ')).join('\n');
  }
  return tokText(b.tokens);
}

function openNoteDialog(bid) {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
  S.noteBid = bid;
  $('#noteQuote').textContent = (blockPlain(bid) || '（空段落）').slice(0, 180);
  $('#noteText').value = '';
  $('#noteMask').classList.add('on');
  setTimeout(() => $('#noteText').focus(), 60);
}

/* 取舍进度自动保存：连续点击时防抖，避免每次点都写盘 */
let _saveTimer = null;

function scheduleSave() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(async () => {
    if (!S.p || !S.p.doc || S.busy) return;
    try {
      const r = await window.pywebview.api.save_progress();
      if (r && r.ok) S.savedAt = Date.now();
    } catch (e) { /* 自动保存失败不打断用户操作 */ }
  }, 1200);
}

/* 撤销：只回退批注状态，不影响原文 */
async function undoLast() {
  const u = (S.undo || []).pop();
  if (!u) return toast('没有可撤销的操作', '');
  const r = await window.pywebview.api.set_status(u.id, u.from);
  if (r && r.ok === false) return toast(r.error || '撤销失败', 'err');
  S.p = r;
  renderAll();
  scheduleSave();
  toast('已撤销上一步', 'ok');
}

/* 行内编辑替换文本 */
function editAnn(id) {
  const card = $(`.ann-card[data-id="${id}"]`);
  const a = S.p && S.p.anns.find(x => x.id === id);
  if (!card || !a || a.op === 'comment') return;
  const diff = card.querySelector('.ac-diff');
  if (!diff || diff.querySelector('.ann-edit')) return;
  diff.innerHTML = `<textarea class="ann-edit" rows="3">${esc(a.replacement || '')}</textarea>
    <div class="ann-edit-btns">
      <button class="mini-btn ok" data-save="1">保存</button>
      <button class="mini-btn" data-cancel="1">取消</button>
    </div>`;
  const ta = diff.querySelector('textarea');
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const cancel = () => { S.p = S.p; renderAnns(); };
  diff.querySelector('[data-cancel]').onclick = cancel;
  diff.querySelector('[data-save]').onclick = async () => {
    const r = await window.pywebview.api.set_replacement(id, ta.value);
    if (r.ok === false) return toast(r.error || '保存失败', 'err');
    S.p = r;
    renderAll();
    scheduleSave();
    toast('替换文本已更新', 'ok');
  };
  ta.onkeydown = e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) diff.querySelector('[data-save]').click();
    if (e.key === 'Escape') cancel();
    e.stopPropagation();
  };
}

/* ────────────────────────────── 交互 ────────────────────────────── */
function switchView(v) {
  if (v === 'relayout' && !S.p?.views?.relayout) {
    if (!S.p) return;
    toast('请先打开右上角「重新排版」开关', 'err');
    return;
  }
  if (S.view === v) return;
  S.view = v;
  $$('#viewTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.view === v));
  renderDoc();
}

function activate(id, scrollDoc) {
  S.active = id;
  $$('.ann-card').forEach(c => c.classList.toggle('active', c.dataset.id === id));
  const card = $(`.ann-card[data-id="${id}"]`);
  if (card) {
    if (S.filter !== 'all') { S.filter = 'all'; $$('#rbFilter .chip').forEach(x => x.classList.toggle('active', x.dataset.f === 'all')); renderAnns(); }
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  $$('.mk, .anchor-chip, .ins').forEach(el => el.classList.toggle('active', el.dataset.op === id));
  if (scrollDoc) {
    const el = $(`[data-op="${id}"]`);
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  } else {
    const a = S.p.anns.find(x => x.id === id);
    if (a) jumpToBlock(a.block_id);
  }
}

function jumpToBlock(bid) {
  const el = document.getElementById(bid);
  if (!el) return;
  el.scrollIntoView({ block: 'start', behavior: 'smooth' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

function highlightBlockAnns(bid) {
  const ids = S.p.anns.filter(a => a.block_id === bid).map(a => a.id);
  if (!ids.length) return;
  activate(ids[0], true);
}

async function setStatus(id, status) {
  if (S.busy) return;
  const before = ((S.p && S.p.anns.find(x => x.id === id)) || {}).status;
  const r = await window.pywebview.api.set_status(id, status);
  if (r && r.ok === false) return toast(r.error || '操作失败', 'err');
  S.p = r;
  if (before && before !== status) {
    S.undo.push({ id, from: before });
    if (S.undo.length > 60) S.undo.shift();
  }
  renderAll();
  scheduleSave();
  const a = r.anns.find(x => x.id === id);
  if (a) {
    toast(`${a.type} · ${status === 'accepted' ? '已保留' : status === 'rejected' ? '已丢弃' : '已重置'}`,
      status === 'accepted' ? 'ok' : '');
  }
}

async function bulk(action) {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
  if (!S.p.anns.length) return toast('还没有批注', 'err');
  const scope = ($('#bulkScope') && $('#bulkScope').value) || 'all';
  const r = await window.pywebview.api.bulk(action, scope);
  if (r && r.ok === false) return toast(r.error || '操作失败', 'err');
  S.p = r;
  renderAll();
  scheduleSave();
  const n = r.affected || 0;
  const lbl = r.scopeLabel || (scope === 'all' ? '全部' : scope);
  toast(`${lbl} · ${action === 'accept' ? '已保留' : '已撤销'} ${n} 条`,
    action === 'accept' ? 'ok' : '');
}

/* ────────────────────────────── 审查 ────────────────────────────── */
let _tickTimer = null;

function startTicker() {
  S.startedAt = Date.now();
  stopTicker();
  _tickTimer = setInterval(() => {
    const el = $('#progressTxt');
    if (!el || !S.busy) return;
    const sec = (Date.now() - S.startedAt) / 1000;
    const pct = parseFloat($('#progressFill').style.width || '0') / 100;
    el.textContent = progressText(pct, sec, S.lastMsg || '处理中');
  }, 1000);
}

function stopTicker() {
  if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null; }
}

async function doReview() {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');

  // 已经在跑 → 这一下按的是「停止」
  if (S.busy) {
    const r = await window.pywebview.api.cancel_review();
    if (r && r.ok) {
      $('#btnReviewTxt').textContent = '正在停止…';
      toast(r.message || '正在停止', '');
    } else {
      toast((r && r.error) || '无法取消', 'err');
    }
    return;
  }

  S.busy = true;
  const btn = $('#btnReview');
  btn.disabled = false;
  btn.classList.add('running');
  $('#btnReviewTxt').textContent = '停止审查';
  $('#progressWrap').classList.add('on');
  $('#progressFill').style.width = '2%';
  $('#progressTxt').textContent = '准备中…';
  S.lastMsg = '准备中';
  startTicker();
  // 先估个时长，让用户在等之前就有心理预期
  try {
    const est = await window.pywebview.api.estimate_review();
    if (est && est.ok) {
      S.lastMsg = `共 ${est.chunks} 段 / ${est.chars} 字，预计约 ${fmtDur(est.eta)}`;
      $('#progressTxt').textContent = S.lastMsg;
    }
  } catch (e) { /* 估算失败不影响主流程 */ }
  try {
    const r = await window.pywebview.api.run_review(true, !!$('#chkIncremental').checked);
    if (r.ok === false) toast(r.error, 'err');
    else handlePayload(r);
  } catch (e) {
    toast('审查失败：' + e, 'err');
  } finally {
    S.busy = false;
    stopTicker();
    btn.disabled = false;
    btn.classList.remove('running');
    $('#btnReviewTxt').textContent = '重新审查';
    setTimeout(() => $('#progressWrap').classList.remove('on'), 600);
  }
}

/* 只补审上次失败的段落：不重跑全文，已处理好的判断全部保留 */
async function doRetryFailed() {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
  if (S.busy) return toast('正在处理中，请稍候', 'err');
  const n = ((S.p.failedBlocks) || []).length;
  S.busy = true;
  const btn = $('#btnReview');
  btn.disabled = false;
  btn.classList.add('running');
  $('#btnReviewTxt').textContent = '停止审查';
  $('#progressWrap').classList.add('on');
  $('#progressFill').style.width = '4%';
  $('#progressTxt').textContent = `正在补审 ${n} 个段落…`;
  S.lastMsg = '补审中';
  startTicker();
  try {
    const r = await window.pywebview.api.retry_failed();
    if (r.ok === false) toast(r.error, 'err');
    else {
      handlePayload(r);
      toast(`已补审 ${r.retried} 个段落`, 'ok');
    }
  } catch (e) {
    toast('补审失败：' + e, 'err');
  } finally {
    S.busy = false;
    stopTicker();
    btn.disabled = false;
    btn.classList.remove('running');
    $('#btnReviewTxt').textContent = '重新审查';
    setTimeout(() => $('#progressWrap').classList.remove('on'), 600);
  }
}

window.__evt = function (evt, data) {
  if (evt === 'progress') {
    const pct = Math.max(0, Math.min(1, data.pct || 0));
    $('#progressFill').style.width = Math.round(pct * 100) + '%';
    S.lastMsg = data.msg || '';
    const sec = S.startedAt ? (Date.now() - S.startedAt) / 1000 : 0;
    $('#progressTxt').textContent = progressText(pct, sec, S.lastMsg);
  }
};

/* ────────────────────────────── Toast ────────────────────────────── */
function toast(msg, kind) {
  const t = document.createElement('div');
  t.className = 'toast ' + (kind || '');
  t.textContent = msg;
  $('#toastWrap').appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .25s, transform .25s';
    t.style.opacity = '0';
    t.style.transform = 'translateY(6px)';
    setTimeout(() => t.remove(), 260);
  }, kind === 'err' ? 4200 : 2400);
}

window.addEventListener('pywebviewready', init);
if (window.pywebview && window.pywebview.api) init();
