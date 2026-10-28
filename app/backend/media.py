"""多媒体笔记解析：语音 → 转写、视频 → 关键帧 + 转写、图片 → 视觉说明。

设计原则与 docmodel 一致：最终都落成「有序 block 列表」，之后再走同一条
审查 / 重排 / 导出链路，不需要为多媒体单独维护一套流程。

外部依赖都是可选的，缺了就给一条明确的说明，不重复刷屏：
  · ffmpeg / ffprobe —— 抽音轨、取视频关键帧（找不到时只提示，不报错）
  · 语音识别 —— 本机 faster-whisper，或任意 OpenAI 兼容 /audio/transcriptions 端点
  · 视觉模型 —— 复用主程序已配置的 Ollama / OpenAI 兼容多模态模型
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .docmodel import ImageStore, new_block

AUDIO_EXT = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma", "amr",
             "aiff", "aif", "caf", "silk"}
VIDEO_EXT = {"mp4", "mov", "m4v", "mkv", "avi", "webm", "flv", "wmv", "mpg",
             "mpeg", "ts", "3gp", "rmvb"}
IMAGE_EXT = {"png", "jpg", "jpeg", "bmp", "webp", "gif", "tif", "tiff"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT | IMAGE_EXT

KIND_CN = {"audio": "语音笔记", "video": "视频笔记", "image": "图片笔记"}


# ---------------------------------------------------------------- 外部工具
def _exe_dir(path: str) -> str:
    return path if os.path.isdir(path) else os.path.dirname(path)


def find_ffmpeg(cfg: Optional[dict] = None) -> Tuple[str, str]:
    """定位 ffmpeg / ffprobe，返回 (ffmpeg, ffprobe)，找不到返回 ("","")。"""
    cfg = cfg or {}
    cands: List[str] = []
    conf = str(cfg.get("ffmpeg_path") or "").strip().strip('"')
    if conf:
        d = _exe_dir(conf)
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        cands.append(os.path.join(d, name) if os.path.isdir(conf) else conf)
    for env in ("FFMPEG_BIN", "FFMPEG_PATH"):
        v = os.environ.get(env)
        if v:
            cands.append(v)
    which = shutil.which("ffmpeg")
    if which:
        cands.append(which)
    # 绿色版常见做法：把 ffmpeg 放在程序自己目录下（或 bin / tools 子目录）
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for sub in ("", "bin", "tools", os.path.join("tools", "ffmpeg"),
                    os.path.join("tools", "ffmpeg", "bin")):
            for exe in ("ffmpeg.exe", "ffmpeg"):
                cands.append(os.path.join(here, sub, exe))
                cands.append(os.path.join(os.path.dirname(here), sub, exe))
    except Exception:
        pass
    roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
             os.environ.get("USERPROFILE", ""), "C:\\"]
    for r in roots:
        if not r:
            continue
        cands.append(os.path.join(r, "ffmpeg", "bin", "ffmpeg.exe"))
        cands.append(os.path.join(r, "CodexTools", "ffmpeg", "bin", "ffmpeg.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            d = os.path.dirname(c)
            probe = os.path.join(d, "ffprobe.exe" if os.name == "nt" else "ffprobe")
            return c, (probe if os.path.isfile(probe) else "")
    return "", ""


def _run(cmd: List[str], timeout: float = 900) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
        return p.returncode, (p.stdout or b"").decode("utf-8", "ignore")
    except Exception as e:
        return -1, str(e)


def probe_media(path: str, cfg: Optional[dict] = None) -> Dict[str, Any]:
    """读取时长 / 分辨率等基本信息，失败时返回空字段（不抛异常）。"""
    info: Dict[str, Any] = {"duration": 0.0, "width": None, "height": None}
    ffmpeg, ffprobe = find_ffmpeg(cfg)
    if ffprobe:
        code, out = _run([ffprobe, "-v", "error", "-show_entries",
                          "format=duration:stream=width,height,codec_type",
                          "-of", "json", path], timeout=60)
        if code == 0:
            try:
                d = json.loads(out)
                info["duration"] = float((d.get("format") or {}).get("duration") or 0)
                for s in d.get("streams") or []:
                    if s.get("codec_type") == "video" and s.get("width"):
                        info["width"], info["height"] = s.get("width"), s.get("height")
                        break
            except Exception:
                pass
    if not info["duration"] and ffmpeg:
        # 只有 ffmpeg 没有 ffprobe 时，从 stderr 的 "Duration: 00:03:21.34" 兜底解析
        code, out = _run([ffmpeg, "-hide_banner", "-i", path], timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out or "")
        if m:
            info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                + float(m.group(3)))
        if not info.get("width"):
            m2 = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out or "")
            if m2:
                info["width"], info["height"] = int(m2.group(1)), int(m2.group(2))
    return info


# ---------------------------------------------------------------- 语音识别
def _has_local_whisper() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def asr_status(cfg: Optional[dict] = None) -> Dict[str, Any]:
    """当前可用的语音识别方案。"""
    cfg = cfg or {}
    base = str(cfg.get("asr_base") or "").strip()
    local = _has_local_whisper()
    if base:
        return {"ok": True, "engine": "http", "detail": f"OpenAI 兼容接口：{base}"}
    if local:
        model = str(cfg.get("asr_local_model") or "small")
        return {"ok": True, "engine": "faster-whisper", "detail": f"本机 faster-whisper（{model}）"}
    return {"ok": False, "engine": "",
            "detail": "未配置语音识别：可在设置里填一个 OpenAI 兼容的 /audio/transcriptions 地址，"
                      "或在本机安装 faster-whisper（pip install faster-whisper）"}


def _post_multipart(url: str, file_path: str, fields: Dict[str, str],
                    headers: Dict[str, str], timeout: float = 1800) -> dict:
    boundary = "----nr" + uuid.uuid4().hex
    buf = bytearray()
    for k, v in fields.items():
        buf += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    name = os.path.basename(file_path)
    buf += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{name}\"\r\nContent-Type: audio/wav\r\n\r\n").encode("utf-8")
    with open(file_path, "rb") as f:
        buf += f.read()
    buf += f"\r\n--{boundary}--\r\n".encode("utf-8")
    h = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    h.update({k: v for k, v in headers.items() if v})
    req = urllib.request.Request(url, data=bytes(buf), headers=h)
    from .llm import _urlopen          # 本机 ASR 服务同样要绕过系统代理
    with _urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except Exception:
        return {"text": raw.strip()}


def _asr_http(wav: str, cfg: dict) -> Tuple[List[Dict], str]:
    base = str(cfg.get("asr_base") or "").strip().rstrip("/")
    url = base + "/audio/transcriptions" if not base.endswith("/audio/transcriptions") else base
    fields = {"model": str(cfg.get("asr_model") or "whisper-1"), "language": "zh",
              "response_format": "verbose_json"}
    key = str(cfg.get("asr_key") or "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    segs: List[Dict] = []
    label = "接口转写"
    for part, offset in _split_wav(wav, cfg):
        d = _post_multipart(url, part, fields, headers)
        chunk = d.get("segments")
        if isinstance(chunk, list) and chunk:
            segs += [{"start": float(s.get("start") or 0) + offset,
                      "end": float(s.get("end") or 0) + offset,
                      "text": str(s.get("text") or "").strip()} for s in chunk]
            continue
        text = str(d.get("text") or "").strip()
        if not text:
            if len(segs):
                continue
            raise RuntimeError("接口未返回文本")
        for s in _split_text(text):
            s["start"] += offset
            s["end"] += offset
            segs.append(s)
    return segs, label


def _wav_duration(path: str) -> float:
    """用标准库读 wav 时长，不依赖 ffprobe。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 1
            return w.getnframes() / float(rate)
    except Exception:
        return 0.0


