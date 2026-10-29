/**
 * 前端纯函数单元测试（Node 直接跑，不需要浏览器）
 *   node tests/test_frontend.mjs
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const require = createRequire(import.meta.url);
const lib = require(path.join(ROOT, 'app', 'frontend', 'lib.js'));

let pass = 0, fail = 0;
const failures = [];

function t(name, fn) {
  try { fn(); pass++; }
  catch (e) { fail++; failures.push([name, e.message]); }
}

/* ── esc ── */
t('esc 转义全部五个字符', () => {
  assert.equal(lib.esc(`<a href="x">'&'</a>`),
    '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;');
});
t('esc 处理 null/undefined', () => {
  assert.equal(lib.esc(null), '');
  assert.equal(lib.esc(undefined), '');
});
t('esc 不改变中文', () => assert.equal(lib.esc('中文笔记'), '中文笔记'));

/* ── fmtBytes ── */
t('fmtBytes 字节', () => assert.equal(lib.fmtBytes(512), '512 B'));
t('fmtBytes KB', () => assert.equal(lib.fmtBytes(2048), '2 KB'));
t('fmtBytes MB', () => assert.equal(lib.fmtBytes(3 * 1024 * 1024), '3.0 MB'));
t('fmtBytes GB', () => assert.equal(lib.fmtBytes(2 * 1024 ** 3), '2.00 GB'));
t('fmtBytes 非法输入归零', () => assert.equal(lib.fmtBytes('abc'), '0 B'));

/* ── fmtDur ── */
t('fmtDur 分钟级', () => assert.equal(lib.fmtDur(75), '1:15'));
t('fmtDur 小时级', () => assert.equal(lib.fmtDur(3675), '1:01:15'));
t('fmtDur 负数归零', () => assert.equal(lib.fmtDur(-5), '0:00'));

/* ── lcsOps / diffHtml ── */
t('lcsOps 完全相同', () => {
  const ops = lib.lcsOps('abc', 'abc');
  assert.deepEqual(ops.map(o => o[0]), ['=', '=', '=']);
});
t('lcsOps 单字替换', () => {
  const ops = lib.lcsOps('猫在垫子上', '猫在椅子上');
  const changed = ops.filter(o => o[0] !== '=').map(o => o[1]).join('');
  assert.ok(changed.includes('垫') && changed.includes('椅'));
});
t('lcsOps 超长直接返回 null', () => {
  assert.equal(lib.lcsOps('x'.repeat(600), 'y'.repeat(600)), null);
});
t('diffHtml 只标出真正变化的字', () => {
  const html = lib.diffHtml('今年是2023年', '今年是2024年');
  const oldPart = (html.match(/class="d-old">([^<]*)</) || [])[1];
  const newPart = (html.match(/class="d-new">([^<]*)</) || [])[1];
  assert.equal(oldPart, '3', '删掉的应该只有那个 3');
  assert.equal(newPart, '4', '新增的应该只有那个 4');
});
t('diffHtml 超长退化为箭头式', () => {
  const html = lib.diffHtml('x'.repeat(600), 'y'.repeat(600));
  assert.ok(html.includes('→'));
});
t('diffHtml 转义 HTML', () => {
  const html = lib.diffHtml('<b>a</b>', '<i>a</i>');
  assert.ok(!html.includes('<b>'));
});

/* ── matchAnn ── */
const A = {
  type: '事实纠错', status: 'pending', severity: 'high',
  reason: 'Adam 提出于 2014 年', target: 'Adam 是 2013 年提出的。', replacement: 'Adam 是 2014 年提出的。'
};
t('matchAnn 无筛选时全通过', () => assert.ok(lib.matchAnn(A, {})));
t('matchAnn 状态不匹配', () => assert.ok(!lib.matchAnn(A, { status: 'accepted' })));
t('matchAnn 严重度不匹配', () => assert.ok(!lib.matchAnn(A, { sev: 'low' })));
t('matchAnn 按理由搜索', () => assert.ok(lib.matchAnn(A, { q: '2014' })));
t('matchAnn 按类型搜索', () => assert.ok(lib.matchAnn(A, { q: '事实' })));
t('matchAnn 搜索不区分大小写', () => assert.ok(lib.matchAnn(A, { q: 'adam' })));
t('matchAnn 搜不到', () => assert.ok(!lib.matchAnn(A, { q: '完全无关的词' })));
t('matchAnn 组合条件', () => assert.ok(lib.matchAnn(A, { status: 'pending', sev: 'high', q: 'Adam' })));

