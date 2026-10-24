# 发行包 · 笔记审查总结系统

本目录存放 **笔记审查总结系统** 的 Windows x64 可运行包体（由桌面快捷方式实际指向的安装/发布目录整理而来）。

| 项目 | 内容 |
| --- | --- |
| 版本 | `1.1.2` |
| 平台 | Windows x64 |
| 包体路径 | `release/1.1.2/note-review-system-1.1.2-win-x64/` |
| 文件数 | 213 |
| 体积 | 约 45.0 MB |

## 运行方式

双击 `笔记审查总结系统.exe`

## 校验完整性

```bash
cd release
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-Content release\SHA256SUMS.txt | ForEach-Object {
  $h, $p = $_ -split '  ', 2
  if ((Get-FileHash $p -Algorithm SHA256).Hash -ne $h.ToUpper()) { "MISMATCH $p" }
}
```

## 关于 笔记审查总结系统

导入 Word / 文本笔记，AI 逐条审校批注，可切换多视图对比。

## 未纳入的内容

开发环境 `.venv/`、`build-env*/`、`build/`，运行期 WebView2 缓存 `data/`、`app/data/`，以及历史备份 `_internal.v*.bak/` 均未纳入。

以上目录属于开发环境、运行期缓存或大体积模型权重，不影响程序本体运行。