ASR_PART_SECONDS = 600          # 单次送转写的最长音频（10 分钟）
ASR_PART_BYTES = 20 * 1024 * 1024


def _split_wav(wav: str, cfg: Optional[dict] = None) -> List[Tuple[str, float]]:
    """把过长的音轨切成若干段，返回 [(文件, 起始秒)]。

    语音识别接口普遍有 25MB 上限，一段两小时的会议录音直接上传必定失败；
    本机 whisper 也倾向于在长音频上超时。切开之后逐段转写、再把时间戳偏移回去。
    """
    try:
        size = os.path.getsize(wav)
    except Exception:
        return [(wav, 0.0)]
    dur = _wav_duration(wav)
    if size <= ASR_PART_BYTES and dur <= ASR_PART_SECONDS:
        return [(wav, 0.0)]
    ffmpeg, _ = find_ffmpeg(cfg)
    if not ffmpeg or dur <= 0:
        return [(wav, 0.0)]
    out: List[Tuple[str, float]] = []
    d = os.path.dirname(wav)
    n = int(dur // ASR_PART_SECONDS) + 1
    for i in range(n):
        start = i * ASR_PART_SECONDS
        if start >= dur:
            break
        seg = os.path.join(d, f"part_{i:03d}.wav")
        code, _o = _run([ffmpeg, "-y", "-ss", str(start), "-t", str(ASR_PART_SECONDS),
                         "-i", wav, "-c", "copy", seg], timeout=600)
        if os.path.isfile(seg) and os.path.getsize(seg) > 1024:
            out.append((seg, float(start)))
    return out or [(wav, 0.0)]


_WHISPER_CACHE: Dict[str, Any] = {}


def _local_model(name: str):
    """加载（并缓存）本机 whisper 模型。

    模型加载要几秒到几十秒，旧版每次导入音频都重新加载一遍，
    连续处理几段语音时这部分开销比转写本身还大。
    """
    m = _WHISPER_CACHE.get(name)
    if m is None:
        from faster_whisper import WhisperModel  # 可选依赖
        m = WhisperModel(name, device="cpu", compute_type="int8")
        _WHISPER_CACHE[name] = m
    return m


def _asr_local(wav: str, cfg: dict, on_progress=None) -> Tuple[List[Dict], str]:
    model_name = str(cfg.get("asr_local_model") or "small")
    model = _local_model(model_name)
    total_dur = _wav_duration(wav) or 0
    segs = []
    for i, s in enumerate(model.transcribe(wav, language="zh", vad_filter=True,
                                           beam_size=1)[0]):
        segs.append({"start": float(s.start), "end": float(s.end),
                     "text": (s.text or "").strip()})
        if on_progress and i % 5 == 0:
            # 按「已转写到音频的第几秒」估算百分比，比「已出 37 句」直观得多
            pct = 0.3 + 0.4 * min(1.0, (float(s.end) / total_dur) if total_dur else 0)
            on_progress(f"正在转写语音… 已到 {_ts(float(s.end))}", pct)
    return segs, f"本机 faster-whisper（{model_name}）"


def _split_text(text: str, budget: int = 220) -> List[Dict]:
    """接口只给整段文本时，按句末标点切分成带相对时间的伪分段。"""
    import re
    parts = [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p.strip()]
    out, cur, t = [], [], 0.0
    for p in parts:
        cur.append(p)
        if sum(len(x) for x in cur) >= budget:
            seg = "".join(cur).strip()
            out.append({"start": t, "end": t, "text": seg})
            t += max(3.0, len(seg) / 5.0)
            cur = []
    if cur:
        out.append({"start": t, "end": t, "text": "".join(cur).strip()})
    return out


def _ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcribe(wav: str, cfg: dict, on_progress=None) -> Tuple[List[Dict], str, str]:
    """返回 (segments, engine_label, error)。error 非空表示失败原因。"""
    st = asr_status(cfg)
    if not st.get("ok"):
        return [], "", st.get("detail", "")
    try:
        if st.get("engine") == "http":
            segs, label = _asr_http(wav, cfg)
        else:
            segs, label = _asr_local(wav, cfg, on_progress)
    except Exception as e:
        return [], "", f"语音识别失败：{e}"
    segs = [s for s in segs if s.get("text")]
    if not segs:
        return [], label, "没有识别出可用的文字（可能是纯音乐或静音）"
    return segs, label, ""


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) \
        or (0x3000 <= o <= 0x303F) or (0xFF00 <= o <= 0xFFEF)


