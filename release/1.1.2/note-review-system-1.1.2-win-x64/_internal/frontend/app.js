/* ══════════════════════════════════════════════════════════════
   笔记审查总结系统 — 前端逻辑
   ══════════════════════════════════════════════════════════════ */
'use strict';

const TYPE_CLS = {
  '错别字': 'typo', '事实纠错': 'fact', '知识补充': 'know',
  '表达优化': 'expr', '逻辑归纳': 'induct', '结构建议': 'struct',
  '存疑待核': 'doubt'
};
const TYPE_COLOR = {
  typo: '#ef4444', fact: '#f97316', know: '#0ea5e9', expr: '#8b5cf6',
  induct: '#10b981', struct: '#f59e0b', doubt: '#94a3b8'
};
const TYPE_LABEL = Object.fromEntries(Object.entries(TYPE_CLS).map(([k, v]) => [v, k]));

const S = {
  p: null,
  view: 'annotated',
  filter: 'all',
  active: null,
  models: [],
  provider: 'ollama',
  busy: false,
  scrollMem: {},
  relayout: false,
  hist: null,
  histKind: null
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

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
    if (boot.pending) {
      const r = await window.pywebview.api.load(boot.pending);
      handlePayload(r);
    }
  } catch (e) {
    toast('初始化失败：' + e, 'err');
  }
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
  $('#modelHint').textContent = S.models.length
    ? '本机可用：' + S.models.join('、')
    : '未检测到 Ollama 模型';

  const h = boot.history || {};
  if ($('#inpHistDir')) $('#inpHistDir').value = h.dir || '';
  if ($('#histDirHint')) {
    $('#histDirHint').textContent = h.source
      ? `${h.source} · 已存 ${h.entries || 0} 条 / 占用 ${fmtBytes(h.bytes)}`
        + (h.warning ? '　⚠ ' + h.warning : '')
      : '';
  }
}

function renderRecent(list) {
  const box = $('#recentBox');
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="rl-title">最近打开</div>' + list.map(p =>
    `<button data-p="${esc(p)}" title="${esc(p)}">${esc(p.split(/[\\/]/).pop())}</button>`
  ).join('');
  $$('#recentBox button').forEach(b => b.onclick = async () => {
    const r = await window.pywebview.api.load_recent(b.dataset.p);
    handlePayload(r);
  });
}

/* ────────────────────────── 审查历史 ────────────────────────── */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function openHistory() {
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
    return `<div class="hist-item${isCur ? ' cur' : ''}">
      <div class="hi-main">
        <div class="hi-top"><b>${esc(e.ts || e.id)}</b>${isCur ? '<span class="hi-tag">当前展示</span>' : ''}</div>
        <div class="hi-sub">批注 ${e.anns || 0} 条 · 图片 ${e.images || 0} 张${e.analyzed ? `（已识别 ${e.analyzed}）` : ''} · 用时 ${e.elapsed || 0}s</div>
        <div class="hi-sub dim">模型 ${esc(e.model || '—')}${e.vision ? ' · 视觉 ' + esc(e.vision) : ''}</div>
      </div>
      <div class="hi-acts">
        <button class="mini-btn ok" data-hload="${esc(e.id)}">${isCur ? '重新载入' : '载入'}</button>
        <button class="mini-btn no" data-hdel="${esc(e.id)}">删除</button>
      </div>
    </div>`;
  }).join('');
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
  $('#btnReview').onclick = doReview;

  $$('#viewTabs .tab').forEach(t => t.onclick = () => switchView(t.dataset.view));

  $$('#rbFilter .chip').forEach(c => c.onclick = () => {
    S.filter = c.dataset.f;
    $$('#rbFilter .chip').forEach(x => x.classList.toggle('active', x === c));
    renderAnns();
  });

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
  $('#histBar').addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'close') { $('#histBar').style.display = 'none'; return; }
    if (act === 'review') { $('#histBar').style.display = 'none'; doReview(); return; }
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
  };
  $('#btnSaveSettings').onclick = async () => {
    await window.pywebview.api.set_provider(
      S.provider, $('#inpBase').value.trim(), $('#inpKey').value, $('#inpModel').value.trim(),
      $('#inpVision') ? $('#inpVision').value.trim() : '');
    const opts = {};
    $$('.checks input').forEach(c => opts[c.dataset.opt] = c.checked);
    if (S.p && S.p.doc) {
      const r = await window.pywebview.api.set_relayout(S.relayout, opts);
      handlePayload(r);
    }
    $('#settingsMask').classList.remove('on');
    toast('设置已保存', 'ok');
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
      setStatus(id, act === 'yes' ? 'accepted' : 'rejected');
      return;
    }
    activate(id, false);
  });

  // 大纲跳转
  $('#tocList').addEventListener('click', e => {
    const b = e.target.closest('.toc-item');
    if (!b) return;
    jumpToBlock(b.dataset.id);
  });

  // 快捷键
  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === '1') switchView('annotated');
    if (e.key === '2') switchView('original');
    if (e.key === '3') switchView('revised');
    if (e.key === '4') switchView('relayout');
    if (e.key === '5') switchView('images');
    if (e.key === 'Enter' && S.active) setStatus(S.active, 'accepted');
    if (e.key === 'Delete' && S.active) setStatus(S.active, 'rejected');
  });
}

