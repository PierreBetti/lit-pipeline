#!/usr/bin/env python3
"""
lit-pipeline: nightly enrichment of the Notero (Zotero -> Notion) database.

  1. Metrics      citation count, field-normalized citation percentile and FWCI (OpenAlex)
  2. Triage       AI summary, relevance to the PhD, research questions, ecosystem, gases,
                  methods, key result, and Category when it is still empty
                  (Gemini API free tier by default)
  3. Suggestions  papers strongly connected to your library but not in it yet
                  (OpenAlex citation links + Semantic Scholar recommendations)
  4. Graph        interactive citation map written to docs/index.html (GitHub Pages)

Usage:
  python pipeline.py              full run
  python pipeline.py --no-ai      skip the AI triage step
  python pipeline.py --dry-run    read and compute everything, write nothing to Notion
"""

import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------
# Configuration (secrets come from environment variables / GitHub secrets)
# ----------------------------------------------------------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")          # free tier, default AI provider
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")    # optional paid alternative
S2_KEY = os.environ.get("S2_API_KEY", "")  # optional, Semantic Scholar works without it
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
# Tried in order. A model whose free quota is 0 for your key is skipped automatically.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-flash-lite-latest,gemini-flash-latest").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
GEMINI_SECONDS_BETWEEN_CALLS = float(os.environ.get("GEMINI_SECONDS_BETWEEN_CALLS", "7"))  # stays under free-tier per-minute limits

LIBRARY_DS = os.environ.get("NOTERO_DATA_SOURCE_ID", "87a4954a-4928-8345-b3a8-87caeb0b2c4a")
SUGGEST_DS = os.environ.get("SUGGESTED_DATA_SOURCE_ID", "444be872-ca23-4e7e-8d93-665568721b36")

MAX_TRIAGE_PER_RUN = int(os.environ.get("MAX_TRIAGE_PER_RUN", "40"))
MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "40"))
MIN_CONNECTIONS = int(os.environ.get("MIN_CONNECTIONS", "2"))
GRAPH_SUGGESTIONS = int(os.environ.get("GRAPH_SUGGESTIONS", "25"))

DRY_RUN = "--dry-run" in sys.argv
# Gemini (free) is used when its key is set; Claude only if you choose to add a paid key instead.
AI_PROVIDER = "none" if "--no-ai" in sys.argv else ("gemini" if GEMINI_KEY else ("claude" if ANTHROPIC_KEY else "none"))
TODAY = dt.date.today().isoformat()

# Option lists must match the Notion property options exactly.
CATEGORIES = [
    "Fluxes & microclimate",
    "Vegetation & tree stems",
    "Soil & water microbes",
    "Hydrology & biogeochemistry",
    "Methods & instrumentation",
    "Nature-based solutions & policy",
]
RESEARCH_QUESTIONS = [
    "RQ1 Source/sink controls",
    "RQ2 Disturbed vs intact",
    "RQ3 Anthropogenic impact",
    "RQ4 Soil vs water C sources",
    "RQ5 Microbial taxonomy & function",
]
ECOSYSTEMS = ["Forested swamp", "Peatland / bog / fen", "Marsh", "Upland forest", "Other wetland", "Other"]
GASES = ["CO2", "CH4", "N2O"]
SKIP_WORK_TYPES = {"paratext", "erratum", "retraction", "editorial", "letter", "peer-review"}


def log(*args):
    print(*args, flush=True)


# ----------------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------------
NOTION_API = "https://api.notion.com/v1"


def notion(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    for attempt in range(6):
        r = requests.request(method, f"{NOTION_API}{path}", headers=headers, json=body, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        if not r.ok:
            raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:600]}")
        time.sleep(0.34)  # Notion allows about 3 requests per second
        return r.json()
    raise RuntimeError(f"Notion {method} {path}: too many retries")


def query_all(data_source_id):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"/data_sources/{data_source_id}/query", body)
        pages.extend(res["results"])
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def update_page(page_id, props):
    if DRY_RUN:
        log(f"   [dry-run] would update {page_id}: {sorted(props)}")
        return
    notion("PATCH", f"/pages/{page_id}", {"properties": props})


def create_page(data_source_id, props):
    if DRY_RUN:
        log(f"   [dry-run] would create page in {data_source_id}")
        return
    notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                              "properties": props})


