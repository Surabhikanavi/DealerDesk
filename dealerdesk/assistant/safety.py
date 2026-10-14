"""Defences against instructions hidden in supplier-written catalogue text.

Layer 1 (here): instruction-like sentences are stripped from catalogue text before it is indexed or
shown to the model, and descriptions are never sent to the model at all.
Layer 2 (agent.py): the system prompt marks tool output as data.
Layer 3 (agent.py): the final reply is checked: every figure, percentage, code and SKU in it must
come from this turn's tool results or the catalogue, otherwise it is rewritten.
"""
import re

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|messages)",
    r"disregard (all |any )?(the )?(previous|prior|above|earlier)",
    r"\[\s*(system|admin|assistant|developer)\s*\]",
    r"\b(system|admin|developer|god|jailbreak)\s+(mode|prompt|message|override)\b",
    r"\byou are now\b",
    r"\b(note|message|instruction)s? (for|to) (the )?(ai|assistant|llm|model|chatbot)",
    r"\bai assistants?\b",
    r"\b(tell|inform) the (customer|dealer|user)\b",
    r"\bconfirm (the|this|any|all|their|the dealer's) orders?\b",
    r"\bwithout checking\b",
    r"\b\d{1,3}\s?% off\b",
    r"\b(promo|coupon|discount) code\b",
    r"\binclude the code\b",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def looks_like_injection(text):
    return bool(text) and bool(_INJECTION.search(text))


def strip_injections(text):
    """Drop every sentence that reads like an instruction to an AI. Ordinary product text survives."""
    if not text:
        return ""
    return " ".join(s for s in _SENTENCE_SPLIT.split(text) if not _INJECTION.search(s)).strip()


def safe_name(product):
    """A product name fit to show the model. A name carrying instructions is replaced by a neutral label."""
    name = (product.name or "").strip()[:150]
    if looks_like_injection(name):
        return f"{product.category or 'Part'} ({product.sku})"
    return name
