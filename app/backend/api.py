"""JS <-> Python 桥接层。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback

from . import docmodel, history as hist, logs, media, pipeline, relayout as rl, report as rpt, review
from .llm import LLM, LLMError
from .pipeline import analyze_images, review_document, summarize

DOC_EXT = (".docx", ".doc", ".txt", ".md", ".markdown", ".text")
MEDIA_SUFFIX = tuple("." + e for e in sorted(media.MEDIA_EXT))
SUPPORTED = DOC_EXT + MEDIA_SUFFIX

RELAYOUT_DEFAULTS = {
    "add_toc": True, "number_headings": True, "merge_lines": True,
    "normalize_text": True, "add_summary": True, "ensure_heading": True,
    "normalize_list": True, "drop_empty": True, "dedup_headings": True,
    "split_long_paras": True, "number_style": "cn",
}

# 导入体积上限：python-docx 会把整份 XML 读进内存，
# 几百 MB 的 docx 在 8G 内存的机器上会直接把进程拖死。
MAX_IMPORT_BYTES = 200 * 1024 * 1024
WARN_IMPORT_BYTES = 60 * 1024 * 1024

FILE_TYPES = (
    "笔记与多媒体 (*.docx;*.doc;*.txt;*.md;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;"
    "*.mp4;*.mov;*.mkv;*.avi;*.webm;*.png;*.jpg;*.jpeg;*.bmp;*.webp)",
    "笔记文档 (*.docx;*.doc;*.txt;*.md)",
    "语音 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma;*.amr)",
    "视频 (*.mp4;*.mov;*.mkv;*.avi;*.webm;*.flv;*.wmv)",
    "图片 (*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif)",
    "所有文件 (*.*)",
)


def _sha(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


def _app_version() -> str:
    try:
        import main as _m
        return getattr(_m, "VERSION", "1.2.0")
    except Exception:
        return "1.2.0"


class Api:
    def __init__(self, workdir: str, get_window, asset_root: str):
        self.workdir = workdir
        self.get_window = get_window
        self.asset_root = asset_root
        self.cfg_path = os.path.join(workdir, "config.json")
        logs.setup(workdir)
        self.cfg = self._load_cfg()
        # 审查历史：默认与程序目录同级（本机在 I 盘），不可写时按优先级回退
        self.app_root = os.path.dirname(os.path.abspath(workdir))
        self.history_dir, self.history_source, self.history_warning = hist.resolve_history_dir(
            self.cfg.get("history_dir", ""), self.app_root)
        self.history = hist.HistoryStore(
            self.history_dir, keep=int(self.cfg.get("history_keep", hist.KEEP_DEFAULT) or hist.KEEP_DEFAULT))
        self.history_current = None   # 当前展示的历史快照 id（None = 实时结果或未审查）

        self.doc = None            # 解析后的文档
        self.anns = []             # 批注列表
        self.summary = {}
        self.image_report = {}     # 图像审查结果
        self.relaid = None
        self.relayout_on = False
        self.blocks_current = None
        self.busy = False
        self._lock = threading.Lock()
        # 审查取消信号：长文档用 8B 模型要跑十几分钟，必须能中途停下
        self._cancel = threading.Event()
        # 支持「把文件拖到快捷方式/图标上」直接打开
        self.pending_path = None
        for a in sys.argv[1:]:
            if os.path.isfile(a) and os.path.splitext(a)[1].lower() in SUPPORTED:
                self.pending_path = os.path.abspath(a)
                break

    # ------------------------------------------------------------ 配置
    def _load_cfg(self) -> dict:
        default = {
            "provider": "ollama",
            "base": "http://127.0.0.1:11434",
            "model": "",
            "api_key": "",
            "num_ctx": 8192,
            "vision_model": "",
            "vision_max_images": 500,
            "vision_concurrency": 3,
            "ffmpeg_path": "",
            "asr_base": "",
            "asr_model": "whisper-1",
            "asr_key": "",
            "asr_local_model": "small",
            "video_max_frames": 12,
            "history_dir": "",
            "history_keep": 8,
            "history_keep_days": 0,
            "review_level": "normal",
            "glossary": [],
            "relayout": dict(RELAYOUT_DEFAULTS),
            "recent": [],
        }
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("配置文件不是 JSON 对象")
            default.update({k: v for k, v in d.items() if v is not None})
            default["relayout"] = {**RELAYOUT_DEFAULTS, **(d.get("relayout") or {})}
        except FileNotFoundError:
            pass
        except Exception as e:
            # 配置损坏时不要静默吞掉，也不要直接覆盖用户原来的文件：
            # 备份成 config.bad-<时间>.json，再用默认值启动。
            bad = self.cfg_path.replace(".json", "") + time.strftime(
                ".bad-%Y%m%d%H%M%S", time.localtime()) + ".json"
            try:
                shutil.copy2(self.cfg_path, bad)
            except Exception:
                bad = ""
            logs.get().warning(f"配置文件读取失败（{e}），已备份到 {bad or '（备份失败）'}，"
                               "本次使用默认配置启动")
        return default

    def _save_cfg(self):
        """原子写：先写临时文件再 replace。

        旧版直接 open(path,'w')，写到一半崩溃（或磁盘满）会留下半截 JSON，
        下次启动配置全丢 —— 模型名、API Key、历史目录全部回退默认。
        """
        tmp = self.cfg_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.cfg_path)
        except Exception as e:
            logs.get().warning(f"配置保存失败：{e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _llm(self) -> LLM:
        return LLM({
            "provider": self.cfg.get("provider", "ollama"),
            "base": self.cfg.get("base"),
            "model": self.cfg.get("model"),
            "vision_model": self.cfg.get("vision_model", ""),
            "api_key": self.cfg.get("api_key", ""),
            "num_ctx": self.cfg.get("num_ctx", 8192),
        })

    def _vision_status(self) -> dict:
        """视觉能力状态（30 秒内复用，避免每次取状态都探测一遍）。"""
        now = time.time()
        c = getattr(self, "_vs_cache", None)
        if c and now - c[0] < 30:
            return c[1]
        try:
            vs = self._llm().vision_status()
        except Exception:
            vs = {"enabled": False, "model": "", "reason": "无法读取视觉能力", "candidates": []}
        self._vs_cache = (now, vs)
        return vs

    # ------------------------------------------------------------ 启动
    def _log(self, msg: str):
        logs.get().info(msg)

    def _warn(self, msg: str):
        logs.get().warning(msg)

    def _err(self, msg: str):
        logs.exception(msg)

    def boot(self) -> dict:
        self._log("boot() 被前端调用 —— 页面脚本已运行")
        # 顺手清理历史导入残留的图片目录（只动程序自己生成的哈希目录）
        try:
            n = docmodel.prune_assets(self.asset_root, keep=40)
            if n:
                self._log(f"已清理 {n} 个历史图片目录")
        except Exception:
            pass
        # 按天数自动清理旧历史（默认关闭，用户在设置里开启）
        try:
            days = int(self.cfg.get("history_keep_days") or 0)
            if days > 0:
                n = self.history.prune_older_than(days)
                if n:
                    self._log(f"已清理 {n} 条超过 {days} 天的历史记录")
        except Exception:
            pass
        llm = self._llm()
        h = llm.health()
        models = h.get("models") or []
        if not self.cfg.get("model") and models:
            # 优先挑 qwen3 系
            pref = [m for m in models if m.startswith("qwen3")]
            self.cfg["model"] = (pref or models)[0]
            self._save_cfg()
        # 视觉模型没配但本机装了多模态模型 → 自动接上（旧版只会默默降级成占位文案）
        vs = llm.vision_status()
        if not self.cfg.get("vision_model") and vs.get("auto"):
            self.cfg["vision_model"] = vs["auto"]
            self._save_cfg()
            llm.vision_model = vs["auto"]
            vs = llm.vision_status()
            self._log(f"已自动启用视觉模型 {vs.get('model')}")
        return {
            "ok": True,
            "health": h,
            "models": models,
            "model": self.cfg.get("model", ""),
            "visionModel": self.cfg.get("vision_model", ""),
            "vision": vs,
            "media": media.capabilities(self.cfg, llm),
            "provider": self.cfg.get("provider", "ollama"),
            "base": self.cfg.get("base", ""),
            "relayoutOpts": self.cfg.get("relayout", {}),
            "reviewLevel": self.cfg.get("review_level", "normal"),
            "glossary": self.cfg.get("glossary", []),
            "recent": self.cfg.get("recent", [])[:8],
            "pending": self.pending_path,
            "history": self.history_info(),
            "version": _app_version(),
        }

    def set_model(self, model: str) -> dict:
        self.cfg["model"] = model
        self._save_cfg()
        return {"ok": True, "model": model}

    def set_provider(self, provider: str, base: str, api_key: str, model: str,
                     vision_model: str = "") -> dict:
        self.cfg["provider"] = provider
        self.cfg["base"] = base
        self.cfg["api_key"] = api_key
        if model:
            self.cfg["model"] = model
        self.cfg["vision_model"] = vision_model or ""
        self._save_cfg()
        return {"ok": True, "health": self._llm().health()}

    # ------------------------------------------------------------ 环境自检
    def diagnose(self) -> dict:
        """一次性体检：磁盘、模型服务、视觉、ffmpeg、语音、历史目录、日志。

        用户报「不能用」时，先看这里就能定位到是哪一环缺东西，
        而不是让他在设置页里逐个试。
        """
        checks = []

        def add(name, ok, detail, hint=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

        # 磁盘
        try:
            du = shutil.disk_usage(self.workdir)
            free_gb = du.free / 1024 ** 3
            add("磁盘空间", free_gb > 0.5,
                f"可用 {free_gb:.1f} GB（总 {du.total / 1024 ** 3:.0f} GB）",
                "" if free_gb > 0.5 else "空间不足，审查历史与图片可能写入失败")
        except Exception as e:
            add("磁盘空间", False, f"无法读取：{e}")

        # 模型服务
        try:
            llm = self._llm()
            h = llm.health()
            if h.get("ok"):
                add("模型服务", True,
                    f"{h.get('provider')} 可用，{len(h.get('models') or [])} 个模型")
            else:
                add("模型服务", False, h.get("msg") or "无法连接",
                    "本机模式请先启动 Ollama（ollama serve）；"
                    "也可在设置里改用 OpenAI 兼容 API")
        except Exception as e:
            add("模型服务", False, f"探测失败：{e}")

        # 视觉
        try:
            vs = self._vision_status()
            if vs.get("enabled"):
                add("图像理解", True, f"已启用 {vs.get('model')}")
            else:
                add("图像理解", False, vs.get("reason") or "未启用视觉模型",
                    "图片仍会按原文位置逐张标注，只是不做内容识别；"
                    "装一个多模态模型（如 qwen2.5vl）即可自动启用")
        except Exception as e:
            add("图像理解", False, f"探测失败：{e}")

        # ffmpeg
        try:
            ff, fp = media.find_ffmpeg(self.cfg)
            add("ffmpeg", bool(ff), ff or "未找到",
                "" if ff else "语音/视频笔记需要它抽音轨与关键帧；可在设置里填 ffmpeg.exe 路径")
        except Exception as e:
            add("ffmpeg", False, f"探测失败：{e}")

        # 语音识别
        try:
            st = media.asr_status(self.cfg)
            add("语音转写", st.get("ok"), st.get("detail") or "", 
                "" if st.get("ok") else "语音笔记会只保留时长信息，无法生成可审查的文字")
        except Exception as e:
            add("语音转写", False, f"探测失败：{e}")

        # 历史目录
        try:
            probe = os.path.join(self.history_dir, ".probe")
            os.makedirs(self.history_dir, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            add("审查历史", True, f"{self.history_dir}（{self.history_source}）",
                self.history_warning or "")
        except Exception as e:
            add("审查历史", False, f"不可写：{e}",
                "结果仍可导出，但下次打开同一文件不会自动载入上次结果")

        bad = [c for c in checks if not c["ok"]]
        return {"ok": True, "checks": checks,
                "problems": len(bad),
                "logPath": logs.log_path(self.workdir),
                "logTail": logs.tail(self.workdir, 40),
                "version": _app_version()}

    def open_log(self) -> dict:
        """打开日志文件夹（报错时让用户能直接把日志发过来）。"""
        try:
            os.makedirs(self.workdir, exist_ok=True)
            os.startfile(self.workdir)  # noqa: S606
            return {"ok": True, "dir": self.workdir,
                    "log": logs.log_path(self.workdir)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def refresh_models(self) -> dict:
        h = self._llm().health()
        llm = self._llm()
        return {"ok": True, "health": h, "models": h.get("models", []),
                "vision": llm.vision_status(),
                "media": media.capabilities(self.cfg, llm)}

    # ------------------------------------------------------------ 多媒体能力
    def set_asr(self, asr_base: str = "", asr_model: str = "", asr_key: str = "",
                asr_local_model: str = "", ffmpeg_path: str = "",
                video_max_frames: int = 0) -> dict:
        """语音识别 / 视频抽帧相关配置。全部留空即为关闭。"""
        self.cfg["asr_base"] = (asr_base or "").strip()
        if asr_model:
            self.cfg["asr_model"] = asr_model.strip()
        if asr_key is not None:
            self.cfg["asr_key"] = asr_key
        if asr_local_model:
            self.cfg["asr_local_model"] = asr_local_model.strip()
        self.cfg["ffmpeg_path"] = (ffmpeg_path or "").strip().strip('"')
        if video_max_frames:
            self.cfg["video_max_frames"] = max(1, min(int(video_max_frames), 60))
        self._save_cfg()
        llm = self._llm()
        return {"ok": True, "media": media.capabilities(self.cfg, llm),
                "vision": llm.vision_status()}

    def media_caps(self) -> dict:
        llm = self._llm()
        return {"ok": True, "media": media.capabilities(self.cfg, llm),
                "vision": llm.vision_status()}

    def detect_ffmpeg(self) -> dict:
        ff, fp = media.find_ffmpeg(self.cfg)
        return {"ok": bool(ff), "ffmpeg": ff, "ffprobe": fp,
                "error": "" if ff else "未找到 ffmpeg，请手动指定 ffmpeg.exe 路径"} 

    # ------------------------------------------------------------ 导入
    def choose_file(self) -> dict:
        win = self.get_window()
        if not win:
            return {"ok": False}
        try:
            import webview
            res = win.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=FILE_TYPES,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False, "canceled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return self.load(path)

    def load(self, path: str) -> dict:
        path = os.path.abspath(path)
        ext = os.path.splitext(path)[1].lower()
        if not os.path.exists(path):
            return {"ok": False, "error": "文件不存在"}
        if os.path.isdir(path):
            return {"ok": False, "error": "这是一个文件夹，请选择具体文件"}
        if self.busy:
            # 审查进行中切换文档会让后台线程把结果写进新文档，直接拒绝
            return {"ok": False, "error": "正在审查中，请等本次审查完成后再导入其他文件"}
        if ext not in SUPPORTED:
            return {"ok": False,
                    "error": f"暂不支持 {ext} 格式；可导入 docx/doc/txt/md，"
                             f"以及 mp3/wav/m4a 等语音、mp4/mov 等视频、png/jpg 等图片"}
        # 超大文件保护（阈值是模块常量，便于测试用小文件验证同一段逻辑）
        try:
            size = os.path.getsize(path)
            if size > MAX_IMPORT_BYTES:
                return {"ok": False,
                        "error": f"文件过大（{size / 1024 / 1024:.0f} MB）。"
                                 "本程序面向个人笔记，建议拆分后再导入，"
                                 "或先用 Word 另存为 .docx（去掉历史版本与批注）压缩体积。"}
            if size > WARN_IMPORT_BYTES:
                self._warn(f"导入较大文件 {size / 1024 / 1024:.0f} MB：{path}")
        except Exception:
            pass

        with self._lock:
            doc_id = _sha(path + str(os.path.getmtime(path)))
            store = docmodel.ImageStore(self.asset_root, doc_id)
            try:
                if ext == ".docx":
                    parsed = docmodel.parse_docx(path, store)
                elif ext == ".doc":
                    parsed = docmodel.parse_doc(path, store)
                elif ext.lstrip(".") in media.MEDIA_EXT:
                    # 语音/视频/图片：先经多模态链路转成文本 + 图片，再走同一条审查流程
                    self._push("progress", {"msg": "正在解析多媒体…", "pct": 0.01})
                    parsed = media.parse_media(
                        path, store, self._llm(), self.cfg,
                        on_progress=lambda m, p: self._push(
                            "progress", {"msg": m, "pct": round(0.01 + 0.09 * p, 3)}))
                    parsed["source_type"] = parsed.get("source_type")
                else:
                    parsed = docmodel.parse_text(path, store)
            except Exception as e:
                self._err(f"解析失败：{path}")
                return {"ok": False, "error": f"解析失败：{e}"}

            self._install_doc(parsed, path, doc_id, store)

            # 同一文件若审查过，直接把上次结果接回来，不重复送模型
            restored = self._restore_history(path)

            recent = [p for p in self.cfg.get("recent", []) if p != path]
            recent.insert(0, path)
            self.cfg["recent"] = recent[:12]
            self._save_cfg()

            return self._payload(extra={"loaded": True, "history": restored})

    def _install_doc(self, parsed: dict, path: str, doc_id: str, store) -> None:
        """把解析结果装进当前会话（load / load_images 共用）。"""
        self.doc = {
            "title": parsed["title"],
            "blocks": parsed["blocks"],
            "path": path,
            "doc_id": doc_id,
            "asset_dir": store.dir,
            "source_type": parsed.get("source_type") or "doc",
            "media": parsed.get("media") or {},
        }
        self.anns = []
        self.summary = {}
        self.image_report = {}
        self.relaid = None
        self.relayout_on = False
        self.blocks_current = parsed["blocks"]
        self._img_doc = None          # 让图片计数缓存失效
        self._view_cache = None

    def choose_images(self) -> dict:
        """一次选多张图片，合成一篇图片笔记。"""
        win = self.get_window()
        if not win:
            return {"ok": False}
        try:
            import webview
            res = win.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("图片 (*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.gif)",
                            "所有文件 (*.*)"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False, "canceled": True}
        paths = [res] if isinstance(res, str) else list(res)
        return self.load_images(paths)

    def load_images(self, paths) -> dict:
        if self.busy:
            return {"ok": False, "error": "正在审查中，请等本次审查完成后再导入"}
        paths = [os.path.abspath(p) for p in (paths or []) if os.path.isfile(p)]
        if not paths:
            return {"ok": False, "error": "没有可导入的图片"}
        for p in paths:
            if os.path.splitext(p)[1].lower().lstrip(".") not in media.IMAGE_EXT:
                return {"ok": False, "error": f"不是支持的图片格式：{os.path.basename(p)}"}
        with self._lock:
            # 多图合成一篇：doc_id 取所有路径的联合哈希，顺序变化不影响结果
            doc_id = _sha("|".join(sorted(paths)) + str(len(paths)))
            store = docmodel.ImageStore(self.asset_root, doc_id)
            title = ("、".join(os.path.splitext(os.path.basename(p))[0] for p in paths[:3])
                     + ("等" if len(paths) > 3 else ""))
            try:
                self._push("progress", {"msg": "正在解析图片…", "pct": 0.02})
                parsed = media.parse_many_images(
                    paths, store, self._llm(), title,
                    on_progress=lambda m, p: self._push(
                        "progress", {"msg": m, "pct": round(0.02 + 0.08 * p, 3)}))
            except Exception as e:
                self._err("多图导入失败")
                return {"ok": False, "error": f"解析失败：{e}"}
            anchor = paths[0]
            self._install_doc(parsed, anchor, doc_id, store)
            recent = [p for p in self.cfg.get("recent", []) if p != anchor]
            recent.insert(0, anchor)
            self.cfg["recent"] = recent[:12]
            self._save_cfg()
            return self._payload(extra={"loaded": True, "imageCount": len(paths)})

    def load_recent(self, path: str) -> dict:
        return self.load(path)

    # ------------------------------------------------------------ 审查历史
    def history_info(self) -> dict:
        """历史存储位置与占用情况（设置页 / 启动时用）。"""
        try:
            s = self.history.summary()
        except Exception:
            s = {"files": 0, "entries": 0, "bytes": 0}
        return {
            "dir": self.history_dir,
            "source": self.history_source,
            "warning": self.history_warning,
            "files": s.get("files", 0),
            "entries": s.get("entries", 0),
            "bytes": s.get("bytes", 0),
            "keep": self.history.keep,
        }

    def _history_block(self) -> dict:
        if not self.doc:
            return {"count": 0, "entries": [], "current": None}
        try:
            ents = self.history.entries(self.doc["path"])
        except Exception:
            ents = []
        return {"count": len(ents), "entries": ents,
                "current": self.history_current, "dir": self.history_dir}

    @staticmethod
    def _fingerprint(path: str):
        try:
            st = os.stat(path)
            return int(st.st_size), float(st.st_mtime)
        except Exception:
            return 0, 0.0

    def _is_stale(self, entry: dict, path: str) -> bool:
        """文件在审查之后被改过？只影响「是否自动套用」，不影响能不能查看。"""
        size, mtime = self._fingerprint(path)
        e_size = int(entry.get("file_size") or 0)
        e_mtime = float(entry.get("file_mtime") or 0)
        if not e_size or not e_mtime:
            return False
        return e_size != size or abs(e_mtime - mtime) > 1.0

    def _apply_snapshot(self, snap: dict) -> tuple[int, int]:
        """把快照套回当前文档，返回 (采用条数, 丢弃条数)。"""
        anns = snap.get("anns") or []
        ids = {b["id"] for b in self.doc["blocks"]} if self.doc else set()
        dropped = 0
        if ids:
            kept = [a for a in anns if a.get("block_id") in ids]
            dropped = len(anns) - len(kept)
            anns = kept
        self.anns = anns
        self.summary = snap.get("summary") or {}
        self.image_report = snap.get("image_report") or {}
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return len(anns), dropped

    def _restore_history(self, path: str) -> dict:
        """打开文件时尝试自动恢复最近一次审查，避免重复送模型。"""
        self.history_current = None
        try:
            ents = self.history.entries(path)
        except Exception as e:
            return {"count": 0, "entries": [], "autoLoaded": False, "error": str(e)}
        if not ents:
            return {"count": 0, "entries": [], "autoLoaded": False}
        latest = ents[0]
        snap = self.history.read(path, latest["id"])
        if not snap:
            return {"count": len(ents), "entries": ents, "autoLoaded": False}
        stale = self._is_stale(latest, path)
        if stale:
            # 文件在审查之后改过：不静默套用旧批注（段落可能已经错位），
            # 但仍把历史列出来，让用户自己决定是查看旧结果还是重新审查。
            return {"count": len(ents), "entries": ents, "autoLoaded": False,
                    "staleAvailable": latest["id"],
                    "staleTs": latest.get("ts") or ""}
        _, dropped = self._apply_snapshot(snap)
        self.history_current = {"id": latest["id"], "ts": latest.get("ts") or "",
                                "stale": False, "auto": True}
        return {"count": len(ents), "entries": ents, "autoLoaded": True,
                "current": self.history_current, "dropped": dropped}

    def _save_history(self, elapsed: float):
        if not self.doc:
            return None
        try:
            size, mtime = self._fingerprint(self.doc["path"])
            snap = {
                "anns": self.anns,
                "summary": self.summary,
                "image_report": self.image_report,
                "model": self.cfg.get("model", ""),
                "vision": self.cfg.get("vision_model", ""),
                "elapsed": elapsed,
                "file_size": size,
                "file_mtime": mtime,
                "blocks": len(self.doc["blocks"]),
                "images": docmodel.count_images(self.doc["blocks"]),
                "title": self.doc.get("title", ""),
            }
            entry = self.history.save(self.doc["path"], snap)
            self.history_current = {"id": entry["id"], "ts": entry.get("ts") or "",
                                    "stale": False, "auto": False}
            self._log(f"已保存审查历史 {entry['id']}（{self.doc['path']}）")
            return entry
        except Exception as e:
            self._log(f"历史保存失败：{e}")
            return None

    def history_load(self, entry_id: str) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        snap = self.history.read(self.doc["path"], entry_id)
        if not snap:
            return {"ok": False, "error": "这条历史记录不存在或已损坏"}
        kept, dropped = self._apply_snapshot(snap)
        stale = self._is_stale({"file_size": snap.get("file_size"),
                                "file_mtime": snap.get("file_mtime")}, self.doc["path"])
        self.history_current = {"id": entry_id, "ts": snap.get("ts") or "",
                                "stale": stale, "auto": False}
        return self._payload(extra={"historyLoaded": True, "kept": kept, "dropped": dropped})

    def history_delete(self, entry_id: str) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        self.history.delete(self.doc["path"], entry_id)
        if self.history_current and self.history_current.get("id") == entry_id:
            self.history_current = None
        return self._payload(extra={"historyDeleted": entry_id})

    def history_clear(self) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        n = self.history.clear(self.doc["path"])
        self.history_current = None
        return self._payload(extra={"historyCleared": n})

    def open_history_dir(self) -> dict:
        """一键打开历史记录所在文件夹。"""
        try:
            os.makedirs(self.history_dir, exist_ok=True)
            os.startfile(self.history_dir)  # noqa: S606  (Windows 专用)
            return {"ok": True, "dir": self.history_dir}
        except Exception as e:
            return {"ok": False, "error": f"无法打开文件夹：{e}", "dir": self.history_dir}

    def choose_history_dir(self) -> dict:
        win = self.get_window()
        if not win:
            return {"ok": False}
        try:
            import webview
            res = win.create_file_dialog(webview.FOLDER_DIALOG, directory=self.history_dir)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False, "canceled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return self.set_history_dir(path)

    def set_history_dir(self, path: str) -> dict:
        """切换历史目录（已有历史不会被搬走，只影响之后的写入）。"""
        self.cfg["history_dir"] = path or ""
        self._save_cfg()
        self.history_dir, self.history_source, self.history_warning = hist.resolve_history_dir(
            self.cfg.get("history_dir", ""), self.app_root)
        self.history = hist.HistoryStore(
            self.history_dir, keep=int(self.cfg.get("history_keep", hist.KEEP_DEFAULT) or hist.KEEP_DEFAULT))
        return {"ok": True, "history": self.history_info()}

    def history_open_entry_folder(self, entry_id: str) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        d = self.history.dir_for(self.doc["path"])
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
            return {"ok": True, "dir": d}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def history_set_note(self, entry_id: str, note: str) -> dict:
        """给一次审查加备注，方便日后分辨「哪次是我改完之后的」。"""
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        ok = self.history.set_note(self.doc["path"], entry_id, note)
        if not ok:
            return {"ok": False, "error": "这条历史记录不存在"}
        return self._payload(extra={"noteSaved": entry_id})

    def history_compare(self, id_a: str, id_b: str) -> dict:
        """对比两次审查的批注差异。"""
        if not self.doc:
            return {"ok": False, "error": "请先导入文档"}
        r = self.history.compare(self.doc["path"], id_a, id_b)
        if not r:
            return {"ok": False, "error": "对比失败：至少有一条记录不存在或已损坏"}
        return {"ok": True, **r}

    def history_search(self, q: str = "") -> dict:
        """跨文件搜索审查记录。"""
        try:
            items = self.history.search(q, limit=100)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "items": items, "query": q}

    def history_prune(self, days: int = 0) -> dict:
        """清理超过 N 天的历史。days=0 时按配置 history_keep_days。"""
        d = int(days or self.cfg.get("history_keep_days") or 0)
        if d <= 0:
            return {"ok": False, "error": "请指定要保留的天数（例如 90）"}
        n = self.history.prune_older_than(d)
        return {"ok": True, "removed": n, "days": d, "history": self.history_info()}

    # ------------------------------------------------------------ 最近打开
    def recent_remove(self, path: str) -> dict:
        rec = [p for p in self.cfg.get("recent", []) if p != path]
        self.cfg["recent"] = rec
        self._save_cfg()
        return {"ok": True, "recent": rec[:12]}

    def recent_clear(self) -> dict:
        self.cfg["recent"] = []
        self._save_cfg()
        return {"ok": True, "recent": []}

    # ------------------------------------------------------------ 视图数据
    def _ser_blocks(self, blocks, anns, mode):
        out = []
        # 一次性把批注按段落分组：旧版每渲染一段都要扫完整张批注表，
        # 1000 段 × 500 条批注 = 50 万次比较，三份视图就是 150 万次。
        grouped: dict = {}
        for a in anns:
            grouped.setdefault(a["block_id"], []).append(a)
        empty: list = []
        for b in blocks:
            if b["kind"] == "table":
                # 表格单元格不参与批注渲染，直接给纯 token，省掉一次分组查找
                rows = []
                for row in b.get("rows", []):
                    rows.append([{"tokens": review.render_tokens(
                        {"id": b["id"], "kind": "para", "segs": c.get("segs", [])},
                        empty, "original")} for c in row])
                out.append({"id": b["id"], "kind": "table", "rows": rows,
                            "level": 0, "no": ""})
                continue
            out.append({
                "id": b["id"],
                "kind": b["kind"],
                "level": b.get("level", 0),
                "no": b.get("heading_no", ""),
                "style": b.get("style") or {},
                "tokens": review.render_tokens(b, grouped.get(b["id"], empty), mode),
            })
        return out

    def _ann_sig(self):
        """批注状态指纹：状态没变就不必重算三份视图。

        一千段的文档每次点「保留」都要重新渲染 3 × 1000 个 block 的 token，
        前端还要接一个几 MB 的 JSON —— 这是交互卡顿的主因。
        """
        return tuple((a.get("id"), a.get("status")) for a in self.anns)

    def _image_count(self) -> int:
        """图片总数只算一次：它在文档生命周期内不会变，
        但 _payload 每次状态切换都会被调用。"""
        doc = self.doc
        if doc is None:
            return 0
        if getattr(self, "_img_doc", None) is not doc:
            self._img_doc = doc
            self._img_n = docmodel.count_images(doc["blocks"])
        return self._img_n

    def _toc(self, blocks):
        return [{"id": b["id"], "level": b.get("level", 1),
                 "text": "".join(s["v"] for s in b["segs"] if s["t"] == "text")[:60]}
                for b in blocks if b["kind"] == "heading"]

    def _payload(self, extra: dict | None = None) -> dict:
        if not self.doc:
            return {"ok": True, "doc": None}
        anns = self.anns
        blocks = self.doc["blocks"]
        cur = self.blocks_current or blocks

        sig = self._ann_sig()
        cache = getattr(self, "_view_cache", None)
        # 缓存键带上 anns 列表本身的引用：重新审查会换掉整个列表对象，
        # 即使新旧批注的 id/status 恰好相同也不会误用旧视图。
        if (cache and cache[0] is self.doc and cache[1] is self.anns
                and cache[2] == sig):
            views, toc = cache[3], cache[4]
        else:
            views = {
                "original": self._ser_blocks(blocks, anns, "original"),
                "annotated": self._ser_blocks(blocks, anns, "annotated"),
                # 修正版基于「原始 blocks + 已采纳修改」渲染，不能拿已经改过的
                # blocks_current 再套一遍，否则会二次应用、句子错位。
                "revised": self._ser_blocks(blocks, anns, "revised"),
            }
            toc = self._toc(blocks)
            self._view_cache = (self.doc, self.anns, sig, views, toc)
        payload = {
            "ok": True,
            "doc": {
                "title": self.doc["title"],
                "path": self.doc["path"],
                "name": os.path.basename(self.doc["path"]),
                "images": self._image_count(),
                "blocks": len(blocks),
                "asset_dir": self.doc["asset_dir"],
                "doc_id": self.doc["doc_id"],
                "source_type": self.doc.get("source_type", "doc"),
                "media": self.doc.get("media") or {},
            },
            "anns": anns,
            "stats": review.stats(anns),
            "views": views,
            "toc": toc,
            "relayoutOn": self.relayout_on,
            "summary": self.summary,
            "imageReport": self.image_report or {"total": 0, "items": [], "note": ""},
            "model": self.cfg.get("model", ""),
            "visionModel": self.cfg.get("vision_model", ""),
            "vision": self._vision_status(),
            "busy": self.busy,
            "history": self._history_block(),
        }
        if self.relaid:
            payload["views"]["relayout"] = {
                # relaid 里的文字已经是改过的，直接用纯 token 渲染
                "blocks": self._ser_blocks(self.relaid["blocks"], anns, "original"),
                "toc": self.relaid.get("toc", []),
                "stats": self.relaid.get("stats", {}),
                "summary": self.relaid.get("summary", ""),
                "key_points": self.relaid.get("key_points", []),
                "gaps": self.relaid.get("gaps", []),
            }
        if extra:
            payload.update(extra)
        return payload

    def state(self) -> dict:
        return self._payload()

    # ------------------------------------------------------------ 审查
    def _push(self, evt: str, data: dict):
        win = self.get_window()
        if not win:
            return
        try:
            win.evaluate_js(
                f"window.__evt && window.__evt({json.dumps(evt)}, "
                f"{json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass

    def cancel_review(self) -> dict:
        """请求中止当前审查。当前分块跑完后停下，已完成的批注全部保留。"""
        if not self.busy:
            return {"ok": False, "error": "当前没有正在进行的审查"}
        self._cancel.set()
        self._log("用户请求取消审查")
        return {"ok": True, "message": "已请求停止，正在收尾（已完成的分块会保留）"}

    def run_review(self, do_summary: bool = True) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入笔记"}
        if self.busy:
            return {"ok": False, "error": "正在处理中，请稍候"}
        # 多媒体笔记若没拿到文字（例如语音未转写成功），提前说清楚，别产出一份空审查
        text_len = sum(len(docmodel.block_text_only(b)) for b in self.doc["blocks"]
                       if b["kind"] != "table")
        if text_len < 20:
            mt = (self.doc.get("media") or {}).get("reason") or ""
            return {"ok": False, "error": ("这份笔记里没有足够的文字可供审查"
                                           + (f"：{mt}" if mt else "")
                                           + "。请先补充文字，或在设置里配置语音识别后重新导入。")}
        llm = self._llm()
        if not self.cfg.get("model"):
            h = llm.health()
            if h.get("models"):
                self.cfg["model"] = h["models"][0]
                llm.model = self.cfg["model"]
            else:
                return {"ok": False, "error": "未检测到可用模型，请先启动 Ollama"}

        self.busy = True
        self._cancel.clear()
        t0 = time.time()

        def prog(msg, pct):
            self._push("progress", {"msg": msg, "pct": round(pct, 3)})

        try:
            prog("正在准备…", 0.02)
            rep: dict = {}
            anns = review_document(
                self.doc, llm, on_progress=prog, report=rep,
                level=str(self.cfg.get("review_level") or "normal"),
                glossary=self.cfg.get("glossary") or [],
                should_stop=self._cancel.is_set)
            self.anns = anns
            canceled = bool(rep.get("canceled"))
            if canceled:
                # 取消时跳过图像理解与摘要（这两个最耗时），直接落盘已完成的部分
                self._log(f"审查已取消，保留 {len(anns)} 条批注")
            else:
                # 图像理解：生成说明、标注位置、识别敏感/不一致/需人工核对
                self.image_report = analyze_images(
                    self.doc, llm, on_progress=prog,
                    max_images=int(self.cfg.get("vision_max_images", 300) or 300),
                    concurrency=int(self.cfg.get("vision_concurrency", 3) or 3))
                self.anns.extend(review.image_annotations(self.image_report, len(self.anns)))
                if do_summary:
                    self.summary = summarize(self.doc, llm, on_progress=prog)
                    ir = self.image_report or {}
                    if ir.get("total"):
                        flagged = sum(1 for it in ir.get("items", [])
                                      if it.get("sensitive") or it.get("inconsistent") or it.get("needs_check"))
                        note = (f"全文共 {ir.get('total')} 张图片，已自动分析 {ir.get('analyzed', 0)} 张"
                                + (f"，其中 {flagged} 张已标注（敏感/不一致/需人工核对）。"
                                   if flagged else "，未发现明显图像问题。"))
                        self.summary["summary"] = (self.summary.get("summary", "") + " " + note).strip()
                        self.summary["images"] = ir
            self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
            prog("已取消" if canceled else "审查完成", 1.0)
            self._rebuild_relayout()
            elapsed = round(time.time() - t0, 1)
            self._last_elapsed = elapsed
            entry = self._save_history(elapsed)      # 自动持久化本次审查
            self.busy = False
            warnings = []
            if canceled:
                warnings.append(f"审查已取消：完成 {rep.get('done_chunks', 0)}/{rep.get('chunks', '?')} 段，"
                                f"保留 {len(anns)} 条批注。未完成的部分可稍后重新审查。")
            if rep.get("failed"):
                detail = "；".join(f"第 {f['chunk']} 段（{f['error'][:60]}）"
                                   for f in rep["failed"][:3])
                warnings.append(f"有 {len(rep['failed'])}/{rep.get('chunks', '?')} 段审查失败："
                                f"{detail}。其余部分已完成，可稍后重新审查补齐。")
            if rep.get("empty") and not canceled:
                warnings.append(f"有 {rep['empty']} 段模型没有给出任何意见（可能是原文质量较好，"
                                "也可能该段被截断）。")
            if rep.get("dropped"):
                warnings.append(f"已自动过滤 {rep['dropped']} 条低质量建议"
                                "（占位符替换、没写理由的整句改写等）。")
            if not entry:
                warnings.append("本次结果未能写入审查历史，请检查历史目录是否可写。")
            return self._payload(extra={
                "elapsed": elapsed,
                "reviewed": True,
                "canceled": canceled,
                "historySaved": entry,
                "reviewReport": rep,
                "warnings": warnings,
            })
        except LLMError as e:
            self.busy = False
            return {"ok": False, "error": str(e)}
        except Exception as e:
            self._err("审查出错")
            self.busy = False
            return {"ok": False, "error": f"审查出错：{e}"}

    def add_manual_note(self, block_id: str, text: str, typ: str = "存疑待核") -> dict:
        """用户自己加一条批注。

        id 用 uuid：旧版取「毫秒时间戳 % 100000」，连续加几条会撞 id，
        点一条批注却高亮了另一条。
        """
        import uuid
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "批注内容不能为空"}
        if self.doc and block_id not in {b["id"] for b in self.doc["blocks"]}:
            return {"ok": False, "error": "找不到对应的段落"}
        if typ not in review.TYPES:
            typ = "存疑待核"
        self.anns.append({
            "id": f"m{uuid.uuid4().hex[:10]}",
            "block_id": block_id, "op": "comment", "type": typ,
            "target": "", "replacement": "", "reason": text,
            "severity": "low", "status": "pending", "anchor": None, "sid": None,
        })
        return self._payload()

    def set_replacement(self, ann_id: str, text: str) -> dict:
        """手动微调 AI 给的替换文本。

        AI 的改写方向对、用词不合口味是常事。以前只能整条丢弃再自己改原文，
        现在可以直接在批注里改掉那几个字。
        """
        a = next((x for x in self.anns if x["id"] == ann_id), None)
        if a is None:
            return {"ok": False, "error": "这条批注不存在"}
        if a.get("op") == "comment":
            return {"ok": False, "error": "「仅提醒」类批注没有替换文本可改"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "替换文本不能为空"}
        if len(text) > 4000:
            return {"ok": False, "error": "替换文本过长（上限 4000 字）"}
        if text == (a.get("replacement") or ""):
            return self._payload()
        a["replacement"] = text
        a["edited"] = True
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return self._payload(extra={"edited": ann_id})

    def estimate_review(self) -> dict:
        """审查前预估：会切成几块、大概要跑多久。

        8B 模型审一篇长笔记要十几分钟，用户在按下按钮之前就应该知道
        自己要等多久，而不是看着进度条猜。
        """
        if not self.doc:
            return {"ok": False, "error": "请先导入笔记"}
        try:
            pieces = pipeline._pieces(self.doc["blocks"])
            chunks = pipeline._chunk_pieces(pieces)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        chars = sum(len(docmodel.block_text_only(b)) for b in self.doc["blocks"]
                    if b["kind"] != "table")
        imgs = self._image_count()
        # 经验值：本机 8B 模型约 25 字/秒，视觉模型每张约 3-6 秒
        text_sec = max(5, int(chars / 25))
        img_sec = int(imgs * 4.5) if self._vision_status().get("enabled") else 0
        return {"ok": True, "chunks": len(chunks), "chars": chars, "images": imgs,
                "eta": text_sec + img_sec,
                "vision": bool(self._vision_status().get("enabled")),
                "model": self.cfg.get("model", "")}

    # ------------------------------------------------------------ 取舍
    def set_status(self, ann_id: str, status: str) -> dict:
        if status not in ("accepted", "rejected", "pending"):
            return {"ok": False, "error": f"无效的状态：{status}"}
        hit = None
        for a in self.anns:
            if a["id"] == ann_id:
                a["status"] = status
                hit = a
                break
        if hit is None:
            return {"ok": False, "error": "这条批注不存在（可能已被新一轮审查替换）"}
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return self._payload()

    def bulk(self, action: str, scope: str = "all") -> dict:
        if action not in ("accept", "reject", "reset"):
            return {"ok": False, "error": f"无效的批量操作：{action}"}
        targets = self.anns
        if scope and scope != "all":
            if scope not in review.TYPES:
                return {"ok": False, "error": f"未知的分类：{scope}"}
            targets = [a for a in self.anns if a["type"] == scope]
        if not targets:
            return {"ok": False, "error": "没有符合条件的批注"}
        for a in targets:
            a["status"] = {"accept": "accepted", "reject": "rejected"}.get(action, "pending")
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return self._payload(extra={"affected": len(targets)})

    # ------------------------------------------------------------ 重排
    def set_review_opts(self, level: str = "", glossary=None) -> dict:
        """审校力度与术语白名单。level 留空表示不改。"""
        if level:
            if level not in ("light", "normal", "strict"):
                return {"ok": False, "error": f"未知的审校力度：{level}"}
            self.cfg["review_level"] = level
        if glossary is not None:
            if isinstance(glossary, str):
                glossary = re.split(r"[\n,，、;；]+", glossary)
            terms = []
            for g in glossary:
                g = str(g).strip()
                if g and g not in terms:
                    terms.append(g)
            self.cfg["glossary"] = terms[:200]
        self._save_cfg()
        return {"ok": True, "reviewLevel": self.cfg.get("review_level", "normal"),
                "glossary": self.cfg.get("glossary", [])}

    def _rebuild_relayout(self):
        if not self.relayout_on or not self.doc:
            self.relaid = None
            return
        opts = self.cfg.get("relayout", {})
        meta = self.summary if opts.get("add_summary", True) else {}
        self.relaid = rl.relayout(self.blocks_current, opts, meta)

    def set_relayout(self, on: bool, opts: dict | None = None) -> dict:
        self.relayout_on = bool(on)
        if opts:
            self.cfg["relayout"] = {**self.cfg.get("relayout", {}), **opts}
            self._save_cfg()
        self._rebuild_relayout()
        return self._payload()

    # ------------------------------------------------------------ 导出
    def choose_export_path(self, kind: str) -> dict:
        win = self.get_window()
        if not win or not self.doc:
            return {"ok": False}
        base = os.path.splitext(os.path.basename(self.doc["path"]))[0]
        suffix = {"original": "_原版", "revised": "_修正版", "relayout": "_重排版",
                  "report": "_审阅报告", "csv": "_批注清单"}.get(kind, "_修正版")
        default = base + suffix + time.strftime("_%Y%m%d", time.localtime())
        types = ("Word 文档 (*.docx)", "网页 (*.html)",
                 "Markdown (*.md)", "纯文本 (*.txt)", "CSV 表格 (*.csv)")
        if kind == "csv":
            types = ("CSV 表格 (*.csv)",) + types
        try:
            import webview
            res = win.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default, file_types=types,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False, "canceled": True}
        path = res if isinstance(res, str) else res[0]
        return {"ok": True, "path": path}

    def export(self, kind: str, fmt: str, out_path: str) -> dict:
        if not self.doc:
            return {"ok": False, "error": "没有可导出的内容"}
        # 审阅报告 / 批注清单是独立的交付物，不走正文导出链路
        if kind in ("report", "csv"):
            return self._export_report(kind, fmt, out_path)
        blocks = self._blocks_for(kind)
        if not blocks:
            return {"ok": False, "error": "没有内容"}
        # 修正版/重排版追加「图像说明与核对」，让总结覆盖图像部分
        if kind != "original" and self.image_report:
            blocks = list(blocks) + docmodel.image_report_blocks(self.image_report)
        fmt = (fmt or "").lower().lstrip(".")
        # 用户在保存对话框里手打了扩展名时，以扩展名为准 ——
        # 否则会出现「文件名叫 笔记.md，内容却是 docx」这种打不开的情况。
        ext_on_path = os.path.splitext(out_path)[1].lower().lstrip(".")
        if ext_on_path in ("docx", "html", "htm", "md", "markdown", "txt"):
            fmt = {"htm": "html", "markdown": "md"}.get(ext_on_path, ext_on_path)
        if not fmt:
            fmt = "docx"
        if not out_path.lower().endswith("." + fmt):
            out_path += "." + fmt
        # 用户可能手打了一个不存在的目录
        parent = os.path.dirname(os.path.abspath(out_path))
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"无法创建导出目录 {parent}：{e}"}
        asset_dir = self.doc["asset_dir"]
        try:
            if fmt == "docx":
                docmodel.export_docx(blocks, asset_dir, out_path, self.doc["title"])
            elif fmt == "html":
                docmodel.export_html(blocks, asset_dir, out_path, self.doc["title"])
            elif fmt == "md":
                self._export_md(blocks, out_path)
            elif fmt == "txt":
                self._export_txt(blocks, out_path)
            else:
                return {"ok": False, "error": f"不支持的格式 {fmt}"}
        except Exception as e:
            self._err(f"导出失败：{out_path}")
            return {"ok": False, "error": f"导出失败：{e}"}
        return {"ok": True, "path": out_path, "format": fmt}

    def _export_report(self, kind: str, fmt: str, out_path: str) -> dict:
        """导出审阅报告（html / md）或批注清单（csv）。"""
        fmt = (fmt or "html").lower().lstrip(".")
        if kind == "csv":
            fmt = "csv"
        if fmt not in ("html", "md", "markdown", "csv"):
            return {"ok": False, "error": f"审阅报告不支持 {fmt} 格式（可选 html / md / csv）"}
        fmt = {"markdown": "md"}.get(fmt, fmt)
        ext_on_path = os.path.splitext(out_path)[1].lower().lstrip(".")
        if ext_on_path in ("html", "htm", "md", "markdown", "csv"):
            fmt = {"htm": "html", "markdown": "md"}.get(ext_on_path, ext_on_path)
        if not out_path.lower().endswith("." + fmt):
            out_path += "." + fmt
        parent = os.path.dirname(os.path.abspath(out_path))
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"无法创建导出目录 {parent}：{e}"}
        try:
            if fmt == "csv":
                rpt.write_csv(self.anns, out_path)
            else:
                rep = rpt.collect(
                    self.doc, self.anns, self.summary, self.image_report,
                    review.stats(self.anns),
                    {"model": self.cfg.get("model", ""),
                     "vision": self.cfg.get("vision_model", ""),
                     "elapsed": getattr(self, "_last_elapsed", 0)})
                body = rpt.to_html(rep) if fmt == "html" else rpt.to_markdown(rep)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(body)
        except Exception as e:
            self._err(f"导出报告失败：{out_path}")
            return {"ok": False, "error": f"导出报告失败：{e}"}
        return {"ok": True, "path": out_path, "format": fmt}

    def _blocks_for(self, kind: str):
        if kind == "original":
            return self.doc["blocks"]
        if kind == "relayout":
            if not self.relaid:
                opts = self.cfg.get("relayout", {})
                self.relaid = rl.relayout(self.blocks_current, opts, self.summary)
            blocks = rl.materialize(self.relaid)
            if self.cfg.get("relayout", {}).get("add_summary", True):
                blocks = rl.prepend_summary({"blocks": blocks,
                                             "summary": self.relaid.get("summary", ""),
                                             "key_points": self.relaid.get("key_points", [])})
            return blocks
        return self.blocks_current or self.doc["blocks"]

    def _export_md(self, blocks, out_path):
        d = os.path.dirname(out_path)
        stem = os.path.splitext(os.path.basename(out_path))[0]
        imdir = os.path.join(d, stem + "_图片")
        # 同一份笔记反复导出到同一位置时，旧图片会越积越多。
        # 只清理本程序自己产出的 `NNN_xxx.ext`，绝不碰用户放进去的其他文件。
        if os.path.isdir(imdir):
            for fn in os.listdir(imdir):
                if re.match(r"^\d{3}_.+\.[A-Za-z0-9]+$", fn):
                    try:
                        os.remove(os.path.join(imdir, fn))
                    except Exception:
                        pass
        lines = [f"# {self.doc['title']}", ""]
        n = 0
        for b in blocks:
            if b["kind"] == "table":
                rows = b.get("rows", [])
                if not rows:
                    continue
                # Markdown 表格必须有分隔行，否则渲染出来是一堆竖线
                ncol = max(sum(max(1, int(c.get("span") or 1)) for c in r) for r in rows)
                for ri, row in enumerate(rows):
                    cells, ci = [], 0
                    for c in row:
                        span = max(1, int(c.get("span") or 1))
                        txt = "".join(s["v"] for s in c.get("segs", []) if s["t"] == "text")
                        cells.append(txt.replace("\n", " ").replace("|", "\\|"))
                        ci += span
                    while ci < ncol:
                        cells.append("")
                        ci += 1
                    lines.append("| " + " | ".join(cells) + " |")
                    if ri == 0:
                        lines.append("|" + "|".join([" --- "] * ncol) + "|")
                lines.append("")
                continue
            buf = []
            for s in b["segs"]:
                if s["t"] == "text":
                    buf.append(s["v"])
                else:
                    os.makedirs(imdir, exist_ok=True)
                    n += 1
                    src = os.path.join(self.doc["asset_dir"], s["file"])
                    dst = os.path.join(imdir, f"{n:03d}_{s['file']}")
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        continue
                    buf.append(f"\n![图{n}]({stem}_图片/{os.path.basename(dst)})\n")
            text = "".join(buf).strip()
            if not text:
                continue
            if b["kind"] == "heading":
                lines.append("#" * min(max(b.get("level", 1), 1) + 1, 6) + " " + text)
            else:
                lines.append(text)
            lines.append("")
        open(out_path, "w", encoding="utf-8").write("\n".join(lines))

    def _export_txt(self, blocks, out_path):
        out = [self.doc["title"], "=" * 40, ""]
        n = 0
        for b in blocks:
            if b["kind"] == "table":
                for row in b.get("rows", []):
                    out.append("  ".join(
                        "".join(s["v"] for s in c.get("segs", []) if s["t"] == "text")
                        for c in row))
                out.append("")
                continue
            buf = []
            for s in b["segs"]:
                if s["t"] == "text":
                    buf.append(s["v"])
                else:
                    n += 1
                    buf.append(f"[图片{n}]")
            t = "".join(buf).strip()
            if t:
                out.append(t)
                out.append("")
        open(out_path, "w", encoding="utf-8").write("\n".join(out))

    def open_folder(self, path: str) -> dict:
        try:
            p = path if os.path.isdir(path) else os.path.dirname(path)
            os.startfile(p)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def quit(self) -> dict:
        """退出。审查进行中先问一句 —— 点叉直接丢掉半小时的审查结果太伤。"""
        if self.busy:
            return {"ok": False, "busy": True,
                    "error": "正在审查中，现在退出会丢失本次结果。确定要退出吗？"}
        win = self.get_window()
        if win:
            win.destroy()
        return {"ok": True}