def read(prop):
    """Turn a Notion property value into a plain Python value."""
    if not prop:
        return None
    t = prop["type"]
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop[t])
    if t in ("url", "number"):
        return prop[t]
    if t == "multi_select":
        return [o["name"] for o in prop["multi_select"]]
    if t in ("select", "status"):
        return prop[t]["name"] if prop[t] else None
    if t == "date":
        return prop["date"]["start"] if prop["date"] else None
    return None


def p_text(text):
    text = (text or "").strip()[:1990]
    return {"rich_text": [{"type": "text", "text": {"content": text}}] if text else []}


def p_title(text):
    return {"title": [{"type": "text", "text": {"content": (text or "Untitled")[:1990]}}]}


def p_num(x):
    return {"number": x}


def p_url(u):
    return {"url": u or None}


def p_multi(names):
    return {"multi_select": [{"name": n} for n in names]}


def p_select(name):
    return {"select": {"name": name} if name else None}


def p_date(d):
    return {"date": {"start": d} if d else None}


# ----------------------------------------------------------------------------
# OpenAlex
# ----------------------------------------------------------------------------
OPENALEX = "https://api.openalex.org"
WORK_FIELDS = ("id,doi,display_name,publication_year,type,cited_by_count,fwci,"
               "citation_normalized_percentile,referenced_works,authorships,"
               "primary_location,abstract_inverted_index")


def openalex(path, params=None):
    params = dict(params or {})
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    for attempt in range(5):
        r = requests.get(f"{OPENALEX}{path}", params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            log(f"   OpenAlex {path} -> {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    return None


def short_id(openalex_id):
    return openalex_id.rsplit("/", 1)[-1] if openalex_id else None


def norm_doi(value):
    if not value:
        return None
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+", value)
    return m.group(0).rstrip(".").lower() if m else None


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def work_by_doi(doi):
    return openalex(f"/works/doi:{requests.utils.quote(doi, safe='/:')}", {"select": WORK_FIELDS})


def work_by_title(title):
    res = openalex("/works", {"search": title, "per_page": 5, "select": WORK_FIELDS})
    for w in (res or {}).get("results", []):
        if norm_title(w.get("display_name")) == norm_title(title):
            return w
    return None


def works_filter(filter_str, per_page=200, sort=None, select=WORK_FIELDS):
    params = {"filter": filter_str, "per_page": per_page, "select": select}
    if sort:
        params["sort"] = sort
    res = openalex("/works", params)
    return (res or {}).get("results", [])


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def abstract_text(inverted_index):
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def first_author(work):
    auths = work.get("authorships") or []
    if not auths:
        return "Unknown"
    name = (auths[0].get("author") or {}).get("display_name") or "Unknown"
    return name.split()[-1]


def author_list(work, limit=6):
    names = [(a.get("author") or {}).get("display_name") for a in (work.get("authorships") or [])]
    names = [n for n in names if n]
    return ", ".join(names[:limit]) + (" et al." if len(names) > limit else "")


def journal(work):
    src = ((work.get("primary_location") or {}).get("source") or {})
    return src.get("display_name") or ""


def percentile(work):
    v = (work.get("citation_normalized_percentile") or {}).get("value")
    return round(v * 100, 1) if v is not None else None


# ----------------------------------------------------------------------------
# Step 0: load the library
# ----------------------------------------------------------------------------
def load_library():
    papers = []
    for page in query_all(LIBRARY_DS):
        pr = page["properties"]
        papers.append({
            "page_id": page["id"],
            "notion_url": page.get("url"),
            "name": read(pr.get("Name")) or "Untitled",
            "title": read(pr.get("Title")) or read(pr.get("Name")) or "",
            "abstract": read(pr.get("Abstract")) or "",
            "journal": read(pr.get("Publication")) or "",
            "doi": norm_doi(read(pr.get("DOI"))),
            "year": read(pr.get("Year")),
            "status": read(pr.get("Reading status")),
            "category": read(pr.get("Category")) or [],
            "triage_date": read(pr.get("Triage date")),
            "citations": read(pr.get("Citations")),
            "percentile": read(pr.get("Citation percentile")),
            "fwci": read(pr.get("FWCI")),
            "openalex_url": read(pr.get("OpenAlex ID")),
        })
    return papers


# ----------------------------------------------------------------------------
# Step 1: metrics
# ----------------------------------------------------------------------------
def enrich_metrics(papers):
    """Look every paper up on OpenAlex, write metrics back when they changed."""
    found = 0
    for p in papers:
        w = work_by_doi(p["doi"]) if p["doi"] else None
        if w is None and p["title"]:
            w = work_by_title(p["title"])
        if not w:
            log(f"   not found on OpenAlex: {p['name']}")
            continue
        found += 1
        p["work"] = w
        if not p["abstract"]:
            p["abstract"] = abstract_text(w.get("abstract_inverted_index"))

        new = {
            "Citations": w.get("cited_by_count"),
            "Citation percentile": percentile(w),
            "FWCI": round(w["fwci"], 2) if w.get("fwci") is not None else None,
        }
        old = {"Citations": p["citations"], "Citation percentile": p["percentile"], "FWCI": p["fwci"]}
        if new != old or p["openalex_url"] != w["id"]:
            props = {k: p_num(v) for k, v in new.items()}
            props["OpenAlex ID"] = p_url(w["id"])
            update_page(p["page_id"], props)
    log(f"   {found}/{len(papers)} papers matched on OpenAlex")


# ----------------------------------------------------------------------------
# Step 2: AI triage
# ----------------------------------------------------------------------------
TRIAGE_TOOL = {
    "name": "record_triage",
    "description": "Record the triage of one scientific paper for a PhD literature database.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "2 to 3 sentences in English: what was studied, where, how, and the main result."},
            "relevance": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "Relevance to the PhD, using the rubric in the system prompt."},
            "relevance_reason": {"type": "string",
                                 "description": "One sentence justifying the score with respect to the PhD."},
            "research_questions": {"type": "array", "items": {"type": "string", "enum": RESEARCH_QUESTIONS},
                                   "description": "PhD research questions this paper substantially informs. May be empty."},
            "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}, "maxItems": 3,
                           "description": "One to three subject categories the paper substantially addresses."},
            "ecosystems": {"type": "array", "items": {"type": "string", "enum": ECOSYSTEMS}},
            "gases": {"type": "array", "items": {"type": "string", "enum": GASES},
                      "description": "Greenhouse gases actually measured or modelled. May be empty."},
            "methods": {"type": "string",
                        "description": "Short phrase, e.g. 'static chambers on soil collars and stems, 2 growing seasons'."},
            "key_result": {"type": "string",
                           "description": "The single most important result, one sentence. Do not invent numbers."},
        },
        "required": ["summary", "relevance", "relevance_reason", "research_questions", "categories",
                     "ecosystems", "gases", "methods", "key_result"],
    },
}

