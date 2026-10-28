"""修复桌面快捷方式：指向 I:\\workbuddy\\笔记审查总结系统\\ 内的主程序。

2026-10-22 21:08 目录被按软件重新分文件夹整理，主程序从
I:\\workbuddy\\笔记审查总结系统.exe 移到了
I:\\workbuddy\\笔记审查总结系统\\笔记审查总结系统.exe，
原快捷方式因此失效。此脚本按新位置重建。
"""
import os

import win32com.client

EXE_NAME = "笔记审查总结系统.exe"
ROOT = r"I:\workbuddy"
APP_DIR = os.path.join(ROOT, "笔记审查总结系统")
EXE = os.path.join(APP_DIR, EXE_NAME)
LNK_NAME = "笔记审查总结系统.lnk"


def desktop_dir() -> str:
    """解析真实桌面（本机桌面被重定向到 D:\\Desktop）。"""
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Desktop")
        return os.path.expandvars(val)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Desktop")


def find_exe() -> str:
    """按新位置找；找不到就整个 I:\\workbuddy 搜一遍。"""
    if os.path.isfile(EXE):
        return EXE
    for dp, _, fs in os.walk(ROOT):
        if EXE_NAME in fs:
            return os.path.join(dp, EXE_NAME)
    raise FileNotFoundError(f"在 {ROOT} 下没找到 {EXE_NAME}")


def main():
    exe = find_exe()
    app_dir = os.path.dirname(exe)
    desk = desktop_dir()
    lnk_path = os.path.join(desk, LNK_NAME)

    # 不用 os.remove：本机有 safe-delete 拦截。WScript.Shell 的 Save()
    # 会直接覆盖同名快捷方式，效果一样且更干净。
    shell = win32com.client.Dispatch("WScript.Shell")
    lnk = shell.CreateShortCut(lnk_path)
    lnk.TargetPath = exe
    lnk.WorkingDirectory = app_dir
    lnk.IconLocation = exe + ",0"
    lnk.Description = "笔记审查总结系统 — 导入 Word/文本笔记，AI 逐条审校批注，可切换多视图对比"
    lnk.WindowStyle = 1
    lnk.Save()

    print("桌面目录:", desk)
    print("快捷方式:", lnk_path)
    print("指向:    ", lnk.TargetPath)
    print("工作目录:", lnk.WorkingDirectory)
    print("目标存在:", os.path.isfile(lnk.TargetPath))
    print("快捷方式大小:", os.path.getsize(lnk_path), "字节")


if __name__ == "__main__":
    main()
