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

/* ── 汇总 ── */
console.log('='.repeat(62));
console.log(`前端用例 ${pass + fail}　通过 ${pass}　失败 ${fail}`);
if (fail) {
  for (const [n, m] of failures) console.log(`  ✗ ${n}\n    ${m}`);
}
console.log('='.repeat(62));
process.exit(fail ? 1 : 0);
