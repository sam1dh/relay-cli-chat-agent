"""Provider-agnostic, dependency-light LLM client (plain HTTPS via requests).

Supports Google Gemini and any OpenAI-compatible chat-completions API.
API keys are only ever sent in request headers to the provider, never logged.
"""
from __future__ import annotations

import json
import re

import requests

from . import config


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return config.LLM_PROVIDER in ("gemini", "openai")


def complete(system: str, user: str, *, json_mode: bool = False, temperature: float = 0.2) -> str:
    if config.LLM_PROVIDER == "gemini":
        return _gemini(system, user, json_mode, temperature)
    if config.LLM_PROVIDER == "openai":
        return _openai(system, user, json_mode, temperature)
    raise LLMError("No LLM provider configured")


def complete_json(system: str, user: str) -> dict:
    text = complete(system, user, json_mode=True, temperature=0.0)
    return parse_json_loose(text)


def parse_json_loose(text: str) -> dict:
    """Parse JSON even if the model wrapped it in ```json fences or added prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise LLMError("Model did not return valid JSON")


def _gemini(system: str, user: str, json_mode: bool, temperature: float) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": temperature},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    try:
        r = requests.post(url, json=body, timeout=config.LLM_TIMEOUT_S,
                          headers={"x-goog-api-key": config.GEMINI_API_KEY})
    except requests.RequestException as e:
        raise LLMError(f"Gemini request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise LLMError(f"Gemini HTTP {r.status_code}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, ValueError):
        raise LLMError("Unexpected Gemini response shape") from None


def _openai(system: str, user: str, json_mode: bool, temperature: float) -> str:
    body = {
        "model": config.OPENAI_MODEL,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if config.LLM_REASONING_EFFORT:
        body["reasoning_effort"] = config.LLM_REASONING_EFFORT
    try:
        r = requests.post(f"{config.OPENAI_BASE_URL}/chat/completions", json=body,
                          timeout=config.LLM_TIMEOUT_S,
                          headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
    except requests.RequestException as e:
        raise LLMError(f"LLM request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise LLMError(f"LLM HTTP {r.status_code}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError):
        raise LLMError("Unexpected LLM response shape") from None


def stream(system: str, user: str, temperature: float = 0.2):
    """Yield text deltas as the model generates them (Gemini SSE or OpenAI-style SSE)."""
    if config.LLM_PROVIDER == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:streamGenerateContent?alt=sse"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": temperature}}
        headers = {"x-goog-api-key": config.GEMINI_API_KEY}
    elif config.LLM_PROVIDER == "openai":
        url = f"{config.OPENAI_BASE_URL}/chat/completions"
        body = {"model": config.OPENAI_MODEL, "temperature": temperature, "stream": True,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if config.LLM_REASONING_EFFORT:
            body["reasoning_effort"] = config.LLM_REASONING_EFFORT
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    else:
        raise LLMError("No LLM provider configured")
    try:
        r = requests.post(url, json=body, headers=headers, stream=True, timeout=config.LLM_TIMEOUT_S)
    except requests.RequestException as e:
        raise LLMError(f"LLM request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        r.close()
        raise LLMError(f"LLM HTTP {r.status_code}")
    with r:
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            try:
                if config.LLM_PROVIDER == "gemini":
                    parts = obj["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts)
                else:
                    text = obj["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                continue
            if text:
                yield text
