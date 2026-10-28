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
| 11 | api._payload | 三视图 + 目录按「文档对象 + 批注列表 + 状态指纹」缓存 | 点一条批注不再重算 3×N 个 token，千段文档交互从卡顿变即时 |
| 12 | api.run_review | 接入分块失败报告，返回 `warnings` | 部分分块失败时用户能知道「哪几段没审到」，而不是以为审完了 |
| 13 | api.add_manual_note | id 改 uuid，并校验空内容 / 段落存在性 | 连续加多条手写批注不再撞 id 串位 |
| 14 | api.set_status | 非法状态与不存在的批注 id 明确报错 | 前端不再静默「点了没反应」 |
| 15 | api.bulk | 校验 action / scope，返回受影响条数 | 按分类批量操作时能确认到底改了几条 |
| 16 | api.run_review | 历史写入失败并入 warnings | 磁盘满/只读时用户不会以为结果已保存 |
| 17 | api.export | 以文件扩展名为准决定格式 | 手打 `笔记.md` 不会导出一个装着 docx 的 .md |
| 18 | api.export | 自动创建不存在的输出目录 | 手输路径不再直接失败 |
| 19 | api._export_md | 重导出前只清理本程序产出的 `NNN_*.ext` 图片 | 反复导出不会堆积旧图，也不会误删用户文件 |
| 20 | api.quit / api.load | 审查进行中禁止退出与切换文档 | 避免后台线程把结果写进另一份文档 |
