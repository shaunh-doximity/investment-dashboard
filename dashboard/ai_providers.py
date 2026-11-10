"""Provider-independent AI text generation for DeepSeek and OpenAI.

DeepSeek is the default and uses the OpenAI Python SDK's compatible Chat
Completions endpoint. OpenAI uses Responses so its optional web-search tool
remains available. Both paths return the same local result structure.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "openai": "gpt-5-nano"}
MODEL_CHOICES = {
    "deepseek": ("deepseek-chat",),
    "openai": ("gpt-5-nano", "gpt-5-mini", "gpt-5"),
}
logger = logging.getLogger(__name__)


class AIProviderError(RuntimeError):
    """Safe, actionable error text suitable for display in the local UI."""


@dataclass(frozen=True)
class AIResult:
    provider: str
    model: str
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    source_metadata: tuple[dict, ...] = ()


def load_ai_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def default_model(provider: str) -> str:
    load_ai_environment()
    normalized = normalize_provider(provider)
    variable = "DEEPSEEK_MODEL" if normalized == "deepseek" else "OPENAI_MODEL"
    configured = os.environ.get(variable, DEFAULT_MODELS[normalized]).strip()
    # The local dashboard intentionally exposes only the tested DeepSeek chat
    # endpoint; unsupported/stale values from .env resolve to that model.
    if normalized == "deepseek" and configured not in MODEL_CHOICES[normalized]:
        return DEFAULT_MODELS[normalized]
    return configured


def normalize_provider(provider: str | None) -> str:
    normalized = (provider or DEFAULT_PROVIDER).strip().lower()
    if normalized not in DEFAULT_MODELS:
        raise ValueError("AI provider must be 'deepseek' or 'openai'.")
    return normalized


def normalize_error(provider: str, model: str, error: Exception) -> AIProviderError:
    """Translate SDK/HTTP errors without echoing request bodies or credentials."""
    status = getattr(error, "status_code", None)
    error_name = type(error).__name__.lower()
    error_code = str(getattr(error, "code", "") or "").lower()
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        body_error = body.get("error")
        if isinstance(body_error, dict):
            error_code += " " + str(body_error.get("code", "")).lower()
            error_code += " " + str(body_error.get("type", "")).lower()
            error_code += " " + str(body_error.get("message", "")).lower()
    if status == 402 or "insufficient_balance" in error_code or "insufficient balance" in error_code:
        message = f"{provider.title()} account balance is insufficient. Add API credits or choose another provider in AI settings."
    elif status == 401 or status == 403 or "authentication" in error_name or "permission" in error_name:
        message = f"{provider.title()} authentication failed. Check its API key and account access in .env."
    elif status == 429 or "ratelimit" in error_name or "insufficient_quota" in error_code or "quota" in error_code:
        message = f"{provider.title()} rate limit or quota reached. Check account limits, then retry manually."
    elif "timeout" in error_name:
        message = f"{provider.title()} request timed out. Check connectivity and retry."
    elif any(word in error_name for word in ("connection", "connect", "network", "proxy", "remoteprotocol", "dns")):
        message = f"Could not connect to {provider.title()}. Check network access and retry."
    elif status and status >= 500:
        message = f"{provider.title()} is temporarily unavailable (HTTP {status}). Retry manually later."
    else:
        message = f"{provider.title()} request failed. Check provider/model '{model}' and account limits."
    logger.warning("AI request failed provider=%s model=%s error_type=%s status=%s",
                   provider, model, type(error).__name__, status)
    return AIProviderError(message)


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _usage(response, provider: str) -> tuple[int | None, int | None, int | None]:
    usage = _field(response, "usage")
    if not usage:
        return None, None, None
    input_name, output_name = (("prompt_tokens", "completion_tokens") if provider == "deepseek"
                               else ("input_tokens", "output_tokens"))
    return (_field(usage, input_name), _field(usage, output_name), _field(usage, "total_tokens"))


def _sources(response) -> tuple[dict, ...]:
    sources = {}
    for item in _field(response, "output", []) or []:
        if _field(item, "type") != "message":
            action = _field(item, "action", {})
            candidates = _field(action, "sources", []) or []
            for source in candidates:
                url = _field(source, "url")
                if url:
                    sources[url] = {"url": url, "title": _field(source, "title", "")}
            continue
        for content in _field(item, "content", []) or []:
            for annotation in _field(content, "annotations", []) or []:
                if _field(annotation, "type") == "url_citation":
                    citation = _field(annotation, "url_citation", annotation)
                    url = _field(citation, "url")
                    if url:
                        sources[url] = {"url": url, "title": _field(citation, "title", "")}
    return tuple(sources.values())


def generate_text(
    *, prompt: str, provider: str | None = None, model: str | None = None,
    max_output_tokens: int = 1400, instructions: str = "",
    web_search: bool = False, json_mode: bool = False,
) -> AIResult:
    """Generate text via one explicitly selected provider; never fall back silently."""
    load_ai_environment()
    provider = normalize_provider(provider or os.environ.get("AI_PROVIDER", DEFAULT_PROVIDER))
    model = (model or default_model(provider)).strip()
    if not model:
        raise AIProviderError("Choose a model in AI settings before requesting generation.")
    if provider == "deepseek" and model not in MODEL_CHOICES["deepseek"]:
        raise AIProviderError("This dashboard currently supports DeepSeek's deepseek-chat model only. Select it in AI settings.")
    if not isinstance(max_output_tokens, int) or not 64 <= max_output_tokens <= 8192:
        raise AIProviderError("Maximum output tokens must be between 64 and 8,192.")
    if web_search and provider != "openai":
        raise AIProviderError("Web search is available only with the OpenAI provider. Select OpenAI explicitly to use it.")

    key_name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
    api_key = os.environ.get(key_name)
    if not api_key:
        raise AIProviderError(f"{provider.title()} is not configured. Add {key_name} to the local .env file.")

    try:
        from openai import OpenAI

        client_args = {"api_key": api_key, "timeout": 30, "max_retries": 0}
        if provider == "deepseek":
            client_args["base_url"] = DEEPSEEK_BASE_URL
            client = OpenAI(**client_args)
            messages = []
            if instructions:
                messages.append({"role": "system", "content": instructions})
            messages.append({"role": "user", "content": prompt})
            response = client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_output_tokens,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            choices = _field(response, "choices", []) or []
            message = _field(choices[0], "message") if choices else None
            content = _field(message, "content") if message else None
            finish_reason = _field(choices[0], "finish_reason") if choices else None
            if json_mode and finish_reason == "length":
                raise AIProviderError(
                    "DeepSeek reached the output-token limit before finishing the strategy JSON. "
                    "Increase Maximum output tokens in AI settings and regenerate; no draft was saved."
                )
            if isinstance(content, list):
                content = "".join(str(_field(item, "text", "")) for item in content)
        else:
            client = OpenAI(**client_args)
            kwargs = {"model": model, "input": prompt,
                      "max_output_tokens": max_output_tokens, "store": False}
            if instructions:
                kwargs["instructions"] = instructions
            if json_mode:
                kwargs["text"] = {"format": {"type": "json_object"}}
            if web_search:
                kwargs["tools"] = [{"type": "web_search", "search_context_size": "low"}]
                kwargs["include"] = ["web_search_call.action.sources"]
                kwargs["tool_choice"] = "required"
            response = client.responses.create(**kwargs)
            content = _field(response, "output_text", "")
        if not isinstance(content, str) or not content.strip():
            raise AIProviderError(f"{provider.title()} returned an empty response. Try a different model or retry manually.")
        input_tokens, output_tokens, total_tokens = _usage(response, provider)
        return AIResult(
            provider=provider, model=_field(response, "model", model), text=content.strip(),
            input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
            source_metadata=_sources(response) if provider == "openai" and web_search else (),
        )
    except AIProviderError:
        raise
    except Exception as error:
        raise normalize_error(provider, model, error) from None