/* ── paginate ── */
t('paginate 第一页', () => {
  const r = lib.paginate([1, 2, 3, 4, 5], 1, 2);
  assert.deepEqual(r.items, [1, 2]);
  assert.equal(r.pages, 3);
  assert.equal(r.hasMore, true);
});
t('paginate 最后一页', () => {
  const r = lib.paginate([1, 2, 3, 4, 5], 3, 2);
  assert.deepEqual(r.items, [5]);
  assert.equal(r.hasMore, false);
  assert.equal(r.shown, 5);
});
t('paginate 页码越界被夹住', () => {
  assert.equal(lib.paginate([1, 2], 99, 10).page, 1);
  assert.equal(lib.paginate([1, 2], -3, 10).page, 1);
});
t('paginate 空列表', () => {
  const r = lib.paginate([], 1, 60);
  assert.equal(r.total, 0);
  assert.equal(r.pages, 1);
  assert.equal(r.hasMore, false);
});

/* ── progressText ── */
t('progressText 只有百分比', () => {
  assert.equal(lib.progressText(0.42, 0, ''), '42%');
});
t('progressText 带剩余时间', () => {
  const s = lib.progressText(0.5, 100, '正在审查');
  assert.ok(s.includes('正在审查'));
  assert.ok(s.includes('约剩'));
});
t('progressText 接近完成不显示剩余', () => {
  assert.ok(!lib.progressText(1, 100, '').includes('约剩'));
});

/* ── 类型映射 ── */
t('TYPE_LABEL 可反查', () => {
  assert.equal(lib.TYPE_LABEL.typo, '错别字');
  assert.equal(lib.TYPE_LABEL.doubt, '存疑待核');
});
t('每个类型都有颜色', () => {
  for (const cls of Object.values(lib.TYPE_CLS)) {
    assert.ok(lib.TYPE_COLOR[cls], cls + ' 缺少颜色');
  }
});
t('SEV_RANK 顺序', () => {
  assert.ok(lib.SEV_RANK.high < lib.SEV_RANK.medium);
  assert.ok(lib.SEV_RANK.medium < lib.SEV_RANK.low);
});

/* ── DOM 契约：JS 里引用的 #id 必须真的存在于 index.html ── */
const fs = require('node:fs');
const html = fs.readFileSync(path.join(ROOT, 'app', 'frontend', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'app', 'frontend', 'app.js'), 'utf8');
const libJs = fs.readFileSync(path.join(ROOT, 'app', 'frontend', 'lib.js'), 'utf8');

