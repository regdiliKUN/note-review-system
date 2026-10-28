# 优化轮次台账

每一轮 = 一处可验证的实际改动。验证方式统一为 `python tests/run_tests.py`，
新增行为都必须先有测试再落地，避免「改完看起来更好、其实退化了」。

| # | 模块 | 改动 | 收益 |
|---|------|------|------|
| 1 | docmodel.ImageStore | 图片按内容 md5 去重，并复用上次导入留下的同名文件 | 重复图只落一份；导出不再重复嵌图 |
| 2 | review.apply_accepted | 同一位置「整句替换 + 句后插入」的排序改为按跨度长度降序 | 修复补充句被替换区间吞掉的丢字 bug |
| 3 | pipeline.review_document | 批注去重从 O(n²) 线性扫描改为 set | 500 条批注时去重从 12 万次比较降到 500 次 |
| 4 | pipeline | 新增 `_pieces()`：超长段落按句切分送审，sid 保持绝对编号 | 两万字单段落不再撑爆上下文，且批注仍能精确落位 |
| 5 | pipeline.review_document | 单块模型调用失败不再中断整篇，失败信息进 `report` | 长文档审查从「一崩全崩」变成「部分成功」 |
| 6 | llm.LLM | 新增 `_with_retry()`，对连接重置/超时/502 等临时故障指数退避重试 | Ollama 重启或显存回收时不再直接判失败 |
| 7 | llm.extract_json | 新增截断修复：补字符串、补括号、去悬空键 | 小模型被 num_predict 截断时能救回已生成的批注 |
| 8 | docmodel.parse_text | 编码自动判定（BOM → UTF-8 → GB18030） | GBK 编码的中文笔记不再整篇乱码 |
| 9 | docmodel.export_html | 图片总量超 8MB 时改外链目录 | 几百张图的笔记不再导出上百 MB 的卡死 HTML |
| 10 | review.stats | status 白名单校验 | 脏数据不会覆盖 total 等统计键 |