TRIAGE_SYSTEM = """You triage scientific papers for a PhD student so they can decide what to read first.

Here is the student's research context:
<context>
{context}
</context>

Relevance rubric:
5 = directly on the PhD topic (GHG fluxes or carbon dynamics in forested swamps or very similar systems), or a key method or framework the student will reuse.
4 = closely related: other wetland types with the same gases or processes, stem fluxes, or microbial drivers of CH4/N2O.
3 = useful background: general wetland carbon cycling, upland forest fluxes, reviews.
2 = tangential.
1 = not relevant.

Rules: base everything only on the title and abstract provided. If the abstract is missing, say so at the start of the summary and keep the relevance score conservative. Never invent numbers, sites or methods that are not in the text. Record your answer with the record_triage tool."""


def _gemini_schema(schema):
    """Convert the JSON schema above into the subset Gemini's responseSchema accepts."""
    out = {"type": schema["type"].upper()}
    if "description" in schema:
        out["description"] = schema["description"]
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if schema["type"] == "object":
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
        out["required"] = schema.get("required", [])
    if schema["type"] == "array":
        out["items"] = _gemini_schema(schema["items"])
    return out


class GeminiQuotaError(RuntimeError):
    pass


def ask_gemini(system, user):
    """Call Gemini, falling back to the next model when one has no free quota for this key."""
    global GEMINI_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(TRIAGE_TOOL["input_schema"]),
        },
    }
    while GEMINI_MODELS:
        GEMINI_MODEL = GEMINI_MODELS[0]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        for attempt in range(4):
            r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
            if r.ok:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            detail = r.text[:400].replace("\n", " ")
            if r.status_code == 404 or (r.status_code == 429 and "limit: 0" in r.text):
                log(f"   {GEMINI_MODEL} not usable with this key ({r.status_code}), trying the next model")
                break
            if r.status_code == 429 and ("PerDay" in r.text or "per day" in r.text.lower()):
                raise GeminiQuotaError(f"daily free quota reached on {GEMINI_MODEL}")
            if r.status_code in (429, 500, 503):
                wait = 15 * (attempt + 1)
                log(f"   Gemini {r.status_code} on {GEMINI_MODEL}, waiting {wait}s. Google says: {detail[:200]}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini {r.status_code} on {GEMINI_MODEL}: {detail}")
        else:
            log(f"   {GEMINI_MODEL} still rate limited after retries, trying the next model")
        GEMINI_MODELS.pop(0)
    raise GeminiQuotaError("no Gemini model has free quota left for this key today")


