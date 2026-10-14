"""Part retrieval: vehicle resolution + hybrid lexical ranking over the catalogue.

Why not embeddings: the catalogue is 600 short, structured records ("Brake Pad Set - KTM Duke 390").
What matters is (1) getting the vehicle exactly right, which is a structured filter, not a similarity,
and (2) matching the part type despite plurals, typos and synonyms. Word + character n-gram TF-IDF
handles (2) deterministically, needs no second provider (Groq has no embeddings API) and no model
download, and runs in a few milliseconds. See DECISIONS.md for where it fails.
"""
import re
import threading

import numpy as np
from django.db.models import Max
from sklearn.feature_extraction.text import TfidfVectorizer

from catalog.models import Product

from .safety import strip_injections

SKU_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,6}\b")
MAKES = {"bajaj", "ktm", "tvs", "yamaha", "hero", "suzuki", "honda", "hyundai", "maruti", "tata", "mahindra",
         "royal", "enfield", "re", "kia", "toyota", "skoda", "renault", "nissan", "ford", "volkswagen", "vw"}
SYNONYMS = {
    "tire": "tyre", "tires": "tyre", "tyres": "tyre", "lamp": "light", "headlamp": "headlight", "blinker": "indicator",
    "blinkers": "indicator", "winker": "indicator", "rotor": "disc", "disk": "disc", "lubricant": "lube",
    "lubrication": "lube", "pads": "pad", "brakepad": "brake pad", "sparkplug": "spark plug", "mirrors": "mirror",
    "gloves": "glove", "grips": "grip", "handlebar": "handle", "jacket": "jacket", "lid": "helmet", "guard": "guard",
    "bulb": "light", "led": "led", "clutchplate": "clutch plate", "sprockets": "sprocket", "cleaner": "cleaner",
}
STOP = {"for", "a", "an", "the", "my", "do", "you", "have", "any", "of", "need", "want", "some", "with", "and",
        "bike", "motorcycle", "car", "scooter", "part", "parts", "please", "pls", "me", "i", "is", "are", "in", "on"}


def tokens(text):
    out = []
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
        tok = SYNONYMS.get(tok, tok)
        for t in tok.split():
            if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
                t = t[:-1]
            out.append(t)
    return out


def normalise(text):
    return " ".join(tokens(text))


class VehicleIndex:
    """Maps free text ("duke 390", "RE Classic", "mt15") to catalogue fitments."""

    def __init__(self, fitments):
        self.fitments = sorted({f for f in fitments if f and f.lower() != "universal"})
        self.entries = []
        for f in self.fitments:
            toks = tokens(f)
            model = [t for t in toks if t not in MAKES] or toks
            self.entries.append({
                "fitment": f,
                "compact_full": "".join(toks),
                "compact_model": "".join(model),
                "keys": {t for t in model if not t.isdigit() and len(t) >= 2},
                "numbers": {t for t in model if any(c.isdigit() for c in t)},
            })

    def resolve(self, text):
        """Returns (fitments, ambiguous). [] when nothing in the text names a known vehicle."""
        toks = tokens(text)
        if not toks:
            return [], False
        compact = "".join(toks)
        strong = [e for e in self.entries if e["compact_model"] in compact or e["compact_full"] in compact]
        if strong:
            best = max(len(e["compact_model"]) for e in strong)
            return [e["fitment"] for e in strong if len(e["compact_model"]) == best], False
        tokset = set(toks)
        weak = [e for e in self.entries if e["keys"] & tokset]
        numbers = {t for t in toks if any(c.isdigit() for c in t)}
        if numbers:
            narrowed = [e for e in weak if e["numbers"] & numbers]
            weak = narrowed or weak
        if len(weak) > 1:  # prefer the entries matching the most words ("pulsar ns" -> NS200)
            most = max(len(e["keys"] & tokset) for e in weak)
            weak = [e for e in weak if len(e["keys"] & tokset) == most]
        return [e["fitment"] for e in weak], len(weak) > 1


