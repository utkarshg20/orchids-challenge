# backend/tasks.py

import os
import re
import json
import pathlib
import tempfile
import base64
from urllib.parse import urlparse

from celery import Celery
from redis import Redis
from dotenv import load_dotenv
import orjson
import sass          # from libsass
from tenacity import retry, stop_after_attempt, wait_exponential
import openai

from backend.scraper import scrape, ScrapeBundle

# ───────── Environment & Infrastructure ────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

celery_app = Celery("orchids", broker="redis://localhost:6379/0")
redis      = Redis(host="localhost", port=6379, db=0, decode_responses=True)

openai.api_key = os.getenv("OPENAI_API_KEY")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def chat(messages: list[dict], model: str, max_tokens: int) -> str:
    response = openai.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def extract_json(text: str) -> dict:
    start = text.find("{")
    if start == -1:
        raise orjson.JSONDecodeError("No '{' found in response", text, 0)
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise orjson.JSONDecodeError("No matching '}' found in response", text, start)
    candidate = text[start : end + 1]
    return orjson.loads(candidate)


# ═══════════ PROMPT BUILDERS ════════════════════════════════════════════════

def p_stage1(dom_html: str, palette: list[str], fonts: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You receive the first 2000 characters of a web page’s HTML plus a color palette and a list of font URLs. "
            "Return only valid JSON with top-level keys: header, nav, main, sections, footer. "
            "Each value should be a brief textual description or structured outline of that region."
        ),
    }
    user = {
        "role": "user",
        "content": f"HTML (first 2000 chars):\n{dom_html[:2000]}\n\nPalette: {palette}\nFonts: {fonts}",
    }
    return [system, user]


def p_tokens(critical_css: str, palette: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a design-systems expert. Given 'critical CSS' and a color palette, "
            "derive a JSON object with exactly these keys:\n"
            "  • primary_font  (CSS font-stack string)\n"
            "  • secondary_font (CSS font-stack string)\n"
            "  • font_scale: { base: int, h1: int, h2: int, h3: int, h4: int, h5: int, h6: int }\n"
            "  • brand_colors: { primary: hex, bg: hex, text: hex, accent: hex }\n"
            "Return ONLY JSON—no markdown or extra explanation."
        ),
    }
    user = {
        "role": "user",
        "content": f"Critical CSS:\n{critical_css[:12000]}\n\nPalette: {palette}",
    }
    return [system, user]


def p_scss(tokens_json: str, css_links: list[str], palette: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a senior CSS/SASS engineer. Given a design-tokens JSON, generate SCSS that:\n"
            "  1. Implements responsive typographic scale for base/h1..h6 based on tokens.\n"
            "  2. Exposes utility classes for colors (e.g. .text-primary, .bg-bg, .text-accent).\n"
            "  3. Imports any external CSS URLs via @import.\n"
            "Return a single code-fenced ```scss … ``` block. No markdown or explanation."
        ),
    }
    user = {
        "role": "user",
        "content": (
            f"Design-tokens JSON:\n{tokens_json}\n\n"
            f"External CSS URLs: {css_links}\n"
            f"Palette: {palette}"
        ),
    }
    return [system, user]


def p_rewrite(structure_json: str, html_snippet: str) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a DOM refactoring bot. Given a JSON semantic outline and the first ~5000 chars "
            "of the original HTML, rewrite the BODY content so that elements use class names "
            "matching the design tokens (e.g., text-primary, bg-bg, btn, hero). "
            "Do NOT output <head>—only the inner HTML of <body>. Return raw HTML with no markdown."
        ),
    }
    user = {
        "role": "user",
        "content": f"Semantic JSON:\n{structure_json}\n\nHTML snippet:\n{html_snippet[:5000]}",
    }
    return [system, user]


# ═══════════ ASSEMBLER HELPERS ═══════════════════════════════════════════════

