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
| 21 | docmodel.handle_table | 按 `w:tc` 元素去重 | 合并单元格的表头不再重复写 2-3 遍 |
| 22 | docmodel 表格 | 保留 gridSpan / vMerge，导出时还原合并 | 表格结构与原文一致，不再是「每格都是表头」 |
| 23 | docmodel.handle_paragraph | 列表层级取 numPr/ilvl，并用样式名兜底 | 多级列表重排后不再全部顶格 |
| 24 | docmodel.handle_paragraph | 识别「引用 / 代码」样式并写入 block.style | 导出时能还原引用缩进与代码块，为后续样式化导出打底 |
| 25 | docmodel.run_segs | 读取 `wp:docPr@descr` 作为图片替代文字 | 图注可用于无障碍与后续图像审查上下文 |
| 26 | docmodel.parse_docx | 标题优先取文档属性的 title | 标题不再被「目录」「Page 1」抢占 |
| 27 | docmodel._docx_notes | 抽取 footnotes.xml / endnotes.xml | 学术笔记的脚注来源不再整段丢失 |
| 28 | docmodel.export_docx | 列表按 list_level 递进缩进 | 导出的 Word 保留层级观感 |
| 29 | docmodel.parse_text | Markdown 表格识别为 table block | 知识点对照表不再退化成一堆竖线 |
| 30 | docmodel._guess_title | 标题推断分级 + 噪声词过滤（页码/日期/目录） | 大量笔记的标题从噪声变成真正的标题 |
| 31 | review.clean_replacement | 去掉「修改后：」前缀与整体包裹的引号 | 采纳后不再把「修改后：」四个字写进正文 |
| 32 | review.keep_annotation | 过滤占位符替换、无理由的整句改写、两字批注 | 批注列表里的噪声显著减少 |
| 33 | review.dedupe_conflicts | 同一处文字只保留严重度最高的一条整句替换 | 修掉「两条冲突改写都采纳导致半句A半句B」 |
| 34 | pipeline._doc_context | 每块送审都带上全文标题与大纲 | 模型不再把上文定义过的术语当「缺失知识」重复补充 |
| 35 | pipeline.review_document | 每块带上「紧接上文」的末两句 | 跨块被切断的句子不再被误判为病句 |
| 36 | pipeline._system_review | 审校力度三档（轻/标准/严格） | 用户可按笔记类型调节批注密度 |
| 37 | pipeline._system_review | 术语白名单注入 prompt | 专有名词、自造写法不会被当成错别字改掉 |
| 38 | pipeline.summarize | 长文档改 map-reduce，先分段提要点再汇总 | 摘要不再只看前 6000 字，长笔记后半篇终于进得来 |
| 39 | pipeline.summarize | 要点/大纲去重与限长 | 摘要不再出现三条一样的要点 |
| 40 | pipeline.analyze_images | 图像送审带上作者写的替代文字 | 有 alt 的图能被更准确地判读 |
