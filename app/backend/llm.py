"""LLM 客户端：默认走本机 Ollama，也可配置任意 OpenAI 兼容端点。"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# 本机地址一律绕过系统代理：机器上装了全局代理时，HTTP_PROXY 会把
# 127.0.0.1:11434 的请求也转发出去，代理一挂本地模型就整片失败。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(req, timeout: float):
    url = getattr(req, "full_url", "") or str(req)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


# 按名字猜多模态：只在拿不到真实能力信息时才用，仅作兜底
VISION_NAME_KEYS = ("llava", "vision", "-vl", "vl:", "vl-", "minicpm-v", "moondream",
                    "bakllava", "gemma3", "qwen2.5vl", "qwen2-vl", "qwen-vl",
                    "llama3.2-vision", "pixtral", "internvl", "cogvlm", "glm-4v",
                    "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "claude-4", "gemini",
                    "internvl2", "ovis", "step-1v")

# 模型自带视觉能力的提示（Ollama /api/show 没有 capabilities 字段时看架构族）
VISION_FAMILIES = ("clip", "mllama", "qwen25vl", "qwen2vl", "llava", "minicpmv",
                   "gemma3", "pixtral", "internvl", "mllava")

_CAPS_CACHE: dict = {}
_CAPS_TTL = 300.0

# 这些错误是「临时性」的：重试一次往往就好了，不该直接判整篇审查失败。
_TRANSIENT = ("timed out", "timeout", "connection reset", "connection aborted",
              "remote end closed", "temporarily unavailable", "connection refused",
              "broken pipe", "eof occurred", " 502", " 503", " 504",
              "bad gateway", "service unavailable", "gateway timeout")


def _transient(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in _TRANSIENT)


class LLMError(Exception):
    pass


class VisionUnsupported(LLMError):
    """模型服务明确表示不接受图片输入。"""


def ollama_caps(base: str, model: str, timeout: float = 6.0) -> dict:
    """向 Ollama 询问模型的真实能力（capabilities）。

    返回 {"known": bool, "capabilities": [...], "error": str}。
    known=False 表示这个版本来没提供能力信息，调用方应退回按模型名推断。
    """
    key = (base or "", model or "")
    now = time.time()
    hit = _CAPS_CACHE.get(key)
    if hit and now - hit[0] < _CAPS_TTL:
        return hit[1]
    data = {"known": False, "capabilities": [], "error": ""}
    try:
        req = urllib.request.Request(
            (base or DEFAULT_OLLAMA).rstrip("/") + "/api/show",
            data=json.dumps({"model": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with _urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        caps = d.get("capabilities")
        if isinstance(caps, list) and caps:
            data = {"known": True, "capabilities": [str(c) for c in caps], "error": ""}
        else:
            fams = [str(x).lower() for x in ((d.get("details") or {}).get("families") or [])]
            proj = json.dumps(d.get("projector_info") or {}).lower()
            if any(any(f in fam for f in VISION_FAMILIES) for fam in fams) or "clip" in proj:
                data = {"known": True, "capabilities": ["completion", "vision"], "error": ""}
    except Exception as e:
        data["error"] = str(e)
    _CAPS_CACHE[key] = (now, data)
    return data


def _looks_multimodal(name: str) -> bool:
    m = (name or "").lower()
    return any(k in m for k in VISION_NAME_KEYS)


class LLM:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.provider = cfg.get("provider", "ollama")   # ollama | openai
        self.base = (cfg.get("base") or DEFAULT_OLLAMA).rstrip("/")
        self.model = cfg.get("model") or "qwen3:8b"
        self.vision_model = cfg.get("vision_model") or ""   # 留空则用 model
        self.api_key = cfg.get("api_key") or ""
        self.temperature = float(cfg.get("temperature", 0.2))
        self.num_ctx = int(cfg.get("num_ctx", 8192))
        self.timeout = int(cfg.get("timeout", 600))
        # 本机模型在长文档里偶尔会自己重启（Ollama 换模型、显存回收），
        # 一次连接失败不代表整篇失败 —— 默认重试 2 次，指数退避。
        self.retries = max(0, min(int(cfg.get("retries", 2)), 5))
        # 运行期被模型服务明确拒绝过图片输入 → 本次会话不再尝试，避免几百张图反复报错
        self._vision_blocked = False
        self._vision_errors = 0

    # ------------------------------------------------------------ 视觉能力
    def vision_name(self) -> str:
        return self.vision_model or self.model

    def vision_status(self) -> dict:
        """真实能力判定：优先问 Ollama，其次看模型名，最后才假设可用。

        返回 {"enabled","model","reason","detected_by","candidates"}。
        """
        name = self.vision_name()
        out = {"enabled": False, "model": name, "reason": "",
               "detected_by": "", "candidates": []}
        if self._vision_blocked:
            out["reason"] = "模型服务已拒绝接收图片输入"
            out["detected_by"] = "runtime"
            return out
        if self.provider == "ollama":
            out["candidates"] = self.vision_candidates()
            caps = ollama_caps(self.base, name)
            if caps.get("known"):
                out["detected_by"] = "ollama"
                out["enabled"] = "vision" in caps["capabilities"]
                if not out["enabled"]:
                    out["reason"] = (f"当前视觉模型 {name} 不具备图像理解能力，"
                                     "Ollama 报告其能力为 " + "/".join(caps["capabilities"]))
            else:
                out["detected_by"] = "name"
                out["enabled"] = _looks_multimodal(name)
                if not out["enabled"]:
                    out["reason"] = f"模型 {name} 看起来不是多模态模型"
            if not self.vision_model and out["candidates"]:
                # 没显式配置，但本机装了视觉模型 → 直接用，提示用户已自动启用
                out["auto"] = out["candidates"][0]
            return out
        # OpenAI 兼容端点：拿不到能力清单，先放行按多模态试一次；
        # 真发不过去会由运行期熔断降级，不会把整篇文档卡死。
        out["detected_by"] = "assume"
        out["enabled"] = True
        if not _looks_multimodal(name):
            out["reason"] = f"未确认 {name} 是否支持图像，将直接尝试（失败会自动停止识别）"
        return out

    def supports_vision(self) -> bool:
        return bool(self.vision_status().get("enabled"))

    def vision_candidates(self) -> list:
        """本机已安装且确实支持视觉的模型，按推荐度排序。"""
        if self.provider != "ollama":
            return []
        out = []
        for m in self.list_models():
            caps = ollama_caps(self.base, m)
            if caps.get("known") and "vision" in caps["capabilities"]:
                out.append(m)
            elif not caps.get("known") and _looks_multimodal(m):
                out.append(m)
        pref = ("qwen2.5vl", "qwen3-vl", "llava", "minicpm-v", "gemma3", "moondream")
        out.sort(key=lambda m: next((i for i, p in enumerate(pref) if m.startswith(p)), len(pref)))
        return out

    def pick_vision_model(self) -> str:
        cands = self.vision_candidates()
        return cands[0] if cands else ""

    def mark_vision_blocked(self):
        self._vision_blocked = True

    def _note_vision_error(self, msg: str):
        """把「模型不支持图片」这类硬错误识别出来，触发熔断。"""
        low = (msg or "").lower()
        hard = ("does not support image", "does not support vision", "no vision",
                "image input is not supported", "unsupported image", "not a multimodal",
                "unknown model", "model not found", "not found, try pulling")
        if any(h in low for h in hard):
            self.mark_vision_blocked()

    # ------------------------------------------------------------ 探测
    def list_models(self) -> list[str]:
        if self.provider == "ollama":
            try:
                with _urlopen(self.base + "/api/tags", timeout=4) as r:
                    d = json.loads(r.read().decode("utf-8"))
                return [m["name"] for m in d.get("models", [])]
            except Exception:
                return []
        return []

    def health(self) -> dict:
        if self.provider == "ollama":
            models = self.list_models()
            return {"ok": bool(models), "models": models, "provider": "ollama",
                    "model": self.model,
                    "msg": "" if models else "无法连接 Ollama，请确认它正在运行"}
        return {"ok": bool(self.base), "models": [self.model], "provider": "openai",
                "model": self.model, "msg": ""}

    # ------------------------------------------------------------ 生成
    def generate(self, system: str, user: str, json_mode: bool = True,
                 on_token=None, max_tokens: int = 0) -> str:
        fn = self._ollama if self.provider == "ollama" else self._openai
        return self._with_retry(
            lambda: fn(system, user, json_mode, on_token, max_tokens))

    def _with_retry(self, call):
        """对临时性网络故障重试；确定性错误（模型不存在、参数错）立即抛出。"""
        last = None
        for attempt in range(self.retries + 1):
            try:
                return call()
            except VisionUnsupported:
                raise
            except LLMError as e:
                last = e
                if attempt >= self.retries or not _transient(str(e)):
                    raise
                time.sleep(min(8.0, 1.0 * (2 ** attempt)))
        raise last if last else LLMError("模型调用失败")

    # ------------------------------------------------------------ 视觉生成
    def generate_vision(self, system: str, user: str, images: list,
                        json_mode: bool = True) -> str:
        """images: [{"b64": <base64>, "mime": <mime>}, ...]"""
        if self._vision_blocked:
            raise VisionUnsupported("本次会话已停用视觉识别")
        fn = self._ollama_vision if self.provider == "ollama" else self._openai_vision
        return self._with_retry(lambda: fn(system, user, images, json_mode))

    @staticmethod
    def _http_error_text(e: Exception) -> str:
        """urllib 的 HTTPError 带着服务端错误体，里面往往写着真正原因。"""
        try:
            body = e.read().decode("utf-8", "ignore")  # type: ignore[attr-defined]
            d = json.loads(body)
            if isinstance(d, dict):
                return str(d.get("error") or d.get("message") or body)
            return body
        except Exception:
            return str(e)

    def _raise_vision(self, e: Exception, prefix: str):
        msg = self._http_error_text(e) if getattr(e, "read", None) else str(e)
        self._vision_errors += 1
        self._note_vision_error(msg)
        if self._vision_blocked:
            raise VisionUnsupported(f"{prefix}：{msg}") from e
        raise LLMError(f"{prefix}：{msg}") from e

    def _ollama_vision(self, system, user, images, json_mode):
        payload = {
            "model": self.vision_name(),
            "prompt": user,
            "system": system,
            "images": [im.get("b64", "") for im in images if im.get("b64")],
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": 1024,
            },
        }
        if json_mode:
            payload["format"] = "json"
        req = urllib.request.Request(
            self.base + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with _urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8")).get("response", "")
        except urllib.error.URLError as e:
            self._raise_vision(e, "连接视觉模型失败")
        except LLMError:
            raise
        except Exception as e:
            self._raise_vision(e, "视觉模型返回异常")

    def _openai_vision(self, system, user, images, json_mode):
        content = [{"type": "text", "text": user}]
        for im in images:
            b64 = im.get("b64")
            if not b64:
                continue
            mime = im.get("mime") or "image/png"
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
        body = {
            "model": self.vision_name(),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with _urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
                return d["choices"][0]["message"]["content"]
        except urllib.error.URLError as e:
            self._raise_vision(e, "连接视觉模型失败")
        except LLMError:
            raise
        except Exception as e:
            self._raise_vision(e, "视觉模型返回异常")

    def _ollama(self, system, user, json_mode, on_token, max_tokens=0):
        payload = {
            "model": self.model,
            "prompt": user,
            "system": system,
            "stream": bool(on_token),
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": int(max_tokens) if max_tokens else 4096,
            },
        }
        if json_mode:
            payload["format"] = "json"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with _urlopen(req, timeout=self.timeout) as r:
                if not on_token:
                    return json.loads(r.read().decode("utf-8")).get("response", "")
                buf = []
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    piece = obj.get("response", "")
                    if piece:
                        buf.append(piece)
                        on_token(piece)
                    if obj.get("done"):
                        break
                return "".join(buf)
        except urllib.error.URLError as e:
            raise LLMError(f"连接模型服务失败：{e}") from e

    def _openai(self, system, user, json_mode, on_token, max_tokens=0):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "stream": bool(on_token),
        }
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with _urlopen(req, timeout=self.timeout) as r:
                if not on_token:
                    d = json.loads(r.read().decode("utf-8"))
                    return d["choices"][0]["message"]["content"]
                buf = []
                for raw in r:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        piece = obj["choices"][0]["delta"].get("content") or ""
                    except Exception:
                        continue
                    if piece:
                        buf.append(piece)
                        on_token(piece)
                return "".join(buf)
        except urllib.error.URLError as e:
            raise LLMError(f"连接模型服务失败：{e}") from e


# ---------------------------------------------------------------- JSON 抽取
def _close_stack(s: str) -> str:
    """把被截断的 JSON 片段补成合法结构：闭合字符串、补上缺的 } / ]。

    小模型（尤其 4B 以下）在 num_predict 用尽时会硬生生截断，
    最后一条批注往往只写了一半。与其丢掉整段结果，不如把它修回来。
    """
    stack = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s.rstrip())
    # 悬空的 "key": 也要去掉，否则补完仍是非法 JSON
    s = re.sub(r',\s*"[^"]*"\s*:\s*$', "", s)
    s = re.sub(r'{\s*"[^"]*"\s*:\s*$', "{", s)
    s += "".join(reversed(stack))
    # 闭合前多余的逗号：{"a":1,} / ["a",]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _repair_truncated(t: str):
    """在截断的 JSON 上找最后一个「安全切点」，截断后补全结构再解析。"""
    cuts = [i for i, ch in enumerate(t) if ch in "}]"]
    for cut in reversed(cuts[-60:]):
        cand = _close_stack(t[:cut + 1])
        try:
            v = json.loads(cand)
            if isinstance(v, (dict, list)):
                return v
        except Exception:
            continue
    # 一个闭合符号都没有（截断在字符串或数字中间）→ 直接补全试试
    try:
        v = json.loads(_close_stack(t))
        if isinstance(v, (dict, list)):
            return v
    except Exception:
        pass
    return None


def extract_json(text: str) -> dict:
    """模型常把 JSON 包在 ``` 里或前后带解释，这里做鲁棒抽取。"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {}
    except Exception:
        pass
    # 找第一个平衡的 { ... }
    start = t.find("{")
    if start < 0:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(t[start:i + 1])
                    return v if isinstance(v, dict) else {}
                except Exception:
                    break
    # 走到这里说明结构不完整（被截断）→ 尝试修复
    v = _repair_truncated(t[start:])
    return v if isinstance(v, dict) else {}