def assemble(head_lines: list[str], body_html: str, url: str) -> str:
    # Filter out any None or empty strings before joining
    filtered = [line for line in head_lines if isinstance(line, str) and line.strip() != ""]
    head_section = "<head>\n" + "\n".join(filtered) + "\n</head>"
    full = "<!DOCTYPE html>\n<html>\n" + head_section + body_html

    prefix, sep, suffix = full.partition("</head>")
    suffix = re.sub(r"<meta\s[^>]*?>", "", suffix, flags=re.I)
    suffix = re.sub(r"<link\s[^>]*?>", "", suffix, flags=re.I)
    suffix = re.sub(r"<title[^>]*?>.*?</title>", "", suffix, flags=re.I | re.S)
    full = prefix + "</head>" + suffix

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    full = full.replace('href="/', f'href="{origin}/')
    full = full.replace('src="/', f'src="{origin}/')
    return full


# ═══════════ MAIN TASK ══════════════════════════════════════════════════════

@celery_app.task(bind=True, acks_late=True, max_retries=1)
def clone_site(self, job_id: str, url: str):
    redis_key = f"jobs:{job_id}"
    redis.hset(redis_key, mapping={"status": "running", "progress": 0})

    try:
        # ───── Stage 0: Scrape ────────────────────────────────────────────
        bundle: ScrapeBundle = scrape(url)
        redis.hset(redis_key, mapping={"progress": 10})

        hero_b64 = base64.b64encode(open(bundle.screenshot_path, "rb").read()).decode()

        tmp_dir = pathlib.Path(tempfile.gettempdir()) / f"orchids_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        (tmp_dir / "bundle.json").write_text(
            json.dumps(bundle.to_dict(), indent=2), "utf-8"
        )

        # ───── Stage 1: Semantic JSON Outline ─────────────────────────────
        stage1_msgs = p_stage1(bundle.dom_html, bundle.palette, bundle.font_links)
        struct_raw = chat(stage1_msgs, model="gpt-3.5-turbo", max_tokens=800)
        structure = extract_json(struct_raw)
        redis.hset(redis_key, mapping={"progress": 30})

        # ───── Stage 2: Design Tokens JSON ────────────────────────────────
        stage2_msgs = p_tokens(bundle.critical_css, bundle.palette)
        tokens_raw = chat(stage2_msgs, model="gpt-4o", max_tokens=700)
        tokens_obj = extract_json(tokens_raw)
        redis.hset(redis_key, mapping={"progress": 45})

        # ───── Stage 3: SCSS → CSS Compilation ────────────────────────────
        tokens_json_str = orjson.dumps(tokens_obj).decode()
        stage3_msgs = p_scss(tokens_json_str, bundle.css_links, bundle.palette)
        scss_code = chat(stage3_msgs, model="gpt-4o", max_tokens=1500)
        scss_clean = re.sub(r"```[^`]*scss", "", scss_code, flags=re.I).replace("```", "")
        css_compiled = sass.compile(string=scss_clean, output_style="expanded")
        style_block = f"<style>\n{css_compiled}\n</style>"
        redis.hset(redis_key, mapping={"progress": 60})

        # ───── Stage 4: Rewrite BODY HTML ────────────────────────────────
        structure_json_str = orjson.dumps(structure).decode()
        stage4_msgs = p_rewrite(structure_json_str, bundle.dom_html)
        body_html = chat(stage4_msgs, model="gpt-4o", max_tokens=1200)
        redis.hset(redis_key, mapping={"progress": 75})

        # ───── Stage 5: Assemble <head> + <body> ─────────────────────────
        head_lines: list[str] = [
            '<meta charset="UTF-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            *[
                m
                for m in bundle.meta_tags
                if m is not None and "charset" not in m.lower() and "viewport" not in m.lower()
            ],
            *bundle.link_icons,
            *[f'<link rel="stylesheet" href="{href}">' for href in bundle.css_links],
            style_block,
        ]

        final_html = assemble(head_lines, body_html, url)

        html_fp = tmp_dir / "index.html"
        html_fp.write_text(final_html, "utf-8")

        redis.hset(redis_key, mapping={
            "status": "complete",
            "progress": 100,
            "html_path": str(html_fp),
        })

    except Exception as exc:
        redis.hset(redis_key, mapping={
            "status": "error",
            "detail": str(exc),
        })
        raise