def _join_parts(parts: List[str]) -> str:
    """把转写片段拼成一段话。

    旧版一律用空格连接，于是中文笔记里出现「今天 我们 学习 了 ZBrush」，
    审查模型会把这种断字当成病句去改。中文之间直接连，英文之间才补空格。
    """
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if not out:
            out = p
        elif _is_cjk(out[-1]) or _is_cjk(p[0]):
            out += p
        else:
            out += " " + p
    return out


def _pack_segments(segs: List[Dict], budget: int = 320) -> List[Tuple[float, str]]:
    """把逐句分段合并成适合阅读/审查的段落，保留段首时间戳。"""
    out: List[Tuple[float, str]] = []
    cur: List[str] = []
    t0: Optional[float] = None
    for s in segs:
        if t0 is None:
            t0 = float(s.get("start") or 0)
        cur.append(str(s.get("text") or "").strip())
        if sum(len(x) for x in cur) >= budget:
            out.append((t0, _join_parts(cur)))
            cur, t0 = [], None
    if cur:
        out.append((t0 or 0.0, _join_parts(cur)))
    return out


# ---------------------------------------------------------------- 视觉说明
IMG_SYSTEM_MEDIA = """你是笔记整理助手。用户会给你一张图片（可能来自截图、视频关键帧或照片）。
请用一句话（不超过 40 字）说明它讲了什么，并判断类型。
只输出 JSON：{"kind":"截图/图表/流程图/公式/手写笔记/照片/界面/其他",
"caption":"一句话说明","text":"图中若有清晰可读的文字，摘出要点，没有就留空"}"""


