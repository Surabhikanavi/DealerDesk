"""Thin Groq client. Returns provider-neutral dicts so the agent loop (and tests) don't depend on the SDK."""
import json
import logging
import time

from django.conf import settings

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    pass


class GroqLLM:
    def __init__(self, api_key=None, models=None):
        from groq import Groq  # imported lazily so tests can run without the SDK configured

        api_key = api_key or settings.GROQ_API_KEY
        if not api_key:
            raise LLMUnavailable("GROQ_API_KEY is not set")
        self.client = Groq(api_key=api_key, max_retries=0)
        self.models = models or [settings.GROQ_MODEL, *settings.GROQ_FALLBACK_MODELS]

    def complete(self, messages, tools, deadline):
        """One chat completion with tool calling. Tries each model, with a short retry for rate limits
        and malformed tool calls, inside the request's time budget."""
        import groq

        last = None
        for model in self.models:
            for attempt in range(4):
                remaining = deadline - time.monotonic()
                if remaining < 3:
                    raise LLMUnavailable(f"out of time ({last})")
                kwargs = dict(model=model, messages=messages, tools=tools, tool_choice="auto",
                              temperature=0, max_tokens=1024, timeout=min(25, remaining - 1))
                if model.startswith("openai/gpt-oss"):
                    kwargs["reasoning_effort"] = "low"
                else:
                    kwargs["parallel_tool_calls"] = False
                try:
                    resp = self.client.chat.completions.create(**kwargs)
                except groq.RateLimitError as exc:  # wait as told (within our budget), then retry the same model
                    last = exc
                    log.warning("groq rate limited on %s: %s", model, exc)
                    time.sleep(min(self._retry_after(exc), max(0.0, deadline - time.monotonic() - 8)))
                    continue
                except groq.BadRequestError as exc:  # e.g. tool_use_failed: the model emitted a bad call
                    last = exc
                    log.warning("groq bad request on %s: %s", model, exc)
                    continue
                except (groq.NotFoundError, groq.PermissionDeniedError) as exc:  # model retired / not enabled
                    last = exc
                    log.warning("groq model %s unavailable: %s", model, exc)
                    break
                except (groq.APIConnectionError, groq.APITimeoutError, groq.InternalServerError) as exc:
                    last = exc
                    log.warning("groq transient error on %s: %s", model, exc)
                    time.sleep(0.5)
                    continue
                msg = resp.choices[0].message
                calls = []
                for tc in msg.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    calls.append({"id": tc.id, "name": tc.function.name, "arguments": args if isinstance(args, dict) else {}})
                return {"content": msg.content or "", "tool_calls": calls, "model": model}
        raise LLMUnavailable(str(last))

    @staticmethod
    def _retry_after(exc):
        try:
            return max(0.5, min(10.0, float(exc.response.headers.get("retry-after", "2"))))
        except (AttributeError, TypeError, ValueError):
            return 2.0