/* ────────────────────────────── 导入 ────────────────────────────── */
async function doImport() {
  if (S.busy) return;
  const r = await window.pywebview.api.choose_file();
  handlePayload(r);
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
  if (first) S.histKind = null;
  renderAll();
  updateHistBar(r);
  if (first) switchView('annotated');
  if (r.error) toast(r.error, 'err');
  if (r.reviewed) toast(`审查完成，共 ${r.anns.length} 条批注，用时 ${r.elapsed}s`, 'ok');
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
}

function renderDocInfo() {
  const d = S.p.doc;
  $('#docInfo').innerHTML = `
    <span class="fname" title="${esc(d.path)}">${esc(d.name)}</span>
    <div class="row"><span>标题</span><span>${esc(S.p.doc.title || '—')}</span></div>
    <div class="row"><span>段落</span><span>${d.blocks}</span></div>
    <div class="row"><span>图片</span><span>${d.images}</span></div>
    <div class="row"><span>批注</span><span>${S.p.stats.total}</span></div>`;
}

function renderToc() {
  const list = (S.view === 'relayout' && S.p.views.relayout) ? S.p.views.relayout.toc : S.p.toc;
  $('#tocCount').textContent = list.length;
  if (!list.length) { $('#tocList').innerHTML = '<div class="empty-mini">无标题结构</div>'; return; }
  $('#tocList').innerHTML = list.map(t =>
    `<button class="toc-item l${Math.min(t.level || 1, 3)}" data-id="${t.id}"
             title="${esc(t.full || t.text)}">${esc(t.full || t.text)}</button>`
  ).join('');
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
  return (tokens || []).map(t => {
    if (t.k === 't') return esc(t.v).replace(/\n/g, '<br>');
    if (t.k === 'i') {
      const w = t.img.w ? ` width="${t.img.w}"` : '';
      return `<img src="/assets/${docId}/${encodeURIComponent(t.img.file)}"${w} loading="lazy" alt="">`;
    }
    if (t.k === 'mark') {
      const a = am[t.op] || {};
      const cls = TYPE_CLS[t.type] || 'expr';
      const st = a.status === 'accepted' ? ' acc' : a.status === 'rejected' ? ' rej' : '';
      return `<span class="mk t-${cls}${st}" data-op="${t.op}" title="${esc(a.reason || '')}">${esc(t.v)}</span>`;
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
      return `<span class="ins" data-op="${t.op}" style="background:${TYPE_COLOR[cls]}22;border-bottom-color:${TYPE_COLOR[cls]}" title="新增内容">${esc(t.v)}</span>`;
    }
    return '';
  }).join('');
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

  if (S.view === 'images') {
    renderImages(paper, docId, am);
    renderToc();
    wrap.scrollTop = S.scrollMem[S.view] || 0;
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
}

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
  let html = `<div class="img-report-head">
    <div class="irh-title">图像审查 <span class="badge-soft">${ir.total || items.length}</span></div>
    <div class="irh-sub">${esc(ir.note || '')}　待核对 ${flagged.length} 张</div>
  </div><div class="img-grid">`;
  items.forEach(it => {
    const flags = [];
    if (it.sensitive) flags.push(['含敏感信息', '#ef4444']);
    if (it.inconsistent) flags.push(['与正文不一致', '#f97316']);
    if (it.needs_check) flags.push(['需人工核对', '#f59e0b']);
    const badges = flags.map(([t, c]) =>
      `<span class="img-flag" style="background:${c}22;color:${c}">${esc(t)}</span>`).join('');
    const note = [it.sensitive_note, it.inconsistent_note, it.check_note]
      .filter(Boolean).map(esc).join('；');
    html += `<div class="img-card${flags.length ? ' flagged' : ''}">
      <div class="img-thumb"><img src="/assets/${docId}/${encodeURIComponent(it.file)}" loading="lazy" alt=""></div>
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
  paper.innerHTML = html;
}

/* 字符级 diff：把「改前 → 改后」收敛到真正变化的那几个字上 */
function lcsOps(a, b) {
  const n = a.length, m = b.length;
  if (n * m > 250000) return null;            // 太长就不做 diff
  const dp = new Array(n + 1);
  for (let i = 0; i <= n; i++) dp[i] = new Uint16Array(m + 1);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push(['=', a[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push(['-', a[i]]); i++; }
    else { ops.push(['+', b[j]]); j++; }
  }
  while (i < n) { ops.push(['-', a[i]]); i++; }
  while (j < m) { ops.push(['+', b[j]]); j++; }
  return ops;
}

function diffHtml(oldS, newS) {
  const ops = lcsOps(oldS, newS);
  if (!ops) {
    return `<span class="d-old">${esc(oldS)}</span>
            <span style="color:var(--txt-3);margin:0 3px">→</span>
            <span class="d-new">${esc(newS)}</span>`;
  }
  // 合并连续同类操作，并把「删+增」相邻块视为替换
  const runs = [];
  for (const [t, ch] of ops) {
    const last = runs[runs.length - 1];
    if (last && last.t === t) last.s += ch;
    else runs.push({ t, s: ch });
  }
  let html = '', ctx = '';
  for (const r of runs) {
    if (r.t === '=') { ctx += r.s; continue; }
    if (ctx) {
      // 只保留变化点前后少量上下文，长句不刷屏
      const head = ctx.length > 14 ? '…' + ctx.slice(-14) : ctx;
      html += esc(head);
      ctx = '';
    }
    if (r.t === '-') html += `<span class="d-old">${esc(r.s)}</span>`;
    else html += `<span class="d-new">${esc(r.s)}</span>`;
  }
  if (ctx) {
    const tail = ctx.length > 14 ? ctx.slice(0, 14) + '…' : ctx;
    html += esc(tail);
  }
  return html;
}

function renderAnns() {
  const p = S.p;
  const box = $('#annList');
  if (!p || !p.anns || !p.anns.length) {
    box.innerHTML = '<div class="empty-mini pad">还没有批注。点击「开始 AI 审查」。</div>';
    return;
  }
  let list = p.anns;
  if (S.filter !== 'all') list = list.filter(a => a.status === S.filter);
  if (!list.length) {
    box.innerHTML = '<div class="empty-mini pad">该分类下没有批注。</div>';
    return;
  }
  const idxOf = id => p.anns.findIndex(x => x.id === id) + 1;
  box.innerHTML = list.map(a => {
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
        <i class="sev-dot ${a.severity}"></i>
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
      </div>
    </div>`;
  }).join('');
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
  const r = await window.pywebview.api.set_status(id, status);
  S.p = r;
  renderAll();
  const a = r.anns.find(x => x.id === id);
  if (a) {
    toast(`${a.type} · ${status === 'accepted' ? '已保留' : status === 'rejected' ? '已丢弃' : '已重置'}`,
      status === 'accepted' ? 'ok' : '');
  }
}

