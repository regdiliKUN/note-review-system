"""全流程 UI 验收：真实解析 + 真实 AI 审查，逐个视图截图。"""
import json
import os
import sys
import time

ROOT = r"I:\workbuddy"
sys.path.insert(0, os.path.join(ROOT, "app"))
from backend import server          # noqa
from backend.api import Api         # noqa
from backend import review as rv    # noqa

from playwright.sync_api import sync_playwright

SAMPLE = os.path.join(ROOT, "app", "build", "示例笔记.docx")
OUT = os.path.join(ROOT, "app", "build")

api = Api(workdir=os.path.join(ROOT, "data"), get_window=lambda: None,
          asset_root=os.path.join(ROOT, "data", "assets"))

print("== 导入 ==", flush=True)
r = api.load(SAMPLE)
print("ok:", r.get("ok"), "图片:", r["doc"]["images"], "段落:", r["doc"]["blocks"], flush=True)

print("== 审查 ==", flush=True)
t = time.time()
r = api.run_review(True)
print(f"耗时 {time.time() - t:.1f}s 批注 {len(api.anns)} 条", flush=True)
for a in api.anns:
    print(f"  [{a['type']}] {a['op']} {a['block_id']} :: {a['reason'][:48]}")

# 保留一部分、丢弃一部分，模拟真实取舍
for i, a in enumerate(api.anns):
    a["status"] = "accepted" if i % 3 != 2 else "rejected"
api.blocks_current = rv.apply_accepted(api.doc["blocks"], api.anns)

print("== 重排 ==", flush=True)
api.set_relayout(True)
payload = api.state()
json.dump(payload, open(os.path.join(OUT, "payload.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print("payload 大小 %.1f KB" % (len(json.dumps(payload)) / 1024), flush=True)

httpd, port = server.start(os.path.join(ROOT, "app", "frontend"),
                           os.path.join(ROOT, "data", "assets"))
URL = f"http://127.0.0.1:{port}/index.html"

STUB = """
window.pywebview = { api: new Proxy({}, {
  get(_, name) { return async function(){
    if (name === 'boot') return {ok:true,
      health:{ok:true,models:['qwen3:8b'],provider:'ollama'},
      models:['qwen3:8b'], model:'qwen3:8b', provider:'ollama',
      base:'http://127.0.0.1:11434',
      relayoutOpts:{add_toc:true,number_headings:true,merge_lines:true,
                    normalize_text:true,add_summary:true,ensure_heading:true},
      recent:[], version:'1.0.0'};
    return {ok:true};
  };}
})};
"""

errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 940})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(URL, wait_until="networkidle")
    pg.evaluate("payload => handlePayload(payload)", payload)
    time.sleep(0.8)

    for view, name in [("annotated", "ui_1_annotated"),
                       ("original", "ui_2_original"),
                       ("revised", "ui_3_revised"),
                       ("relayout", "ui_4_relayout")]:
        pg.evaluate("v => switchView(v)", view)
        time.sleep(0.6)
        pg.screenshot(path=os.path.join(OUT, name + ".png"))

    # 滚动到文档中段，看图片与批注在正文里的样子
    pg.evaluate("v => switchView(v)", "annotated")
    time.sleep(0.4)
    pg.evaluate("document.getElementById('paperWrap').scrollTop = 260")
    time.sleep(0.4)
    pg.screenshot(path=os.path.join(OUT, "ui_5_scrolled.png"))
    b.close()

print("ERRORS:", errors, flush=True)
httpd.shutdown()