def _describe_image(llm, b64: str, mime: str, hint: str = "") -> Tuple[str, str, str]:
    """返回 (kind, caption, ocr_text)；模型不可用时返回空串。"""
    from .llm import LLMError, extract_json
    try:
        raw = llm.generate_vision(IMG_SYSTEM_MEDIA,
                                  (hint + "\n请按格式输出 JSON。").strip(),
                                  [{"b64": b64, "mime": mime}], json_mode=True)
        d = extract_json(raw)
        return (str(d.get("kind") or "")[:12],
                str(d.get("caption") or "").strip()[:80],
                str(d.get("text") or "").strip()[:600])
    except LLMError:
        return "", "", ""
    except Exception:
        return "", "", ""


def _save_image(store: ImageStore, path: str, idx: int) -> Optional[Dict[str, Any]]:
    from PIL import Image  # 可选依赖
    try:
        with open(path, "rb") as f:
            blob = f.read()
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
        saved = store.save(blob, ext, idx)
        w = h = None
        try:
            import io
            with Image.open(io.BytesIO(blob)) as im:
                w, h = im.size
        except Exception:
            pass
        return {"t": "img", "file": saved["file"], "w": w, "h": h}
    except Exception:
        return None


# ---------------------------------------------------------------- 视频抽帧
def _extract_frames(video: str, outdir: str, count: int, duration: float,
                    cfg: Optional[dict] = None) -> Tuple[List[Tuple[str, float]], str]:
    """抽关键帧。优先按「画面变化」抽，抽不到再退回等间隔抽。

    等间隔抽帧的问题是：讲同一张 PPT 的十分钟里抽到的全是同一页，
    而真正换页的那一瞬间反而被跳过。先按场景变化抽更贴合「关键帧」的本意。
    """
    ffmpeg, _ = find_ffmpeg(cfg)
    if not ffmpeg:
        return [], "未找到 ffmpeg，无法抽取视频关键帧"
    if count <= 0:
        return [], ""
    scale = "scale='min(1280,iw)':-2"

    def _collect(pattern_name: str) -> List[Tuple[str, float]]:
        got = []
        for i in range(1, count + 1):
            p = os.path.join(outdir, pattern_name.format(i=i))
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                got.append(p)
        return got

    # ① 场景变化优先
    pattern = os.path.join(outdir, "scene_%03d.jpg")
    code, out = _run([ffmpeg, "-y", "-i", video, "-vf",
                      f"select='gt(scene,0.28)',{scale}",
                      "-vsync", "vfr", "-frames:v", str(count), "-q:v", "3", pattern],
                     timeout=1200)
    files = _collect("scene_{i:03d}.jpg")
    interval = max(1.0, (duration or 0) / max(1, count)) if duration else 10.0
    if files:
        step = (duration or 0) / max(1, len(files))
        return [(f, round(i * step, 2)) for i, f in enumerate(files)], ""

    # ② 等间隔兜底
    pattern2 = os.path.join(outdir, "frame_%03d.jpg")
    code2, out2 = _run([ffmpeg, "-y", "-i", video, "-vf",
                        f"fps=1/{interval:.3f},{scale}",
                        "-frames:v", str(count), "-q:v", "3", pattern2],
                       timeout=1200)
    files2 = _collect("frame_{i:03d}.jpg")
    if files2:
        return [(p, round(interval * i, 2)) for i, p in enumerate(files2)], ""

    tail = (out2 or out or "").strip().splitlines()
    reason = tail[-1][:160] if tail else "未知原因"
    if "Invalid data" in (out2 + out) or "moov atom not found" in (out2 + out):
        reason = "视频文件损坏或不完整（moov atom 缺失），请重新导出后再试"
    elif "No such file" in (out2 + out):
        reason = "找不到视频文件"
    return [], f"未能提取关键帧：{reason}"