async function bulk(action) {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
  if (!S.p.anns.length) return toast('还没有批注', 'err');
  const r = await window.pywebview.api.bulk(action, 'all');
  S.p = r;
  renderAll();
  toast(action === 'accept' ? '已全部保留' : '已全部撤销',
    action === 'accept' ? 'ok' : '');
}

/* ────────────────────────────── 审查 ────────────────────────────── */
async function doReview() {
  if (!S.p || !S.p.doc) return toast('请先导入文档', 'err');
  if (S.busy) return;
  S.busy = true;
  const btn = $('#btnReview');
  btn.disabled = true;
  $('#btnReviewTxt').textContent = '审查中…';
  $('#progressWrap').classList.add('on');
  $('#progressFill').style.width = '2%';
  $('#progressTxt').textContent = '准备中…';
  try {
    const r = await window.pywebview.api.run_review(true);
    if (r.ok === false) toast(r.error, 'err');
    else handlePayload(r);
  } catch (e) {
    toast('审查失败：' + e, 'err');
  } finally {
    S.busy = false;
    btn.disabled = false;
    $('#btnReviewTxt').textContent = '重新审查';
    setTimeout(() => $('#progressWrap').classList.remove('on'), 600);
  }
}

window.__evt = function (evt, data) {
  if (evt === 'progress') {
    $('#progressFill').style.width = Math.round((data.pct || 0) * 100) + '%';
    $('#progressTxt').textContent = data.msg || '';
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
