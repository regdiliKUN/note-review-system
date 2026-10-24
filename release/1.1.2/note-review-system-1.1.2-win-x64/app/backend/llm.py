"""LLM 客户端：默认走本机 Ollama，也可配置任意 OpenAI 兼容端点。"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

DEFAULT_OLLAMA = "http://127.0.0.1:11434"


class LLMError(Exception):
    pass


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

    # ------------------------------------------------------------ 视觉能力
    def vision_name(self) -> str:
        return self.vision_model or self.model

    def supports_vision(self) -> bool:
        """按模型名判断是否可能是多模态模型（避免对纯文本模型发图片导致报错）。"""
        m = (self.vision_name() or "").lower()
        keys = ("llava", "vision", "-vl", "vl:", "minicpm-v", "moondream",
                "bakllava", "gemma3", "qwen2.5vl", "qwen2-vl", "qwen-vl",
                "llama3.2-vision", "pixtral", "internvl", "cogvlm", "glm-4v",
                "gpt-4o", "gpt-4.1", "claude-3", "claude-4", "gemini")
        return any(k in m for k in keys)

    # ------------------------------------------------------------ 探测
    def list_models(self) -> list[str]:
        if self.provider == "ollama":
            try:
                with urllib.request.urlopen(self.base + "/api/tags", timeout=4) as r:
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
                 on_token=None) -> str:
        if self.provider == "ollama":
            return self._ollama(system, user, json_mode, on_token)
        return self._openai(system, user, json_mode, on_token)

    # ------------------------------------------------------------ 视觉生成
    def generate_vision(self, system: str, user: str, images: list,
                        json_mode: bool = True) -> str:
        """images: [{"b64": <base64>, "mime": <mime>}, ...]"""
        if self.provider == "ollama":
            return self._ollama_vision(system, user, images, json_mode)
        return self._openai_vision(system, user, images, json_mode)

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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8")).get("response", "")
        except urllib.error.URLError as e:
            raise LLMError(f"连接视觉模型失败：{e}") from e
        except Exception as e:
            raise LLMError(f"视觉模型返回异常：{e}") from e

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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
                return d["choices"][0]["message"]["content"]
        except urllib.error.URLError as e:
            raise LLMError(f"连接视觉模型失败：{e}") from e
        except Exception as e:
            raise LLMError(f"视觉模型返回异常：{e}") from e

    def _ollama(self, system, user, json_mode, on_token):
        payload = {
            "model": self.model,
            "prompt": user,
            "system": system,
            "stream": bool(on_token),
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": 4096,
            },
        }
        if json_mode:
            payload["format"] = "json"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
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

    def _openai(self, system, user, json_mode, on_token):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "stream": bool(on_token),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
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
def extract_json(text: str) -> dict:
    """模型常把 JSON 包在 ``` 里或前后带解释，这里做鲁棒抽取。"""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
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
                    return json.loads(t[start:i + 1])
                except Exception:
                    return {}
    return {}
