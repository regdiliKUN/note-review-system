/* ══════════════════════════════════════════════════════════════
   纯函数层 —— 不碰 DOM、不碰 window，可在 Node 里直接跑单元测试。
   浏览器里由 index.html 在 app.js 之前加载，const 声明进入全局词法环境。
   ══════════════════════════════════════════════════════════════ */
'use strict';

/* 批注类型 → 样式类 / 颜色 */
const TYPE_CLS = {
  '错别字': 'typo', '事实纠错': 'fact', '知识补充': 'know',
  '表达优化': 'expr', '逻辑归纳': 'induct', '结构建议': 'struct',
  '存疑待核': 'doubt'
};
const TYPE_COLOR = {
  typo: '#ef4444', fact: '#f97316', know: '#0ea5e9', expr: '#8b5cf6',
  induct: '#10b981', struct: '#f59e0b', doubt: '#94a3b8'
};
const TYPE_LABEL = Object.fromEntries(
  Object.entries(TYPE_CLS).map(([k, v]) => [v, k]));
const SEV_RANK = { high: 0, medium: 1, low: 2 };

/* HTML 转义 */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* 字节数人性化 */
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

/* 秒数 → m:ss / h:mm:ss */
function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = n => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
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

/* 批注过滤：状态 + 严重度 + 关键词 */
function matchAnn(a, opts) {
  opts = opts || {};
  if (opts.status && opts.status !== 'all' && a.status !== opts.status) return false;
  if (opts.sev && opts.sev !== 'all' && a.severity !== opts.sev) return false;
  const q = (opts.q || '').trim().toLowerCase();
  if (!q) return true;
  const hay = [a.type, a.reason, a.target, a.replacement]
    .map(x => String(x == null ? '' : x).toLowerCase());
  return hay.some(x => x.includes(q));
}

/* 分页：返回当前页切片与是否还有更多 */
function paginate(list, page, size) {
  const total = list.length;
  const pages = Math.max(1, Math.ceil(total / Math.max(1, size)));
  const p = Math.min(Math.max(1, page | 0), pages);
  const start = (p - 1) * size;
  return {
    items: list.slice(start, start + size),
    page: p, pages, total,
    hasMore: p < pages,
    shown: Math.min(total, start + size)
  };
}

/* 进度百分比 + 剩余时间估算 */
function progressText(pct, elapsedSec, msg) {
  const p = Math.max(0, Math.min(1, Number(pct) || 0));
  let s = `${Math.round(p * 100)}%`;
  if (elapsedSec > 3 && p > 0.03 && p < 1) {
    const left = Math.round(elapsedSec / p - elapsedSec);
    if (left > 0 && left < 86400) s += ` · 约剩 ${fmtDur(left)}`;
  }
  return msg ? `${msg}（${s}）` : s;
}

/* 把 token 列表拼成纯文本（图片记为占位符），用于「有没有变化」的比较 */
function blockText(tokens) {
  return (tokens || []).map(t => (t.k === 'i' ? '\u27e6图\u27e7' : (t.v || ''))).join('');
}

/* 原文 / 修正版逐段对齐。
   apply_accepted 不增删 block，所以按下标对齐是安全的；
   万一长度不一致也照样输出，不会因为一边缺段而整页空白。 */
function diffBlocks(orig, rev) {
  const out = [];
  const n = Math.max((orig || []).length, (rev || []).length);
  for (let i = 0; i < n; i++) {
    const a = orig && orig[i];
    const b = rev && rev[i];
    const left = a ? blockText(a.tokens) : '';
    const right = b ? blockText(b.tokens) : '';
    out.push({
      id: (a && a.id) || (b && b.id) || ('x' + i),
      kind: (a && a.kind) || (b && b.kind) || 'para',
      level: (a && a.level) || 0,
      leftTokens: (a && a.tokens) || [],
      rightTokens: (b && b.tokens) || [],
      left, right,
      changed: left !== right
    });
  }
  return out;
}

/* 批注密度：把段落按数量分成若干桶，供右侧迷你导航条使用 */
function densityBuckets(blocks, anns, buckets = 80) {
  const n = (blocks || []).length;
  if (!n) return [];
  const want = Math.max(1, Math.min(buckets | 0 || 80, n));
  const per = Math.max(1, Math.ceil(n / want));
  const byBlock = {};
  (anns || []).forEach(a => {
    const k = a && a.block_id;
    if (!k) return;
    const cur = byBlock[k] || (byBlock[k] = { count: 0, high: 0, medium: 0, low: 0 });
    cur.count++;
    const s = a.severity === 'high' ? 'high' : a.severity === 'low' ? 'low' : 'medium';
    cur[s]++;
  });
  const out = [];
  for (let i = 0; i < n; i += per) {
    let count = 0, high = 0, medium = 0, low = 0;
    for (let j = i; j < Math.min(i + per, n); j++) {
      const d = byBlock[blocks[j].id];
      if (!d) continue;
      count += d.count; high += d.high; medium += d.medium; low += d.low;
    }
    out.push({ blockId: blocks[i].id, index: i, count, high, medium, low });
  }
  return out;
}

const LIB = {
  TYPE_CLS, TYPE_COLOR, TYPE_LABEL, SEV_RANK,
  esc, fmtBytes, fmtDur, lcsOps, diffHtml, matchAnn, paginate, progressText,
  blockText, diffBlocks, densityBuckets
};

/* Node（单元测试）与浏览器（app.js 里写 lib.xxx）两种用法都支持 */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = LIB;
}
if (typeof window !== 'undefined') {
  window.lib = LIB;
}