def ask_claude(client, system, user):
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1200,
        system=system,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "record_triage"},
        messages=[{"role": "user", "content": user}],
    )
    return next((b.input for b in msg.content if b.type == "tool_use"), None)


def triage_papers(papers):
    if AI_PROVIDER == "none":
        log("   skipped (no GEMINI_API_KEY, or --no-ai)")
        return
    log(f"   using {AI_PROVIDER} ({', '.join(GEMINI_MODELS) if AI_PROVIDER == 'gemini' else CLAUDE_MODEL})")
    client = None
    if AI_PROVIDER == "claude":
        import anthropic  # only needed if you opt into the paid Claude API
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    context = (ROOT / "research_context.md").read_text(encoding="utf-8")
    system = TRIAGE_SYSTEM.format(context=context).replace(
        "Record your answer with the record_triage tool.", "Answer in the requested JSON format.")
    if AI_PROVIDER == "claude":
        system = TRIAGE_SYSTEM.format(context=context)

    todo = [p for p in papers if not p["triage_date"]]
    todo.sort(key=lambda p: p["status"] == "Read")  # unread papers first
    done = 0
    for p in todo[:MAX_TRIAGE_PER_RUN]:
        user = (f"Title: {p['title']}\nYear: {p['year'] or 'unknown'}\n"
                f"Journal: {p['journal'] or 'unknown'}\n"
                f"Abstract: {p['abstract'] or '(no abstract available)'}")
        try:
            if AI_PROVIDER == "gemini":
                result = ask_gemini(system, user)
                time.sleep(GEMINI_SECONDS_BETWEEN_CALLS)
            else:
                result = ask_claude(client, system, user)
        except Exception as e:
            log(f"   AI error on {p['name']}: {e}")
            if isinstance(e, GeminiQuotaError):
                break  # stop for today, the rest is picked up tomorrow
            continue
        if not result:
            log(f"   no triage returned for {p['name']}")
            continue

        def keep(values, allowed):
            return [v for v in (values or []) if v in allowed]

        try:
            relevance = max(1, min(5, int(result.get("relevance", 1))))
        except (TypeError, ValueError):
            relevance = 1
        props = {
            "Summary": p_text(result.get("summary")),
            "Relevance": p_num(relevance),
            "Relevance reason": p_text(result.get("relevance_reason")),
            "Research questions": p_multi(keep(result.get("research_questions"), RESEARCH_QUESTIONS)),
            "Ecosystem": p_multi(keep(result.get("ecosystems"), ECOSYSTEMS)),
            "Gases": p_multi(keep(result.get("gases"), GASES)),
            "Methods": p_text(result.get("methods")),
            "Key result": p_text(result.get("key_result")),
            "Triage date": p_date(TODAY),
        }
        if not p["category"]:  # never overwrite a category you set yourself
            cats = keep(result.get("categories"), CATEGORIES)[:3]
            props["Category"] = p_multi(cats)
            p["category"] = cats
        update_page(p["page_id"], props)
        done += 1
    left = max(0, len(todo) - done)
    log(f"   triaged {done} papers" + (f", {left} left for the next runs" if left else ""))


