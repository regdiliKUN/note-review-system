"""JS <-> Python 桥接层。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback

from . import docmodel, history as hist, relayout as rl, review
from .llm import LLM, LLMError
from .pipeline import analyze_images, review_document, summarize

SUPPORTED = (".docx", ".doc", ".txt", ".md", ".markdown", ".text")


def _sha(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


class Api:
    def __init__(self, workdir: str, get_window, asset_root: str):
        self.workdir = workdir
        self.get_window = get_window
        self.asset_root = asset_root
        self.cfg_path = os.path.join(workdir, "config.json")
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
            "history_dir": "",
            "history_keep": 8,
            "relayout": {
                "add_toc": True, "number_headings": True,
                "merge_lines": True, "normalize_text": True,
                "add_summary": True, "ensure_heading": True,
            },
            "recent": [],
        }
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            default.update({k: v for k, v in d.items() if v is not None})
            default["relayout"] = {**{"add_toc": True, "number_headings": True,
                                      "merge_lines": True, "normalize_text": True,
                                      "add_summary": True, "ensure_heading": True},
                                   **(d.get("relayout") or {})}
        except Exception:
            pass
        return default

    def _save_cfg(self):
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
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

    # ------------------------------------------------------------ 启动
    def _log(self, msg: str):
        try:
            with open(os.path.join(self.workdir, "app.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def boot(self) -> dict:
        self._log("boot() 被前端调用 —— 页面脚本已运行")
        llm = self._llm()
        h = llm.health()
        models = h.get("models") or []
        if not self.cfg.get("model") and models:
            # 优先挑 qwen3 系
            pref = [m for m in models if m.startswith("qwen3")]
            self.cfg["model"] = (pref or models)[0]
            self._save_cfg()
        return {
            "ok": True,
            "health": h,
            "models": models,
            "model": self.cfg.get("model", ""),
            "visionModel": self.cfg.get("vision_model", ""),
            "provider": self.cfg.get("provider", "ollama"),
            "base": self.cfg.get("base", ""),
            "relayoutOpts": self.cfg.get("relayout", {}),
            "recent": self.cfg.get("recent", [])[:8],
            "pending": self.pending_path,
            "history": self.history_info(),
            "version": "1.1.2",
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

    def refresh_models(self) -> dict:
        h = self._llm().health()
        return {"ok": True, "health": h, "models": h.get("models", [])}

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
                file_types=("笔记文档 (*.doc;*.docx;*.txt;*.md;*.markdown)", "所有文件 (*.*)"),
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
        if ext not in SUPPORTED:
            return {"ok": False, "error": f"暂不支持 {ext} 格式，请用 .doc / .docx / .txt / .md"}

        with self._lock:
            doc_id = _sha(path + str(os.path.getmtime(path)))
            store = docmodel.ImageStore(self.asset_root, doc_id)
            try:
                if ext == ".docx":
                    parsed = docmodel.parse_docx(path, store)
                elif ext == ".doc":
                    parsed = docmodel.parse_doc(path, store)
                else:
                    parsed = docmodel.parse_text(path, store)
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": f"解析失败：{e}"}

            self.doc = {
                "title": parsed["title"],
                "blocks": parsed["blocks"],
                "path": path,
                "doc_id": doc_id,
                "asset_dir": store.dir,
            }
            self.anns = []
            self.summary = {}
            self.image_report = {}
            self.relaid = None
            self.relayout_on = False
            self.blocks_current = parsed["blocks"]

            # 同一文件若审查过，直接把上次结果接回来，不重复送模型
            restored = self._restore_history(path)

            recent = [p for p in self.cfg.get("recent", []) if p != path]
            recent.insert(0, path)
            self.cfg["recent"] = recent[:12]
            self._save_cfg()

            return self._payload(extra={"loaded": True, "history": restored})

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

    # ------------------------------------------------------------ 视图数据
    def _ser_blocks(self, blocks, anns, mode):
        out = []
        for b in blocks:
            if b["kind"] == "table":
                rows = []
                for row in b.get("rows", []):
                    rows.append([{"tokens": review.render_tokens(
                        {"id": b["id"], "kind": "para", "segs": c.get("segs", [])},
                        anns, mode)} for c in row])
                out.append({"id": b["id"], "kind": "table", "rows": rows,
                            "level": 0, "no": ""})
                continue
            out.append({
                "id": b["id"],
                "kind": b["kind"],
                "level": b.get("level", 0),
                "no": b.get("heading_no", ""),
                "tokens": review.render_tokens(b, anns, mode),
            })
        return out

    def _payload(self, extra: dict | None = None) -> dict:
        if not self.doc:
            return {"ok": True, "doc": None}
        anns = self.anns
        blocks = self.doc["blocks"]
        cur = self.blocks_current or blocks

        views = {
            "original": self._ser_blocks(blocks, anns, "original"),
            "annotated": self._ser_blocks(blocks, anns, "annotated"),
            # 修正版基于「原始 blocks + 已采纳修改」渲染，不能拿已经改过的
            # blocks_current 再套一遍，否则会二次应用、句子错位。
            "revised": self._ser_blocks(blocks, anns, "revised"),
        }
        payload = {
            "ok": True,
            "doc": {
                "title": self.doc["title"],
                "path": self.doc["path"],
                "name": os.path.basename(self.doc["path"]),
                "images": docmodel.count_images(blocks),
                "blocks": len(blocks),
                "asset_dir": self.doc["asset_dir"],
                "doc_id": self.doc["doc_id"],
            },
            "anns": anns,
            "stats": review.stats(anns),
            "views": views,
            "toc": [{"id": b["id"], "level": b.get("level", 1),
                     "text": "".join(s["v"] for s in b["segs"] if s["t"] == "text")[:60]}
                    for b in blocks if b["kind"] == "heading"],
            "relayoutOn": self.relayout_on,
            "summary": self.summary,
            "imageReport": self.image_report or {"total": 0, "items": [], "note": ""},
            "model": self.cfg.get("model", ""),
            "visionModel": self.cfg.get("vision_model", ""),
            "busy": self.busy,
            "history": self._history_block(),
        }
        if self.relaid:
            payload["views"]["relayout"] = {
                # relaid 里的文字已经是改过的，直接用纯 token 渲染
                "blocks": self._ser_blocks(self.relaid["blocks"], anns, "original"),
                "toc": self.relaid.get("toc", []),
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

    def run_review(self, do_summary: bool = True) -> dict:
        if not self.doc:
            return {"ok": False, "error": "请先导入笔记"}
        if self.busy:
            return {"ok": False, "error": "正在处理中，请稍候"}
        llm = self._llm()
        if not self.cfg.get("model"):
            h = llm.health()
            if h.get("models"):
                self.cfg["model"] = h["models"][0]
                llm.model = self.cfg["model"]
            else:
                return {"ok": False, "error": "未检测到可用模型，请先启动 Ollama"}

        self.busy = True
        t0 = time.time()

        def prog(msg, pct):
            self._push("progress", {"msg": msg, "pct": round(pct, 3)})

        try:
            prog("正在准备…", 0.02)
            anns = review_document(self.doc, llm, on_progress=prog)
            self.anns = anns
            # 图像理解：生成说明、标注位置、识别敏感/不一致/需人工核对
            self.image_report = analyze_images(
                self.doc, llm, on_progress=prog,
                max_images=int(self.cfg.get("vision_max_images", 300) or 300),
                concurrency=int(self.cfg.get("vision_concurrency", 3) or 3))
            self.anns.extend(review.image_annotations(self.image_report, len(self.anns)))
            if do_summary:
                self.summary = summarize(self.doc, llm)
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
            prog("审查完成", 1.0)
            self._rebuild_relayout()
            elapsed = round(time.time() - t0, 1)
            entry = self._save_history(elapsed)      # 自动持久化本次审查
            self.busy = False
            return self._payload(extra={
                "elapsed": elapsed,
                "reviewed": True,
                "historySaved": entry,
            })
        except LLMError as e:
            self.busy = False
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            self.busy = False
            return {"ok": False, "error": f"审查出错：{e}"}

    def add_manual_note(self, block_id: str, text: str, typ: str = "存疑待核") -> dict:
        """用户自己加一条批注。"""
        self.anns.append({
            "id": f"m{int(time.time() * 1000) % 100000}",
            "block_id": block_id, "op": "comment", "type": typ,
            "target": "", "replacement": "", "reason": text,
            "severity": "low", "status": "pending", "anchor": None,
        })
        return self._payload()

    # ------------------------------------------------------------ 取舍
    def set_status(self, ann_id: str, status: str) -> dict:
        for a in self.anns:
            if a["id"] == ann_id:
                a["status"] = status if status in ("accepted", "rejected", "pending") else "pending"
                break
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return self._payload()

    def bulk(self, action: str, scope: str = "all") -> dict:
        targets = self.anns
        if scope != "all":
            targets = [a for a in self.anns if a["type"] == scope]
        for a in targets:
            if action == "accept":
                a["status"] = "accepted"
            elif action == "reject":
                a["status"] = "rejected"
            else:
                a["status"] = "pending"
        self.blocks_current = review.apply_accepted(self.doc["blocks"], self.anns)
        self._rebuild_relayout()
        return self._payload()

    # ------------------------------------------------------------ 重排
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
        suffix = {"original": "_原版", "revised": "_修正版", "relayout": "_重排版"}.get(kind, "_修正版")
        ext = {"docx": ".docx", "html": ".html", "md": ".md", "txt": ".txt"}
        default = base + suffix
        try:
            import webview
            res = win.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default,
                file_types=("Word 文档 (*.docx)", "网页 (*.html)",
                            "Markdown (*.md)", "纯文本 (*.txt)"),
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
        blocks = self._blocks_for(kind)
        if not blocks:
            return {"ok": False, "error": "没有内容"}
        # 修正版/重排版追加「图像说明与核对」，让总结覆盖图像部分
        if kind != "original" and self.image_report:
            blocks = list(blocks) + docmodel.image_report_blocks(self.image_report)
        fmt = fmt.lower().lstrip(".")
        if not out_path.lower().endswith("." + fmt):
            out_path += "." + fmt
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
            traceback.print_exc()
            return {"ok": False, "error": f"导出失败：{e}"}
        return {"ok": True, "path": out_path}

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
        lines = [f"# {self.doc['title']}", ""]
        n = 0
        for b in blocks:
            if b["kind"] == "table":
                for row in b.get("rows", []):
                    lines.append("| " + " | ".join(
                        "".join(s["v"] for s in c.get("segs", []) if s["t"] == "text")
                        for c in row) + " |")
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
        win = self.get_window()
        if win:
            win.destroy()
        return {"ok": True}