class CatalogIndex:
    def __init__(self, products):
        self.ids = [p.id for p in products]
        self.pos = {pid: i for i, pid in enumerate(self.ids)}
        self.fitment = [p.vehicle_fitment for p in products]
        self.category = [p.category.lower() for p in products]
        self.part_type = [normalise(p.name.split(" - ")[0]) for p in products]
        docs = [normalise(f"{p.name} {p.name.split(' - ')[0]} {p.category} {p.brand} {p.vehicle_fitment} "
                          f"{strip_injections(p.description)[:300]}") for p in products]
        self.word = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit(docs)
        self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True).fit(docs)
        self.word_m = self.word.transform(docs)
        self.char_m = self.char.transform(docs)
        self.vehicles = VehicleIndex(self.fitment)

    def rank(self, query, allowed=None, category=None):
        q = normalise(query)
        qtoks = {t for t in q.split() if t not in STOP}
        if not qtoks:
            return []
        scores = 0.6 * (self.word_m @ self.word.transform([q]).T).toarray().ravel() \
            + 0.4 * (self.char_m @ self.char.transform([q]).T).toarray().ravel()
        for i, ptype in enumerate(self.part_type):
            type_toks = {t for t in ptype.split() if t not in {"set", "kit"}}
            if type_toks and type_toks <= qtoks:  # the query names this part type exactly
                scores[i] += 0.5
        mask = np.ones(len(self.ids), dtype=bool)
        if allowed is not None:
            mask &= np.array([f in allowed for f in self.fitment])
        if category:
            c = category.lower().strip()
            mask &= np.array([c in cat for cat in self.category])
        scores = np.where(mask, scores, -1.0)
        order = np.argsort(-scores)
        top = scores[order[0]] if len(order) else 0
        if top < 0.12:
            return []
        return [(self.ids[i], float(scores[i])) for i in order if scores[i] >= max(0.12, 0.75 * top)]


_lock = threading.Lock()
_cache = {"key": None, "index": None}


def get_index():
    """Rebuilt when the catalogue changes (new products from supplier sync, edits via PATCH)."""
    key = (Product.objects.count(), Product.objects.aggregate(m=Max("updated_at"))["m"])
    with _lock:
        if _cache["key"] != key:
            _cache["index"] = CatalogIndex(list(Product.objects.order_by("id")))
            _cache["key"] = key
        return _cache["index"]


def search(query, vehicle=None, category=None, limit=8):
    """Returns {"product_ids": [...], "vehicle": resolved or None, "note": str|None, "vehicles": [...]}."""
    index = get_index()
    result = {"product_ids": [], "resolved_vehicle": None, "note": None, "vehicle_options": []}
    if not index.ids:
        return result

    explicit = [s for s in SKU_RE.findall((query or "").upper()) if s]
    if explicit:
        ids = list(Product.objects.filter(sku__in=explicit).values_list("id", flat=True))
        if ids:
            result["product_ids"] = ids[:limit]
            return result

    vehicle_text = vehicle or query
    fitments, ambiguous = index.vehicles.resolve(vehicle_text)
    if vehicle and not fitments:
        fitments, ambiguous = index.vehicles.resolve(f"{vehicle} {query}")
    if ambiguous:
        result["vehicle_options"] = fitments
        result["note"] = ("The vehicle is ambiguous. Ask the dealer which of these vehicles they mean: "
                          + ", ".join(fitments) + ".")
        return result
    if vehicle and not fitments:
        ranked = index.rank(query, allowed={"Universal"}, category=category)
        result["product_ids"] = [pid for pid, _ in ranked[:limit]]
        result["note"] = (f"No parts in the catalogue are listed for '{vehicle}'. Only universal-fit parts "
                          f"(if any) are returned. Do not suggest parts for other vehicles as if they fit.")
        return result

    if fitments:
        result["resolved_vehicle"] = fitments[0]
        ranked = index.rank(query, allowed=set(fitments) | {"Universal"}, category=category)
        # Prefer exact-vehicle parts over universal ones of the same score.
        ranked.sort(key=lambda x: (-(x[1] + (0.05 if index.fitment[index.pos[x[0]]] in fitments else 0))))
        result["product_ids"] = [pid for pid, _ in ranked[:limit]]
        return result

    ranked = index.rank(query, category=category)
    specific = {index.fitment[index.pos[pid]] for pid, _ in ranked} - {"Universal"}
    if len(specific) > 1:
        # Vehicle-specific parts but no vehicle given: the assistant must ask, not guess.
        result["vehicle_options"] = sorted(specific)
        result["note"] = ("No vehicle given and these parts are vehicle-specific. Ask the dealer which vehicle "
                          "(make and model) before recommending anything. Vehicles we stock this part for: "
                          + ", ".join(sorted(specific)) + ".")
        return result
    result["product_ids"] = [pid for pid, _ in ranked[:limit]]
    return result