# ----------------------------------------------------------------------------
# Step 3: suggestions
# ----------------------------------------------------------------------------
def semantic_scholar_recommendations(dois):
    if not dois:
        return []
    headers = {"x-api-key": S2_KEY} if S2_KEY else {}
    body = {"positivePaperIds": [f"DOI:{d}" for d in dois[:100]]}
    for attempt in range(4):
        r = requests.post("https://api.semanticscholar.org/recommendations/v1/papers",
                          params={"limit": 60, "fields": "title,externalIds"},
                          json=body, headers=headers, timeout=60)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if not r.ok:
            log(f"   Semantic Scholar -> {r.status_code}, skipping recommendations")
            return []
        recs = r.json().get("recommendedPapers", [])
        return [norm_doi((x.get("externalIds") or {}).get("DOI")) for x in recs if (x.get("externalIds") or {}).get("DOI")]
    return []


def build_suggestions(papers):
    lib = [p for p in papers if p.get("work")]
    lib_ids = {short_id(p["work"]["id"]) for p in lib}
    lib_dois = {p["doi"] for p in papers if p["doi"]}
    name_of = {short_id(p["work"]["id"]): p["name"] for p in lib}

    links = defaultdict(lambda: {"cited_by": set(), "cites": set(), "s2": False})

    # a) works your papers cite (bibliographic coupling)
    for p in lib:
        for ref in p["work"].get("referenced_works") or []:
            rid = short_id(ref)
            if rid not in lib_ids:
                links[rid]["cited_by"].add(p["name"])

    # b) works that cite your papers
    for chunk in chunks(sorted(lib_ids), 40):
        citing = works_filter("cites:" + "|".join(chunk), per_page=200,
                              sort="cited_by_count:desc", select="id,referenced_works")
        for c in citing:
            cid = short_id(c["id"])
            if cid in lib_ids:
                continue
            hits = {short_id(r) for r in (c.get("referenced_works") or [])} & lib_ids
            links[cid]["cites"].update(name_of[h] for h in hits)

    # c) Semantic Scholar recommendations, mapped back to OpenAlex IDs
    rec_dois = [d for d in semantic_scholar_recommendations(sorted(lib_dois)) if d and d not in lib_dois]
    for chunk in chunks(rec_dois, 50):
        for w in works_filter("doi:" + "|".join(chunk), per_page=50, select="id"):
            wid = short_id(w["id"])
            if wid not in lib_ids:
                links[wid]["s2"] = True

    def score(info):
        return len(info["cited_by"] | info["cites"]) + (1 if info["s2"] else 0)

    ranked = sorted(((wid, info) for wid, info in links.items()
                     if score(info) >= MIN_CONNECTIONS or (info["s2"] and score(info) >= 1)),
                    key=lambda kv: score(kv[1]), reverse=True)

    # fetch metadata for the best candidates
    meta = {}
    wanted = [wid for wid, _ in ranked[:MAX_SUGGESTIONS * 2]]
    for chunk in chunks(wanted, 50):
        for w in works_filter("openalex_id:" + "|".join(chunk), per_page=50):
            meta[short_id(w["id"])] = w

    suggestions = []
    for wid, info in ranked:
        w = meta.get(wid)
        if not w or w.get("type") in SKIP_WORK_TYPES or norm_doi(w.get("doi")) in lib_dois:
            continue
        reasons, sources = [], []
        if info["cited_by"]:
            reasons.append(f"Cited by {len(info['cited_by'])} of your papers ({'; '.join(sorted(info['cited_by'])[:4])})")
            sources.append("Cited by your papers")
        if info["cites"]:
            reasons.append(f"Cites {len(info['cites'])} of your papers ({'; '.join(sorted(info['cites'])[:4])})")
            sources.append("Cites your papers")
        if info["s2"]:
            reasons.append("Recommended by Semantic Scholar from your library")
            sources.append("Semantic Scholar recommendation")
        suggestions.append({"id": wid, "work": w, "score": score(info), "why": ". ".join(reasons) + ".",
                            "sources": sources, "cited_by": info["cited_by"], "cites": info["cites"]})
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    log(f"   {len(links)} connected papers found, keeping the top {len(suggestions)}")
    return suggestions, lib_ids


