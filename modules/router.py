from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


class RouterError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str
    capabilities: set[str]
    rpm: int = 20
    rpd: int = 1000
    tpm: int = 6000
    priority: int = 50
    enabled: bool = True
    auth_type: str = "bearer"
    # Тип API: openai-совместимый, gemini-native, anthropic, yandex, gigachat
    api_format: str = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Доп. параметры (folder_id для Yandex, scope для GigaChat, anthropic_version и т.п.)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.enabled


def estimate_tokens(text: str) -> int:
    """Эвристика: 1 токен ≈ 2 символа для кириллицы, 3 для латиницы.
    Точнее, чем `len(text)//3`, для русских промптов.
    """
    if not text:
        return 0
    cyrillic = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    other = len(text) - cyrillic
    return max(1, cyrillic // 2 + other // 3)


class MultiProviderRouter:
    """OpenAI-compatible (+adapters) multi-provider router with soft limits and JSON parsing.

    It does not bypass provider limits. It only rotates between configured providers
    when a provider is not configured, has hit its in-process limit, or returns an error.

    Supports:
    - openai-compatible (OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Together, xAI, Gemini-OpenAI)
    - anthropic (Claude messages API)
    - gemini-native (Google AI Studio v1beta)
    - yandex (yandex foundation models / yandexgpt)
    - gigachat (Sberbank GigaChat) with optional auto-refresh
    """

    def __init__(self, config_path: str | Path = "configs/config_default.yaml", cache_ttl_sec: int = 60 * 60 * 24 * 30):
        self.config_path = Path(config_path)
        self.cache_ttl_sec = cache_ttl_sec
        self.providers = self._load_config(self.config_path)
        self.usage: dict[str, dict[str, Any]] = {
            p.name: {"day": 0, "minute": [], "tokens_min": 0, "day_started": self._day_key()} for p in self.providers
        }
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        # GigaChat token state (auto-refresh)
        self._gigachat_token: dict[str, Any] = {"token": "", "expires_at": 0.0}

    def _day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _reset_if_new_day(self, provider_name: str) -> None:
        today = self._day_key()
        u = self.usage[provider_name]
        if u.get("day_started") != today:
            u.update({"day": 0, "minute": [], "tokens_min": 0, "day_started": today})

    def _load_config(self, config_path: Path) -> list[ProviderConfig]:
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        providers = []
        for item in data.get("providers", []):
            providers.append(
                ProviderConfig(
                    name=item["name"],
                    base_url=item["base_url"].rstrip("/"),
                    model=item["model"],
                    api_key_env=item["api_key_env"],
                    capabilities=set(item.get("capabilities", [])),
                    rpm=int(item.get("rpm", 20)),
                    rpd=int(item.get("rpd", 1000)),
                    tpm=int(item.get("tpm", 6000)),
                    priority=int(item.get("priority", 50)),
                    enabled=bool(item.get("enabled", True)),
                    auth_type=item.get("auth_type", "bearer"),
                    api_format=item.get("api_format", "openai"),
                    extra_headers=dict(item.get("extra_headers", {})),
                    extras=dict(item.get("extras", {})),
                )
            )
        return providers

    def provider_status(self) -> list[dict[str, Any]]:
        rows = []
        for p in self.providers:
            self._reset_if_new_day(p.name)
            u = self.usage[p.name]
            rows.append(
                {
                    "name": p.name,
                    "model": p.model,
                    "format": p.api_format,
                    "configured": p.is_configured,
                    "capabilities": sorted(p.capabilities),
                    "rpm": p.rpm,
                    "rpd": p.rpd,
                    "used_today": u["day"],
                    "priority": p.priority,
                    "enabled": p.enabled,
                }
            )
        return rows

    async def pick(self, capability: str, est_tokens: int = 500) -> ProviderConfig | None:
        async with self._lock:
            now = time.time()
            candidates: list[tuple[int, float, ProviderConfig]] = []
            for p in self.providers:
                self._reset_if_new_day(p.name)
                if not p.is_configured:
                    continue
                if capability not in p.capabilities:
                    continue
                u = self.usage[p.name]
                u["minute"] = [t for t in u["minute"] if now - t < 60]
                # Сбрасываем TPM-окно грубо раз в минуту вместе с minute.
                if not u["minute"]:
                    u["tokens_min"] = 0
                if u["day"] >= p.rpd:
                    continue
                if len(u["minute"]) >= p.rpm:
                    continue
                if u["tokens_min"] + est_tokens > p.tpm:
                    continue
                remaining_ratio = (p.rpd - u["day"]) / max(p.rpd, 1)
                candidates.append((p.priority, remaining_ratio, p))
            if not candidates:
                return None
            # Явная сортировка: priority DESC, remaining_ratio DESC.
            candidates.sort(key=lambda x: (-x[0], -x[1]))
            chosen = candidates[0][2]
            u = self.usage[chosen.name]
            u["minute"].append(now)
            u["day"] += 1
            u["tokens_min"] += est_tokens
            return chosen

    async def call_json(
        self,
        capability: str,
        prompt: str,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int = 900,
        allow_cache: bool = True,
    ) -> dict[str, Any]:
        cache_key = hashlib.sha256(f"{capability}:{system}:{prompt}".encode("utf-8")).hexdigest()
        now = time.time()
        if allow_cache and cache_key in self.cache:
            ts, result = self.cache[cache_key]
            if now - ts < self.cache_ttl_sec:
                return {"cached": True, **result}

        # Более точная оценка токенов с учётом кириллицы.
        est = max(200, estimate_tokens(prompt) + estimate_tokens(system) + max_tokens // 2)
        last_error = None
        for attempt in range(6):
            provider = await self.pick(capability, est_tokens=est)
            if provider is None:
                await asyncio.sleep(min(2**attempt, 15))
                continue
            try:
                result = await self._http_call(provider, prompt, system, temperature, max_tokens)
                parsed = self._validate_with_schema(result)
                # SEC-4: не кэшируем «отрицательные» ответы и явные ошибки.
                if allow_cache and not parsed.get("error") and parsed.get("is_company_contact") is not False:
                    self.cache[cache_key] = (now, parsed)
                return parsed
            except Exception as exc:  # fallback to next provider
                last_error = exc
                continue
        return {"error": "all_providers_failed", "details": str(last_error) if last_error else "no provider available"}

    # ──────────────────────────────────────────────────────────────────
    # GigaChat: OAuth token exchange + автообновление.
    # ──────────────────────────────────────────────────────────────────
    async def _gigachat_get_access_token(self, provider: ProviderConfig) -> str:
        """Возвращает действующий access_token. При истечении — обновляет.

        Использует GIGACHAT_AUTH_KEY (base64 от client_id:client_secret).
        Если задан GIGACHAT_ACCESS_TOKEN — используем его как fallback (без refresh).
        """
        now = time.time()
        # Если уже есть live-токен, отданный нам через .env, и refresh-ключа нет — используем его.
        env_token = os.getenv("GIGACHAT_ACCESS_TOKEN", "").strip()
        auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()

        # Запас 60 секунд до истечения.
        if self._gigachat_token["token"] and self._gigachat_token["expires_at"] - now > 60:
            return self._gigachat_token["token"]

        if not auth_key:
            if env_token:
                return env_token
            raise RouterError("GIGACHAT_AUTH_KEY is not set; cannot refresh GigaChat token")

        scope = os.getenv("GIGACHAT_SCOPE", provider.extras.get("scope", "GIGACHAT_API_PERS"))
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Authorization": f"Basic {auth_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        verify_ssl = not bool(int(os.getenv("GIGACHAT_INSECURE", "0") or "0"))
        async with httpx.AsyncClient(timeout=20, verify=verify_ssl) as client:
            resp = await client.post(url, headers=headers, data={"scope": scope})
        if resp.status_code >= 400:
            raise RouterError(f"GigaChat OAuth failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        token = data.get("access_token") or ""
        expires_at = data.get("expires_at")  # ms timestamp
        if not token:
            raise RouterError(f"GigaChat OAuth empty token: {data}")
        if expires_at:
            # API отдаёт ms, конвертируем в секунды.
            self._gigachat_token["expires_at"] = float(expires_at) / 1000.0
        else:
            # По спецификации токен живёт 30 минут.
            self._gigachat_token["expires_at"] = now + 25 * 60
        self._gigachat_token["token"] = token
        return token

    # ──────────────────────────────────────────────────────────────────
    # Универсальный HTTP-вызов с роутингом по api_format.
    # ──────────────────────────────────────────────────────────────────
    async def _http_call(
        self,
        provider: ProviderConfig,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        fmt = provider.api_format
        if fmt == "openai":
            return await self._call_openai_compatible(provider, prompt, system, temperature, max_tokens)
        if fmt == "anthropic":
            return await self._call_anthropic(provider, prompt, system, temperature, max_tokens)
        if fmt == "gemini-native":
            return await self._call_gemini_native(provider, prompt, system, temperature, max_tokens)
        if fmt == "yandex":
            return await self._call_yandex(provider, prompt, system, temperature, max_tokens)
        if fmt == "gigachat":
            return await self._call_gigachat(provider, prompt, system, temperature, max_tokens)
        raise RouterError(f"Unknown api_format: {fmt}")

    async def _call_openai_compatible(self, p, prompt, system, temperature, max_tokens) -> str:
        headers = {"Content-Type": "application/json", **p.extra_headers}
        if p.auth_type == "x-goog-api-key":
            headers["x-goog-api-key"] = p.api_key
        elif p.auth_type == "api-key":
            headers["api-key"] = p.api_key
        else:
            headers["Authorization"] = f"Bearer {p.api_key}"
        # OpenRouter attribution
        if p.base_url.startswith("https://openrouter.ai"):
            if os.getenv("OPENROUTER_SITE_URL"):
                headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL", "")
            if os.getenv("OPENROUTER_SITE_NAME"):
                headers["X-Title"] = os.getenv("OPENROUTER_SITE_NAME", "")
        payload = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system or "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Многие OpenAI-совместимые модели поддерживают response_format=json_object.
        if p.extras.get("supports_json_mode"):
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{p.base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code in {401, 403, 429}:
            raise RouterError(f"{p.name} returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RouterError(f"Empty response from {p.name}")
        return content

    async def _call_anthropic(self, p, prompt, system, temperature, max_tokens) -> str:
        version = p.extras.get("anthropic_version", "2023-06-01")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": p.api_key,
            "anthropic-version": version,
            **p.extra_headers,
        }
        payload = {
            "model": p.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system or "Return valid JSON only.",
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{p.base_url}/messages", headers=headers, json=payload)
        if resp.status_code in {401, 403, 429}:
            raise RouterError(f"{p.name} returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        # Anthropic: { content: [ { type: 'text', text: '...' } ] }
        chunks = data.get("content", [])
        content = "".join(c.get("text", "") for c in chunks if c.get("type") == "text")
        if not content:
            raise RouterError(f"Empty response from {p.name}: {data}")
        return content

    async def _call_gemini_native(self, p, prompt, system, temperature, max_tokens) -> str:
        # Native v1beta: POST /models/{model}:generateContent?key=API_KEY
        url = f"{p.base_url}/models/{p.model}:generateContent"
        params = {"key": p.api_key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, params=params, json=payload)
        if resp.status_code in {401, 403, 429}:
            raise RouterError(f"{p.name} returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RouterError(f"No candidates from {p.name}: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        content = "".join(part.get("text", "") for part in parts)
        if not content:
            raise RouterError(f"Empty response from {p.name}")
        return content

    async def _call_yandex(self, p, prompt, system, temperature, max_tokens) -> str:
        # YandexGPT: https://llm.api.cloud.yandex.net/foundationModels/v1/completion
        # model uri: gpt://<folder_id>/<model_name>/latest
        folder_id = os.getenv("YANDEX_FOLDER_ID", p.extras.get("folder_id", "")).strip()
        if not folder_id:
            raise RouterError("YANDEX_FOLDER_ID is required for YandexGPT")
        model_uri = f"gpt://{folder_id}/{p.model}/latest"
        url = f"{p.base_url.rstrip('/')}/foundationModels/v1/completion"
        if not p.base_url.endswith("net"):  # допускаем оба варианта
            url = f"{p.base_url}/foundationModels/v1/completion"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {p.api_key}",
            "x-folder-id": folder_id,
        }
        messages = []
        if system:
            messages.append({"role": "system", "text": system})
        messages.append({"role": "user", "text": prompt})
        payload = {
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": messages,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in {401, 403, 429}:
            raise RouterError(f"{p.name} returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        alts = data.get("result", {}).get("alternatives", [])
        if not alts:
            raise RouterError(f"No alternatives from {p.name}: {data}")
        content = alts[0].get("message", {}).get("text", "")
        if not content:
            raise RouterError(f"Empty response from {p.name}")
        return content

    async def _call_gigachat(self, p, prompt, system, temperature, max_tokens) -> str:
        # GigaChat: native chat/completions с OAuth Bearer.
        token = await self._gigachat_get_access_token(p)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **p.extra_headers,
        }
        verify_ssl = not bool(int(os.getenv("GIGACHAT_INSECURE", "0") or "0"))
        # Temperature в GigaChat — [0, 2], нормализуем.
        gc_temp = max(0.0, min(2.0, temperature * 2 if temperature <= 1.0 else temperature))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": p.model,
            "messages": messages,
            "temperature": gc_temp,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=60, verify=verify_ssl) as client:
            resp = await client.post(f"{p.base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code == 401:
            # Просим обновить токен и пробуем ещё раз.
            self._gigachat_token = {"token": "", "expires_at": 0.0}
            token = await self._gigachat_get_access_token(p)
            headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=60, verify=verify_ssl) as client:
                resp = await client.post(f"{p.base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code in {401, 403, 429}:
            raise RouterError(f"{p.name} returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise RouterError(f"Empty response from {p.name}")
        return content

    def _validate_with_schema(self, content: str | dict[str, Any]) -> dict[str, Any]:
        """Parse JSON from raw model text, markdown fenced JSON or already-decoded dict.

        Стратегия:
        1) Если content уже dict — возвращаем.
        2) Снимаем markdown-обёртку ```...```.
        3) Используем json.JSONDecoder.raw_decode — он съедает первый валидный
           JSON-объект из строки, игнорируя trailing-мусор.
        4) Fallback: ищем пары '{' / '}' с балансом скобок.
        """
        if isinstance(content, dict):
            return content
        text = str(content).strip()
        # ```json ... ``` or ``` ... ```
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
        if fence:
            text = fence.group(1).strip()
        # Some models prepend prose. Extract first JSON object/array.
        if not (text.startswith("{") or text.startswith("[")):
            obj_start = text.find("{")
            arr_start = text.find("[")
            starts = [x for x in [obj_start, arr_start] if x != -1]
            if starts:
                text = text[min(starts):]

        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text)
            return obj
        except json.JSONDecodeError:
            pass

        # Балансировка скобок как последний шанс.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start == -1:
                continue
            depth = 0
            in_str = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        raise json.JSONDecodeError("Cannot parse JSON", text, 0)


def load_router(config_path: str | Path | None = None) -> MultiProviderRouter:
    return MultiProviderRouter(config_path or os.getenv("SCRAPER_CONFIG", "configs/config_default.yaml"))