const htmlIds = new Set([...html.matchAll(/\bid="([\w-]+)"/g)].map(m => m[1]));
// 有些节点是 JS 动态生成的（例如「加载更多」按钮），它们的 id 写在模板字符串里
const dynIds = new Set([...appJs.matchAll(/\bid="([\w-]+)"/g)].map(m => m[1]));
const usedIds = new Set(
  [...appJs.matchAll(/\$\$?\(\s*['"]#([\w-]+)['"]/g)].map(m => m[1]));

t('index.html 里存在被引用的所有元素 id', () => {
  const missing = [...usedIds].filter(id => !htmlIds.has(id) && !dynIds.has(id));
  assert.deepEqual(missing, [], `app.js 引用了不存在的 id: ${missing.join(', ')}`);
});

t('index.html 的 id 没有重复', () => {
  const all = [...html.matchAll(/\bid="([\w-]+)"/g)].map(m => m[1]);
  const dup = all.filter((v, i) => all.indexOf(v) !== i);
  assert.deepEqual([...new Set(dup)], []);
});

t('脚本加载顺序：lib.js 必须在 app.js 之前', () => {
  const iLib = html.indexOf('/lib.js');
  const iApp = html.indexOf('/app.js');
  assert.ok(iLib > 0 && iApp > 0, '两个脚本都要被引入');
  assert.ok(iLib < iApp, 'lib.js 必须先于 app.js 加载，否则 const 未定义');
});

t('app.js 不再重复声明 lib.js 里的符号', () => {
  for (const name of ['TYPE_CLS', 'TYPE_COLOR', 'TYPE_LABEL']) {
    const re = new RegExp(`^\\s*const\\s+${name}\\s*=`, 'm');
    assert.ok(!re.test(appJs), `${name} 应在 lib.js 中定义，app.js 里不要再声明`);
  }
});

t('没有残留的 console.log 调试输出', () => {
  const hits = [...appJs.matchAll(/^\s*console\.log\(/gm)];
  assert.deepEqual(hits.map(h => h[0].trim()), []);
});

/* ── blockText / diffBlocks / densityBuckets ── */
const T = v => ({ k: 't', v });
const IMG = { k: 'i', img: { file: 'a.png' } };

t('blockText 拼文本并把图片记成占位符', () => {
  assert.equal(lib.blockText([T('甲'), IMG, T('乙')]), '甲⟦图⟧乙');
  assert.equal(lib.blockText([]), '');
  assert.equal(lib.blockText(null), '');
});

t('diffBlocks 识别改动段落', () => {
  const orig = [{ id: 'b0', kind: 'para', tokens: [T('原句。')] },
                { id: 'b1', kind: 'para', tokens: [T('没变。')] }];
  const rev = [{ id: 'b0', kind: 'para', tokens: [T('新句。')] },
               { id: 'b1', kind: 'para', tokens: [T('没变。')] }];
  const rows = lib.diffBlocks(orig, rev);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].changed, true);
  assert.equal(rows[1].changed, false);
  assert.equal(rows[0].id, 'b0');
});

t('diffBlocks 长度不一致也不崩', () => {
  const rows = lib.diffBlocks([{ id: 'b0', kind: 'para', tokens: [T('x')] }], []);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].right, '');
  assert.equal(rows[0].changed, true);
});

t('diffBlocks 保留 token 供高亮渲染', () => {
  const tk = [{ k: 'ins', v: '新增', op: 'a1', type: '知识补充' }];
  const rows = lib.diffBlocks([], [{ id: 'b0', kind: 'para', tokens: tk }]);
  assert.deepEqual(rows[0].rightTokens, tk);
});

t('densityBuckets 聚合严重度', () => {
  const blocks = [{ id: 'b0' }, { id: 'b1' }, { id: 'b2' }];
  const anns = [
    { block_id: 'b0', severity: 'high' },
    { block_id: 'b0', severity: 'low' },
    { block_id: 'b2', severity: 'medium' }
  ];
  const d = lib.densityBuckets(blocks, anns, 3);
  assert.equal(d.length, 3);
  assert.equal(d[0].count, 2);
  assert.equal(d[0].high, 1);
  assert.equal(d[0].low, 1);
  assert.equal(d[1].count, 0);
  assert.equal(d[2].medium, 1);
});

t('densityBuckets 桶数不超过段落数', () => {
  const blocks = [{ id: 'b0' }, { id: 'b1' }];
  assert.equal(lib.densityBuckets(blocks, [], 80).length, 2);
  assert.deepEqual(lib.densityBuckets([], [], 80), []);
});

t('densityBuckets 无批注时计数全零', () => {
  const d = lib.densityBuckets([{ id: 'b0' }], [], 10);
  assert.equal(d[0].count, 0);
  assert.equal(d[0].high + d[0].medium + d[0].low, 0);
});

t('lib 在浏览器环境挂到 window 上', () => {
  const src = libJs;
  assert.ok(/window\.lib\s*=/.test(src), 'lib.js 必须把 API 挂到 window.lib，app.js 才能用 lib.xxx');
});

t('对比视图与密度条的元素存在于 HTML', () => {
  assert.ok(/id="density"/.test(html), '缺少密度条容器');
  assert.ok(/data-view="compare"/.test(html), '缺少对比视图标签');
});

/* ── healthScore ── */
const HB = n => Array.from({ length: n }, (_, i) => ({ id: 'b' + i, kind: 'para', tokens: [{ k: 't', v: '字'.repeat(100) }] }));
const HA = (n, sev, status) => Array.from({ length: n }, () => ({ block_id: 'b0', severity: sev || 'low', status: status || 'pending' }));

t('healthScore 无批注时接近满分', () => {
  const h = lib.healthScore(HB(5), []);
  assert.equal(h.score, 100);
  assert.equal(h.level, 'good');
});

t('healthScore 密度越高分越低', () => {
  const few = lib.healthScore(HB(5) , HA(2));
  const many = lib.healthScore(HB(5) , HA(40));
  assert.ok(many.score < few.score, `${many.score} 应低于 ${few.score}`);
});

t('healthScore 高优先级扣分更多', () => {
  const low = lib.healthScore(HB(5) , HA(4, 'low'));
  const high = lib.healthScore(HB(5) , HA(4, 'high'));
  assert.ok(high.score < low.score);
});

t('healthScore 有未审段落时扣分并给出建议', () => {
  const h = lib.healthScore(HB(10) , HA(2), { failed: 3 });
  assert.ok(h.score < 100);
  assert.match(h.advice, /补审/);
});

t('healthScore 丢弃率过高时提示力度偏严', () => {
  const h = lib.healthScore(HB(5) , HA(10), { rejected: 9 });
  assert.match(h.advice, /偏严/);
});

t('healthScore 分数恒在 0..100', () => {
  const h = lib.healthScore(HB(3) , HA(300, 'high'), { failed: 3 });
  assert.ok(h.score >= 0 && h.score <= 100);
  assert.equal(h.level, 'bad');
});

t('healthScore 空文档不崩', () => {
  const h = lib.healthScore([], []);
  assert.equal(h.score, 100);
  assert.equal(h.detail.chars, 0);
});

/* ── 快捷键不能重复绑定 ── */
t('数字键 1-7 各自只绑定一个视图', () => {
  const keys = [...appJs.matchAll(/e\.key === '(\d)'\)\s*switchView\('(\w+)'\)/g)];
  const map = {};
  for (const [, k, v] of keys) {
    assert.ok(!map[k], `按键 ${k} 被重复绑定（${map[k]} 与 ${v}）`);
    map[k] = v;
  }
  const views = Object.values(map);
  assert.equal(new Set(views).size, views.length, '同一视图不应占用两个按键');
  for (const v of ['annotated', 'original', 'revised', 'relayout', 'images', 'summary', 'compare']) {
    assert.ok(views.includes(v), `视图 ${v} 没有快捷键`);
  }
});

t('快捷键面板列出了全部视图键', () => {
  for (const k of ['1', '2', '3', '4', '5', '6', '7']) {
    assert.ok(new RegExp(`<kbd>${k}</kbd>`).test(html), `快捷键面板缺少 ${k}`);
  }
});

t('健康度卡片样式已定义', () => {
  const css = libJs && require('node:fs').readFileSync(
    path.join(ROOT, 'app', 'frontend', 'styles.css'), 'utf8');
  for (const cls of ['.health-score', '.health-good', '.health-bad']) {
    assert.ok(css.includes(cls), `缺少样式 ${cls}`);
  }
});

t('批量审查弹层元素齐备', () => {
  for (const id of ['btnBatch', 'batchMask', 'batchFmt', 'batchList', 'btnBatchPick']) {
    assert.ok(html.includes(`id="${id}"`), `缺少 #${id}`);
  }
  assert.ok(/batch_review/.test(appJs), 'app.js 应调用 batch_review');
});

t('批量审查有进度事件分支', () => {
  assert.ok(/evt === 'batch'/.test(appJs), '需要处理 batch 进度事件');
});

t('前端有全局错误兜底', () => {
  assert.ok(/addEventListener\('error'/.test(appJs), '缺少 window error 兜底');
  assert.ok(/unhandledrejection/.test(appJs), '缺少 Promise 拒绝兜底');
  assert.ok(/log_client_error/.test(appJs), '前端错误应落盘到日志');
});

t('图片加载失败有占位样式', () => {
  const css = require('node:fs').readFileSync(
    path.join(ROOT, 'app', 'frontend', 'styles.css'), 'utf8');
  assert.ok(/img-broken/.test(appJs), '渲染图片时应挂 onerror 标记');
  assert.ok(/img\.img-broken/.test(css), '缺少裂图占位样式');
});

/* ── 汇总 ── */
console.log('='.repeat(62));
console.log(`前端用例 ${pass + fail}　通过 ${pass}　失败 ${fail}`);
if (fail) {
  for (const [n, m] of failures) console.log(`  ✗ ${n}\n    ${m}`);
}
console.log('='.repeat(62));
process.exit(fail ? 1 : 0);