def sync_suggestions(suggestions, lib_ids):
    existing = {}
    for page in query_all(SUGGEST_DS):
        oid = short_id(read(page["properties"].get("OpenAlex ID")))
        if oid:
            existing[oid] = page

    # Suggestions you have since added to Zotero get marked automatically.
    for oid, page in existing.items():
        if oid in lib_ids and read(page["properties"].get("Decision")) == "To review":
            update_page(page["id"], {"Decision": p_select("Added to Zotero")})

    created = updated = 0
    for s in suggestions:
        w = s["work"]
        props = {
            "Connection score": p_num(s["score"]),
            "Why suggested": p_text(s["why"]),
            "Source": p_multi(s["sources"]),
            "Citations": p_num(w.get("cited_by_count")),
            "Citation percentile": p_num(percentile(w)),
        }
        page = existing.get(s["id"])
        if page:
            update_page(page["id"], props)
            updated += 1
            continue
        props.update({
            "Title": p_title(w.get("display_name")),
            "Authors": p_text(author_list(w)),
            "Year": p_num(w.get("publication_year")),
            "Journal": p_text(journal(w)),
            "DOI": p_url(w.get("doi")),
            "OpenAlex ID": p_url(w["id"]),
            "Abstract": p_text(abstract_text(w.get("abstract_inverted_index"))),
            "Decision": p_select("To review"),
            "First suggested": p_date(TODAY),
        })
        create_page(SUGGEST_DS, props)
        created += 1
    log(f"   {created} new suggestions, {updated} updated")


# ----------------------------------------------------------------------------
# Step 4: graph
# ----------------------------------------------------------------------------
def build_graph(papers, suggestions):
    lib = [p for p in papers if p.get("work")]
    lib_ids = {short_id(p["work"]["id"]) for p in lib}
    lib_by_name = {p["name"]: short_id(p["work"]["id"]) for p in lib}
    nodes, edges = [], []

    for p in lib:
        w = p["work"]
        nodes.append({
            "id": short_id(w["id"]),
            "label": p["name"],
            "title": p["title"],
            "year": p["year"] or w.get("publication_year"),
            "citations": w.get("cited_by_count") or 0,
            "percentile": percentile(w),
            "category": (p["category"] or ["Uncategorized"])[0],
            "read": p["status"] == "Read",
            "doi": w.get("doi") or (f"https://doi.org/{p['doi']}" if p["doi"] else None),
            "notion": p.get("notion_url"),
            "kind": "library",
        })
        for ref in w.get("referenced_works") or []:
            rid = short_id(ref)
            if rid in lib_ids:
                edges.append({"from": short_id(w["id"]), "to": rid})

    for s in suggestions[:GRAPH_SUGGESTIONS]:
        w = s["work"]
        nodes.append({
            "id": s["id"],
            "label": f"{first_author(w)} et al., {w.get('publication_year') or 'n.d.'}",
            "title": w.get("display_name"),
            "year": w.get("publication_year"),
            "citations": w.get("cited_by_count") or 0,
            "percentile": percentile(w),
            "category": "Suggested",
            "read": False,
            "doi": w.get("doi"),
            "notion": None,
            "kind": "suggested",
            "why": s["why"],
        })
        for name in s["cited_by"]:
            edges.append({"from": lib_by_name[name], "to": s["id"]})
        for name in s["cites"]:
            edges.append({"from": s["id"], "to": lib_by_name[name]})

    data = {"generated": TODAY, "nodes": nodes, "edges": edges}
    template = (ROOT / "graph_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__GRAPH_DATA__*/null", json.dumps(data, ensure_ascii=False))
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"   wrote {out.relative_to(ROOT)} ({len(nodes)} nodes, {len(edges)} links)")


# ----------------------------------------------------------------------------
def main():
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN is not set.")
    if not OPENALEX_KEY:
        log("Warning: OPENALEX_API_KEY is not set, OpenAlex will only allow a tiny daily quota.")

    log("Loading library from Notion")
    papers = load_library()
    log(f"   {len(papers)} papers")

    log("1. Citation metrics (OpenAlex)")
    enrich_metrics(papers)

    log("2. AI triage")
    triage_papers(papers)

    log("3. Suggested papers")
    suggestions, lib_ids = build_suggestions(papers)
    sync_suggestions(suggestions, lib_ids)

    log("4. Literature map")
    build_graph(papers, suggestions)
    log("Done.")


if __name__ == "__main__":
    main()
