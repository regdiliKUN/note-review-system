"""审查历史：按「文件」维度持久化每一次审查结果。

设计要点
--------
1. **按文件区分**：同一路径 = 同一份历史，与 mtime 无关（文件被改动后仍能找回旧结果，
   只是会标记为「可能已过期」）。
2. **一次审查一个快照**：`<历史根>/<文件名>_<路径哈希>/<时间戳>.json`，
   旁边一个 `meta.json` 记录该文件的路径、指纹（大小 + 修改时间）与快照索引。
3. **纯 JSON、UTF-8、可读可备份**：不依赖数据库，删目录即清空。
4. **存储位置**：优先 I 盘、默认与程序目录同级；都不可写时回退到
   `%LOCALAPPDATA%\\NoteReview\\history` 并向上层返回明确提示。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

SCHEMA = 1
KEEP_DEFAULT = 8          # 每个文件保留最近多少次审查
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ---------------------------------------------------------------- 目录解析
# 可写性探测要真落一次盘，而 resolve_history_dir 会连着探好几个候选目录。
# 结果在进程内缓存：同一次会话里没必要反复试探同一个路径（I 盘是移动盘时尤其慢）。
_WRITABLE_CACHE: dict = {}


def _writable(d: str) -> bool:
    """真正落一次盘来判断可写，避免「目录能建但写不进」的假阳性。"""
    key = os.path.normcase(os.path.abspath(d))
    hit = _WRITABLE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        ok = True
    except Exception:
        ok = False
    _WRITABLE_CACHE[key] = ok
    return ok


def resolve_history_dir(configured: str, app_root: str) -> tuple[str, str, str]:
    """决定历史目录。返回 (目录, 来源说明, 警告文案)。

    优先级：用户指定 → 程序目录（在 I 盘）→ I 盘独立目录 → 程序目录 → 系统盘兜底。
    """
    # 1) 用户在设置里指定过
    if configured:
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(configured)))
        if _writable(p):
            return p, "用户指定", ""
        # 指定目录不可用不能静默吞掉，要走后面的回退并提示
        warn_prefix = f"指定的历史目录不可写（{p}），已自动回退。"
    else:
        warn_prefix = ""

    drive = os.path.splitdrive(os.path.abspath(app_root))[0].upper()

    # 2) 程序本身就在 I 盘：历史放程序目录下，既满足「优先 I 盘」也满足「跟软件放一起」
    if drive == "I:":
        d = os.path.normpath(os.path.join(app_root, "history"))
        if _writable(d):
            return d, "程序目录（I 盘）", warn_prefix

    # 3) 程序不在 I 盘，但 I 盘可用：把历史单独放 I 盘
    if os.path.isdir("I:\\"):
        d = os.path.normpath(os.path.join("I:\\", "NoteReviewHistory"))
        if _writable(d):
            return d, "I 盘", warn_prefix

    # 4) 程序目录
    d = os.path.normpath(os.path.join(app_root, "history"))
    if _writable(d):
        warn = warn_prefix or "I 盘不可用，历史记录已改为保存在程序目录。"
        return d, "程序目录", warn

    # 5) 最后兜底：系统盘用户目录
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.normpath(os.path.join(base, "NoteReview", "history"))
    _writable(d)
    warn = (warn_prefix or "I 盘与程序目录均不可写，")
    warn += "历史记录已回退到系统盘：" + d + "（建议在设置里改到 I 盘，避免占用 C 盘空间）。"
    return d, "系统盘兜底", warn


# ---------------------------------------------------------------- 工具
def _safe_name(name: str, limit: int = 40) -> str:
    s = _UNSAFE.sub("_", name).strip(" .")
    return (s[:limit] or "untitled")


def file_key(path: str) -> str:
    """同一路径恒定不变的短哈希（大小写、斜杠归一）。"""
    norm = os.path.normcase(os.path.abspath(path))
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:10]


def _atomic_write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- 存储
class HistoryStore:
    def __init__(self, root: str, keep: int = KEEP_DEFAULT):
        self.root = root
        self.keep = max(1, int(keep or KEEP_DEFAULT))

    # -------------------------------------------------- 路径
    def dir_for(self, path: str) -> str:
        name = _safe_name(os.path.splitext(os.path.basename(path))[0])
        return os.path.join(self.root, f"{name}_{file_key(path)}")

    def meta_path(self, path: str) -> str:
        return os.path.join(self.dir_for(path), "meta.json")

    def snap_path(self, path: str, entry_id: str) -> str:
        return os.path.join(self.dir_for(path), f"{entry_id}.json")

    # -------------------------------------------------- 读
    def meta(self, path: str) -> dict:
        d = self.dir_for(path)
        m = _read_json(os.path.join(d, "meta.json")) or {}
        if not m:
            # meta 丢了就按目录里的快照重建索引，不让历史凭空消失
            m = {"schema": SCHEMA, "path": os.path.abspath(path),
                 "name": os.path.basename(path), "entries": []}
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d), reverse=True):
                    if not fn.endswith(".json") or fn == "meta.json":
                        continue
                    snap = _read_json(os.path.join(d, fn))
                    if isinstance(snap, dict):
                        m["entries"].append(self._entry_from_snapshot(snap, fn[:-5]))
        m.setdefault("entries", [])
        return m

    def entries(self, path: str) -> list[dict]:
        """按时间倒序返回该文件的历次审查（只含索引信息，不含正文）。"""
        ents = self.meta(path).get("entries") or []
        ents = [e for e in ents if isinstance(e, dict) and e.get("id")]
        ents.sort(key=lambda e: e.get("epoch") or 0, reverse=True)
        return ents

    def read(self, path: str, entry_id: str):
        return _read_json(self.snap_path(path, entry_id))

    def latest(self, path: str):
        ents = self.entries(path)
        if not ents:
            return None, None
        return ents[0], self.read(path, ents[0]["id"])

    # -------------------------------------------------- 写
    @staticmethod
    def _entry_from_snapshot(snap: dict, entry_id: str) -> dict:
        return {
            "id": entry_id,
            "epoch": snap.get("epoch") or 0,
            "ts": snap.get("ts") or "",
            "model": snap.get("model") or "",
            "vision": snap.get("vision") or "",
            "anns": len(snap.get("anns") or []),
            "images": (snap.get("image_report") or {}).get("total") or 0,
            "analyzed": (snap.get("image_report") or {}).get("analyzed") or 0,
            "elapsed": snap.get("elapsed") or 0,
            "file_size": snap.get("file_size") or 0,
            "file_mtime": snap.get("file_mtime") or 0,
            "note": snap.get("note") or "",
        }

    def update_snapshot(self, path: str, entry_id: str, snapshot: dict) -> bool:
        """原地更新一条已存在的快照（用于保存取舍进度）。"""
        if not os.path.exists(self.snap_path(path, entry_id)):
            return False
        snap = dict(snapshot)
        snap["id"] = entry_id
        _atomic_write_json(self.snap_path(path, entry_id), snap)
        m = self.meta(path)
        for i, e in enumerate(m.get("entries", [])):
            if e.get("id") == entry_id:
                m["entries"][i] = self._entry_from_snapshot(snap, entry_id)
                break
        _atomic_write_json(self.meta_path(path), m)
        self._sum_cache = None
        return True

    def set_note(self, path: str, entry_id: str, note: str) -> bool:
        """给某次审查加一句备注（"改完错别字后的版本"）。"""
        snap = self.read(path, entry_id)
        if not isinstance(snap, dict):
            return False
        snap["note"] = (note or "").strip()[:200]
        _atomic_write_json(self.snap_path(path, entry_id), snap)
        m = self.meta(path)
        for e in m.get("entries", []):
            if e.get("id") == entry_id:
                e["note"] = snap["note"]
        _atomic_write_json(self.meta_path(path), m)
        return True

    def compare(self, path: str, id_a: str, id_b: str) -> dict:
        """对比两次审查：哪些批注是新出现的、哪些消失了、哪些没变。

        用途：改完一轮错别字再跑一次，看看「这次 AI 又挑出了什么」，
        或者确认「上次的问题是不是都改掉了」。
        """
        a = self.read(path, id_a) or {}
        b = self.read(path, id_b) or {}
        if not a or not b:
            return {}

        def key(x):
            return (x.get("block_id"), x.get("sid"), x.get("type"),
                    (x.get("replacement") or "")[:120])

        sa = {}
        for x in (a.get("anns") or []):
            sa.setdefault(key(x), x)
        sb = {}
        for x in (b.get("anns") or []):
            sb.setdefault(key(x), x)

        added = [sb[k] for k in sb if k not in sa]
        removed = [sa[k] for k in sa if k not in sb]
        same = sum(1 for k in sa if k in sb)

        def brief(snap):
            ir = snap.get("image_report") or {}
            return {
                "id": snap.get("id") or "",
                "ts": snap.get("ts") or "",
                "model": snap.get("model") or "",
                "elapsed": snap.get("elapsed") or 0,
                "anns": len(snap.get("anns") or []),
                "images": ir.get("total") or 0,
                "summary": (snap.get("summary") or {}).get("summary", "")[:200],
                "note": snap.get("note") or "",
            }

        return {"a": brief(a), "b": brief(b), "added": added[:80],
                "removed": removed[:80], "same": same}

    def search(self, q: str, limit: int = 80) -> list:
        """跨文件搜索审查记录（文件名 / 时间 / 备注 / 模型）。"""
        q = (q or "").strip().lower()
        out = []
        try:
            names = os.listdir(self.root)
        except Exception:
            return []
        for fn in names:
            d = os.path.join(self.root, fn)
            if not os.path.isdir(d):
                continue
            m = _read_json(os.path.join(d, "meta.json")) or {}
            name = m.get("name") or fn
            path = m.get("path") or ""
            for e in m.get("entries") or []:
                if not isinstance(e, dict):
                    continue
                blob = " ".join(str(e.get(k) or "") for k in ("ts", "note", "model")).lower()
                blob += " " + str(name).lower()
                if q and q not in blob:
                    continue
                out.append({"file": path, "name": name, **e})
        out.sort(key=lambda x: x.get("epoch") or 0, reverse=True)
        return out[:limit]

    def prune_older_than(self, days: int) -> int:
        """删除超过 N 天的快照。返回删除条数。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            names = os.listdir(self.root)
        except Exception:
            return 0
        for fn in names:
            d = os.path.join(self.root, fn)
            if not os.path.isdir(d):
                continue
            mp = os.path.join(d, "meta.json")
            m = _read_json(mp)
            if not isinstance(m, dict):
                continue
            ents = m.get("entries") or []
            keep = []
            for e in ents:
                if (e.get("epoch") or 0) and (e.get("epoch") or 0) < cutoff:
                    try:
                        os.remove(os.path.join(d, f"{e['id']}.json"))
                    except Exception:
                        pass
                    removed += 1
                else:
                    keep.append(e)
            if len(keep) != len(ents):
                m["entries"] = keep
                _atomic_write_json(mp, m)
        if removed:
            self._sum_cache = None
        return removed

    def save(self, path: str, snapshot: dict) -> dict:
        """写入一次审查快照，并刷新索引、按 keep 修剪。"""
        path = os.path.abspath(path)
        d = self.dir_for(path)
        os.makedirs(d, exist_ok=True)

        now = time.time()
        entry_id = snapshot.get("id")
        if not entry_id:
            # 时间戳精确到秒，同一秒内重复审查会撞 id 把上一条覆盖掉，
            # 这里补一个序号后缀，确保每次都留下独立快照。
            base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
            entry_id = base
            n = 0
            while os.path.exists(self.snap_path(path, entry_id)):
                n += 1
                entry_id = f"{base}-{n:02d}"
        snapshot = dict(snapshot)
        snapshot.update({
            "schema": SCHEMA,
            "id": entry_id,
            "epoch": snapshot.get("epoch") or now,
            "ts": snapshot.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "file": path,
            "file_name": os.path.basename(path),
        })
        _atomic_write_json(self.snap_path(path, entry_id), snapshot)

        m = self.meta(path)
        m.update({"schema": SCHEMA, "path": path, "name": os.path.basename(path)})
        entry = self._entry_from_snapshot(snapshot, entry_id)
        ents = [e for e in m.get("entries", []) if e.get("id") != entry_id]
        ents.insert(0, entry)
        ents.sort(key=lambda e: e.get("epoch") or 0, reverse=True)
        # 修剪：超出 keep 的旧快照连文件一起删掉
        for old in ents[self.keep:]:
            try:
                os.remove(self.snap_path(path, old["id"]))
            except Exception:
                pass
        m["entries"] = ents[:self.keep]
        _atomic_write_json(os.path.join(d, "meta.json"), m)
        return entry

    def delete(self, path: str, entry_id: str) -> bool:
        m = self.meta(path)
        before = len(m.get("entries", []))
        m["entries"] = [e for e in m.get("entries", []) if e.get("id") != entry_id]
        try:
            os.remove(self.snap_path(path, entry_id))
        except Exception:
            pass
        _atomic_write_json(self.meta_path(path), m)
        return len(m["entries"]) < before

    def clear(self, path: str) -> int:
        m = self.meta(path)
        n = len(m.get("entries", []))
        d = self.dir_for(path)
        try:
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    os.remove(os.path.join(d, fn))
        except Exception:
            pass
        return n

    # -------------------------------------------------- 汇总
    def summary(self) -> dict:
        """历史总占用。

        目录里可能有上千个快照文件，boot() 每次都全量 stat 一遍会明显拖慢启动，
        所以按目录 mtime 做 60 秒缓存；有新写入时 mtime 会变，缓存自动失效。
        """
        import time as _t
        try:
            stamp = os.path.getmtime(self.root)
        except Exception:
            stamp = 0
        cache = getattr(self, "_sum_cache", None)
        now = _t.time()
        if cache and cache[0] == stamp and now - cache[1] < 60:
            return cache[2]

        files = 0
        entries = 0
        size = 0
        try:
            for fn in os.listdir(self.root):
                d = os.path.join(self.root, fn)
                if not os.path.isdir(d):
                    continue
                got = False
                for f2 in os.listdir(d):
                    if f2.endswith(".json"):
                        got = True
                        try:
                            size += os.path.getsize(os.path.join(d, f2))
                        except Exception:
                            pass
                        if f2 != "meta.json":
                            entries += 1
                files += 1 if got else 0
        except Exception:
            pass
        result = {"files": files, "entries": entries, "bytes": size, "root": self.root}
        self._sum_cache = (stamp, now, result)
        return result
