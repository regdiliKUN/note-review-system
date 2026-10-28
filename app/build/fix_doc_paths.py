"""把使用说明.html 里的旧路径更新为整理后的新路径。"""
import io

P = r"I:\workbuddy\笔记审查总结系统\使用说明.html"
OLD_ROOT = "I:" + chr(92) + "workbuddy" + chr(92)
NEW_ROOT = "I:" + chr(92) + "workbuddy" + chr(92) + "笔记审查总结系统" + chr(92)

s = io.open(P, encoding="utf-8").read()

# 1) 具体文件/目录路径整体替换（先长后短，避免嵌套替换出错）
pairs = [
    (OLD_ROOT + "笔记审查总结系统.exe", NEW_ROOT + "笔记审查总结系统.exe"),
    (OLD_ROOT + "示例笔记.docx", NEW_ROOT + "示例笔记.docx"),
    (OLD_ROOT + "data" + chr(92) + "assets" + chr(92), NEW_ROOT + "data" + chr(92) + "assets" + chr(92)),
    (OLD_ROOT + "data" + chr(92) + "config.json", NEW_ROOT + "data" + chr(92) + "config.json"),
    (OLD_ROOT + "导出结果" + chr(92), NEW_ROOT + "导出结果" + chr(92)),
]
hit = 0
for a, b in pairs:
    if a in s:
        s = s.replace(a, b)
        hit += 1
        print("替换:", a)
    else:
        print("未找到:", a)

# 2) 剩下的裸 "I:\workbuddy" （不带尾部反斜杠）后面接 </b> 或换行的
tail = "I:" + chr(92) + "workbuddy"
for a, b in [
    ("跳到 <b>" + tail + "</b>", "跳到 <b>" + NEW_ROOT.rstrip(chr(92)) + "</b>"),
    ("主目录：" + tail, "程序目录：" + NEW_ROOT.rstrip(chr(92))),
]:
    if a in s:
        s = s.replace(a, b)
        hit += 1
        print("替换:", a)
    else:
        print("未找到:", a)

io.open(P, "w", encoding="utf-8").write(s)
print(f"\n完成，共替换 {hit} 处")

# 校验：不该再有指向旧位置的路径
rest = [ln.strip() for ln in s.splitlines() if tail in ln and NEW_ROOT not in ln]
print("仍含旧路径的行数:", len(rest))
for ln in rest[:6]:
    print("  ", ln[:100])