def _extract_audio(media_path: str, outdir: str, cfg: Optional[dict] = None) -> Optional[str]:
    ffmpeg, _ = find_ffmpeg(cfg)
    if not ffmpeg:
        return None
    wav = os.path.join(outdir, "audio.wav")
    cmd = [ffmpeg, "-y", "-i", media_path, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", wav]
    code, out = _run(cmd, timeout=1800)
    if code != 0 or not os.path.isfile(wav) or os.path.getsize(wav) < 1024:
        return None
    return wav


# ---------------------------------------------------------------- 主入口
def parse_media(path: str, store: ImageStore, llm, cfg: Optional[dict] = None,
                on_progress=None) -> Dict[str, Any]:
    """把语音 / 视频 / 图片文件解析成与 docx 一致的文档结构。"""
    cfg = cfg or {}
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    title = os.path.splitext(os.path.basename(path))[0]
    if ext in AUDIO_EXT:
        kind = "audio"
    elif ext in VIDEO_EXT:
        kind = "video"
    else:
        kind = "image"

    def prog(msg, pct):
        if on_progress:
            on_progress(msg, pct)

    if kind == "image":
        return _parse_image(path, store, llm, title, on_progress=prog)
    info = probe_media(path, cfg)
    dur = float(info.get("duration") or 0)
    blocks: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"kind": kind, "duration": dur,
                            "width": info.get("width"), "height": info.get("height"),
                            "asr_ok": False, "asr_engine": "", "reason": "",
                            "frames": 0, "chars": 0, "segments": 0}

    blocks.append(new_block("heading", level=1, segs=[{"t": "text",
                                                       "v": f"{KIND_CN[kind]}：{title}"}]))
    blocks.append(new_block("para", segs=[{"t": "text", "v":
        (f"来源文件：{os.path.basename(path)}　"
         + (f"时长 {_ts(dur)}　" if dur else "")
         + (f"{info['width']}×{info['height']}　" if info.get("width") else "")
         + f"格式 {ext.upper() or '未知'}")}]))
    if dur > 2 * 3600:
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            f"提示：这段素材长达 {_ts(dur)}，转写与审查会花较长时间；"
            "如果只关心其中某一段，建议先剪出来再导入。"}]))

    tmp = tempfile.mkdtemp(prefix="nr_media_")
    try:
        # ---------- 语音转写（音频与视频共用）
        prog("正在准备音频…", 0.04)
        wav = _extract_audio(path, tmp, cfg) if find_ffmpeg(cfg)[0] else None
        segs: List[Dict] = []
        engine = ""
        err = ""
        if not find_ffmpeg(cfg)[0]:
            err = "未找到 ffmpeg，无法抽取音轨（可在设置里指定 ffmpeg 路径）"
        elif wav is None:
            err = "该文件没有可用音轨，或音轨提取失败"
        else:
            prog("正在转写语音…", 0.12)
            segs, engine, err = transcribe(wav, cfg, on_progress=prog)

        if segs:
            meta.update({"asr_ok": True, "asr_engine": engine,
                         "segments": len(segs),
                         "chars": sum(len(s["text"]) for s in segs)})
            if kind == "video":
                blocks.append(new_block("heading", level=2, segs=[{"t": "text", "v": "语音转写"}]))
            for t0, text in _pack_segments(segs):
                blocks.append(new_block("para", segs=[{"t": "text", "v": f"【{_ts(t0)}】{text}"}]))
        else:
            meta["reason"] = err
            if kind == "audio":
                blocks.append(new_block("para", segs=[{"t": "text", "v":
                    f"未能获得语音转写：{err}。这份语音里暂时没有可审查的文字，"
                    "配好语音识别后重新导入即可自动转成文字笔记。"}]))
            else:
                blocks.append(new_block("para", segs=[{"t": "text", "v":
                    f"未能获得语音转写：{err}。视频里的口述内容需人工补充，"
                    "或先在设置里配置语音识别后重新导入。"}]))

        # ---------- 视频关键帧
        if kind == "video":
            want = int(cfg.get("video_max_frames", 12) or 12)
            workdir = os.path.join(tmp, "frames")
            os.makedirs(workdir, exist_ok=True)
            prog("正在提取视频关键帧…", 0.5)
            frames, frame_err = _extract_frames(path, workdir, want, dur, cfg)
            if not frames:
                blocks.append(new_block("para", segs=[{"t": "text", "v":
                    frame_err or "未能提取关键帧（可能缺少 ffmpeg 或该编码不受支持）。"}]))
                meta["frame_error"] = frame_err
            else:
                blocks.append(new_block("heading", level=2, segs=[{"t": "text",
                                                                   "v": f"关键帧（{len(frames)} 张）"}]))
                vision_ok = bool(llm.supports_vision())
                for i, (fp, ts) in enumerate(frames):
                    prog(f"正在理解关键帧 {i + 1}/{len(frames)}…", 0.5 + 0.45 * (i + 1) / len(frames))
                    seg = _save_image(store, fp, 900 + i)
                    if not seg:
                        continue
                    caption = ""
                    if vision_ok:
                        from .pipeline import _vision_payload
                        with open(fp, "rb") as f:
                            b64, mime = _vision_payload(f.read(), "jpg")
                        fkind, caption, ocr = _describe_image(
                            llm, b64, mime, f"这是视频在 {_ts(ts)} 处的画面。")
                        if caption:
                            seg["pre_caption"] = caption
                            seg["pre_kind"] = fkind or "视频关键帧"
                    blocks.append(new_block("para", segs=[seg]))
                    note = f"〔{_ts(ts)}〕" + (caption or "（未生成画面说明）")
                    blocks.append(new_block("para", segs=[{"t": "text", "v": note}]))
                meta["frames"] = len(frames)
                if not vision_ok:
                    blocks.append(new_block("para", segs=[{"t": "text", "v":
                        "未启用视觉识别，以上关键帧只保留画面与时间点，内容需人工核对。"}]))
        return {"title": title, "blocks": blocks, "source_type": kind, "media": meta}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parse_image(path: str, store: ImageStore, llm, title: str,
                 on_progress=None) -> Dict[str, Any]:
    from .pipeline import _vision_payload
    if on_progress:
        on_progress("正在读取图片…", 0.2)
    seg = _save_image(store, path, 1)
    blocks: List[Dict[str, Any]] = [
        new_block("heading", level=1, segs=[{"t": "text", "v": f"图片笔记：{title}"}]),
    ]
    meta: Dict[str, Any] = {"kind": "image", "asr_ok": False, "vision_ok": False,
                            "reason": "", "chars": 0}
    if not seg:
        blocks.append(new_block("para", segs=[{"t": "text", "v": "图片读取失败，请换一张再试。"}]))
        meta["reason"] = "图片读取失败"
        return {"title": title, "blocks": blocks, "source_type": "image", "media": meta}

    blocks.append(new_block("para", segs=[seg]))
    vision_ok = bool(llm.supports_vision())
    if vision_ok:
        if on_progress:
            on_progress("正在识别图片内容…", 0.6)
        with open(path, "rb") as f:
            b64, mime = _vision_payload(f.read(), os.path.splitext(path)[1].lstrip("."))
        kind, caption, ocr = _describe_image(llm, b64, mime, f"文件：{os.path.basename(path)}")
        if caption:
            seg["pre_caption"] = caption
            seg["pre_kind"] = kind or "图片"
            blocks.append(new_block("para", segs=[{"t": "text", "v": f"图片说明：{caption}"
                                                                    + (f"［{kind}］" if kind else "")}]))
        if ocr:
            blocks.append(new_block("para", segs=[{"t": "text", "v": f"图中文字：{ocr}"}]))
            meta["chars"] = len(ocr)
        meta["vision_ok"] = bool(caption)
    else:
        vs = llm.vision_status()
        meta["reason"] = vs.get("reason") or "未启用视觉识别"
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            f"未启用视觉识别（{meta['reason']}），图片内容需人工补充说明。"}]))
    return {"title": title, "blocks": blocks, "source_type": "image", "media": meta}


def parse_many_images(paths: List[str], store: ImageStore, llm, title: str = "",
                      on_progress=None) -> Dict[str, Any]:
    """一次导入多张图片，合成一篇「图片笔记」。

    很多人把截图当笔记（界面步骤、板书、参考图）。旧版一次只能导一张，
    十张截图就要操作十次，而且彼此毫无关联。
    """
    from .pipeline import _vision_payload
    blocks: List[Dict[str, Any]] = [
        new_block("heading", level=1, segs=[{"t": "text",
                                            "v": f"图片笔记：{title or '未命名'}"}]),
    ]
    meta: Dict[str, Any] = {"kind": "image", "count": len(paths), "asr_ok": False,
                            "vision_ok": False, "reason": "", "chars": 0, "images": 0}
    vision_ok = bool(llm.supports_vision())
    if not vision_ok:
        meta["reason"] = (llm.vision_status() or {}).get("reason") or "未启用视觉识别"

    for i, p in enumerate(paths):
        if on_progress:
            on_progress(f"正在处理第 {i + 1}/{len(paths)} 张图片…",
                        0.05 + 0.9 * (i / max(1, len(paths))))
        seg = _save_image(store, p, i + 1)
        if not seg:
            continue
        meta["images"] += 1
        blocks.append(new_block("heading", level=2,
                                segs=[{"t": "text", "v": f"图 {i + 1}"}]))
        blocks.append(new_block("para", segs=[seg]))
        if not vision_ok:
            continue
        try:
            with open(p, "rb") as f:
                b64, mime = _vision_payload(f.read(),
                                            os.path.splitext(p)[1].lstrip("."))
            kind, caption, ocr = _describe_image(
                llm, b64, mime, f"这是第 {i + 1} 张图片，文件名 {os.path.basename(p)}")
        except Exception:
            continue
        if caption:
            seg["pre_caption"] = caption
            seg["pre_kind"] = kind or "图片"
            meta["vision_ok"] = True
            blocks.append(new_block("para", segs=[{"t": "text",
                                                  "v": f"说明：{caption}"
                                                       + (f"［{kind}］" if kind else "")}]))
        if ocr:
            blocks.append(new_block("para", segs=[{"t": "text", "v": f"图中文字：{ocr}"}]))
            meta["chars"] += len(ocr)

    if not vision_ok:
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            f"未启用视觉识别（{meta['reason']}），以上图片只保留画面，"
            "内容需人工补充说明。"}]))
    if meta["images"] == 0:
        meta["reason"] = "所有图片都读取失败"
    return {"title": title or "图片笔记", "blocks": blocks,
            "source_type": "image", "media": meta}


def capabilities(cfg: Optional[dict] = None, llm=None) -> Dict[str, Any]:
    """给设置页用：多媒体链路各环节是否就绪。"""
    cfg = cfg or {}
    ffmpeg, ffprobe = find_ffmpeg(cfg)
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "asr": asr_status(cfg),
        "vision": (llm.vision_status() if llm else {}),
        "video_frames": int(cfg.get("video_max_frames", 12) or 12),
        "config": {
            "asr_base": cfg.get("asr_base", ""),
            "asr_model": cfg.get("asr_model", ""),
            "asr_local_model": cfg.get("asr_local_model", ""),
            "has_asr_key": bool(cfg.get("asr_key")),
            "ffmpeg_path": cfg.get("ffmpeg_path", ""),
            "video_max_frames": int(cfg.get("video_max_frames", 12) or 12),
        },
        "input": {
            "audio": sorted(AUDIO_EXT), "video": sorted(VIDEO_EXT),
            "image": sorted(IMAGE_EXT),
        },
    }
