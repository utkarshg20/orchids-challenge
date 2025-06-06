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
import sass
from tenacity import retry, stop_after_attempt, wait_exponential
import openai
import requests

from backend.scraper import scrape, ScrapeBundle


# ───────── Environment & Infra ────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

celery_app = Celery("orchids", broker="redis://localhost:6379/0")
redis      = Redis(host="localhost", port=6379, db=0, decode_responses=True)

openai.api_key = os.getenv("OPENAI_API_KEY")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def chat(messages: list[dict], model: str, max_tokens: int) -> str:
    response = openai.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def extract_json(text: str) -> dict:
    """
    Attempts to find the first “{” and matching “}” in the LLM’s response,
    slice out exactly that bracketed portion, and parse it via orjson.
    If no complete JSON object is found, simply return {} instead of raising.
    """
    if not text or "{" not in text:
        return {}

    # Find the first “{”
    start = text.find("{")
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

    # If we never found a matching “}”, bail out with an empty dict
    if end == -1:
        return {}

    candidate = text[start : end + 1]
    try:
        return orjson.loads(candidate)
    except Exception:
        # If parsing fails for any reason, return empty dict
        return {}


def split_head_body(dom_html: str) -> tuple[str, str]:
    head_match = re.search(r"<head.*?>(.*?)</head>", dom_html, flags=re.I | re.S)
    body_match = re.search(r"<body.*?>(.*?)</body>", dom_html, flags=re.I | re.S)

    head_html = head_match.group(1).strip() if head_match else ""
    body_html = body_match.group(1).strip() if body_match else dom_html
    return head_html, body_html


# ══════════ PROMPT BUILDERS ════════════════════════════════════════════════

def p_stage1(body_html: str, palette: list[str], fonts: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You receive the BODY HTML (no <head>) of a public website, plus a color palette and font URLs. "
            "Return ONLY a JSON object with keys:\n"
            "  • header   (brief description)\n"
            "  • nav      (structure/items)\n"
            "  • main     (description)\n"
            "  • sections (array of descriptions)\n"
            "  • footer   (brief description)\n"
            "Do NOT output markdown—only pure JSON."
        ),
    }
    user = {
        "role": "user",
        "content": (
            "BODY HTML (truncated if too long):\n```\n"
            f"{body_html[:20000]}\n"
            "…(truncated)…\n```\n\n"
            f"Palette: {palette}\nFonts: {fonts}"
        ),
    }
    return [system, user]


def p_tokens(critical_css: str, palette: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a design-systems expert. Given a blob of CSS and a color palette, "
            "return a JSON object:\n"
            "  • primary_font  (string)\n"
            "  • secondary_font (string)\n"
            "  • font_scale     ({ base, h1, …, h6 })\n"
            "  • brand_colors   ({ primary, bg, text, accent })\n"
            "Output only JSON."
        ),
    }
    user = {
        "role": "user",
        "content": f"CSS:\n```\n{critical_css}\n```\n\nPalette: {palette}",
    }
    return [system, user]


def p_scss(tokens_json: str, css_links: list[str], palette: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a CSS/SASS engineer. Given design-tokens JSON, external CSS URLs, and a palette, "
            "generate SCSS that:\n"
            "  1. Implements a responsive typographic scale.\n"
            "  2. Provides utility classes for brand colors.\n"
            "  3. Imports external CSS via @import/@use.\n"
            "Return a single ```scss … ``` block only."
        ),
    }
    user = {
        "role": "user",
        "content": (
            f"Design-tokens JSON:\n```\n{tokens_json}\n```\n"
            f"External CSS URLs: {css_links}\nPalette: {palette}"
        ),
    }
    return [system, user]


def p_rewrite(structure_json: str, full_html: str) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a DOM-refactoring bot. Given a semantic JSON outline and the FULL HTML, "
            "rewrite only the <body> so that elements use class names from the SCSS (e.g., text-primary, bg-bg). "
            "Return only the inner HTML of <body> (no <head> or <html> wrapper)."
        ),
    }
    user = {
        "role": "user",
        "content": f"Semantic JSON:\n```\n{structure_json}\n```\n\nFull HTML:\n```\n{full_html}\n```",
    }
    return [system, user]


def p_assets(dom_html: str, css_links: list[str], font_links: list[str], script_tags: list[str]) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a web-optimization assistant. Given full HTML, a list of CSS URLs, font URLs, and raw script tags, "
            "identify:\n"
            "  • inline_images (array of { selector, reason })\n"
            "  • needs_font_preload (boolean)\n"
            "  • updated_script_tags (array of raw <script>…</script> strings)\n"
            "Return only JSON."
        ),
    }
    user = {
        "role": "user",
        "content": (
            f"DOM HTML:\n```\n{dom_html[:20000]}\n```\n\n"
            f"CSS URLs: {css_links}\nFont URLs: {font_links}\n"
            f"Script tags:\n```\n{script_tags[:20000]}\n```"
        ),
    }
    return [system, user]


def p_final(structure_json: str, body_html: str, css_compiled: str) -> list[dict]:
    system = {
        "role": "system",
        "content": (
            "You are a senior frontend engineer. Given semantic JSON, rewritten <body> HTML (possibly truncated), and compiled CSS (possibly truncated), "
            "produce one complete HTML string that:\n"
            "  1. Ensures all <img> have alt text.\n"
            "  2. Places <link> and <script> tags correctly in <head>.\n"
            "  3. Provides font fallbacks.\n"
            "  4. Adds ARIA labels to navigation.\n"
            "Return only the final HTML."
        ),
    }
    user = {
        "role": "user",
        "content": (
            f"Semantic JSON:\n```\n{structure_json}\n```\n\n"
            "Rewritten <body> HTML (truncated):\n```\n"
            f"{body_html[:100000]}\n"
            "…(truncated)…\n```\n\n"
            "Compiled CSS (truncated):\n```\n"
            f"{css_compiled[:50000]}\n"
            "…(truncated)…\n```"
        ),
    }
    return [system, user]


# ═══════════ ASSEMBLER HELPERS ═══════════════════════════════════════════════

def assemble(head_lines: list[str], body_html: str, url: str) -> str:
    """
    Build a final HTML document by deduplicating/trimming <head> / <body> / rewiring absolute URLs.
    """
    filtered = [line for line in head_lines if isinstance(line, str) and line.strip() != ""]
    head_section = "<head>\n" + "\n".join(filtered) + "\n</head>"
    full = "<!DOCTYPE html>\n<html>\n" + head_section + body_html + "\n</html>"

    # Remove any extra meta, link, or <title> tags that might have been duplicated
    prefix, sep, suffix = full.partition("</head>")
    suffix = re.sub(r"<meta\s[^>]*?>", "", suffix, flags=re.I)
    suffix = re.sub(r"<link\s[^>]*?>", "", suffix, flags=re.I)
    suffix = re.sub(r"<title[^>]*?>.*?</title>", "", suffix, flags=re.I | re.S)
    full = prefix + "</head>" + suffix

    # Rewrite any leading "/" paths to absolute origin
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    full = full.replace('href="/', f'href="{origin}/')
    full = full.replace('src="/',  f'src="{origin}/')

    return full


# ═══════════ MAIN TASK ═══════════════════════════════════════════════════════

@celery_app.task(bind=True, acks_late=True, max_retries=1)
def clone_site(self, job_id: str, url: str):
    redis_key = f"jobs:{job_id}"
    redis.hset(redis_key, mapping={"status": "running", "progress": 0})

    try:
        # ───── Stage 0: Scrape ───────────────────────────────────────────────
        bundle: ScrapeBundle = scrape(url)

        tmp_dir = pathlib.Path(tempfile.gettempdir()) / f"orchids_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        bundle_json_path = tmp_dir / "bundle.json"
        # If ScrapeBundle has a .to_dict(), we serialize it; otherwise adapt accordingly
        try:
            bundle_json_path.write_text(json.dumps(bundle.to_dict(), indent=2), "utf-8")
        except AttributeError:
            # Fallback: if bundle has no to_dict(), just write the raw DOM
            bundle_json_path.write_text(json.dumps({"dom_html": bundle.dom_html}, indent=2), "utf-8")

        redis.hset(redis_key, mapping={
            "progress": 10,
            "bundle_json": str(bundle_json_path),
        })

        # If your ScrapeBundle included a screenshot_path, base64‐encode it:
        hero_b64 = ""
        if hasattr(bundle, "screenshot_path") and pathlib.Path(bundle.screenshot_path).exists():
            hero_bytes = open(bundle.screenshot_path, "rb").read()
            hero_b64 = base64.b64encode(hero_bytes).decode()

        # ───── Stage 1: Semantic JSON Outline (send only <body>) ─────────────
        _, body_html_full = split_head_body(bundle.dom_html)
        stage1_msgs = p_stage1(body_html_full, bundle.palette, bundle.font_links)
        struct_raw = chat(stage1_msgs, model="gpt-4.1", max_tokens=8000)
        structure = extract_json(struct_raw)
        redis.hset(redis_key, mapping={"progress": 25})

        # ───── Stage 2: Design Tokens JSON ───────────────────────────────────
        if bundle.css_links:
            css_contents: list[str] = []
            for css_url in bundle.css_links:
                if css_url.startswith("http"):
                    try:
                        resp = requests.get(css_url, timeout=5)
                        if resp.status_code == 200:
                            css_contents.append(f"/* {css_url} */\n{resp.text}")
                    except Exception:
                        pass
            critical_css = "\n".join(css_contents)
        else:
            critical_css = ""

        stage2_msgs = p_tokens(critical_css, bundle.palette)
        tokens_raw = chat(stage2_msgs, model="gpt-4.1", max_tokens=6000)
        tokens_obj = extract_json(tokens_raw)
        redis.hset(redis_key, mapping={
            "progress": 40,
            "tokens_obj": json.dumps(tokens_obj),
        })

        # ───── Stage 3: SCSS → CSS Compilation ───────────────────────────────
        tokens_json_str = orjson.dumps(tokens_obj).decode()
        stage3_msgs = p_scss(tokens_json_str, bundle.css_links, bundle.palette)
        scss_code = chat(stage3_msgs, model="gpt-4.1", max_tokens=12000)

        # Strip out Markdown fences and remove leading “variables” if present
        scss_clean = re.sub(r"```[^`]*scss", "", scss_code, flags=re.I).replace("```", "")
        lines = scss_clean.splitlines()
        if lines and lines[0].strip().lower() == "variables":
            lines = lines[1:]
        scss_clean = "\n".join(lines)

        try:
            css_compiled = sass.compile(string=scss_clean, output_style="expanded")
        except sass.CompileError:
            css_compiled = ""  # fallback if SCSS invalid

        scss_path = tmp_dir / "generated.scss"
        scss_path.write_text(scss_clean, "utf-8")
        redis.hset(redis_key, mapping={
            "progress": 55,
            "scss_code": str(scss_path),
        })

        style_block = f"<style>\n{css_compiled}\n</style>"

        # ───── Stage 4: Rewrite BODY HTML ────────────────────────────────────
        structure_json_str = orjson.dumps(structure).decode()
        stage4_msgs = p_rewrite(structure_json_str, bundle.dom_html)
        body_html = chat(stage4_msgs, model="gpt-4.1", max_tokens=15000)
        redis.hset(redis_key, mapping={"progress": 70})

        # ───── Stage 5: Asset Inlining & Optimization ────────────────────────
        stage5_msgs = p_assets(bundle.dom_html, bundle.css_links, bundle.font_links, bundle.script_tags)
        assets_raw = chat(stage5_msgs, model="gpt-4.1", max_tokens=6000)
        assets_obj = extract_json(assets_raw)

        inline_images = assets_obj.get("inline_images", [])
        for image_info in inline_images:
            selector = image_info.get("selector", "")
            if selector.startswith("img"):
                body_html = re.sub(
                    r'<img\s+([^>]*?)src="[^"]+"(.*?)>',
                    f'<img \\1src="data:image/png;base64,{hero_b64}"\\2>',
                    body_html,
                    count=1
                )

        needs_font_preload = assets_obj.get("needs_font_preload", False)
        updated_scripts   = assets_obj.get("updated_script_tags", [])

        script_lines: list[str] = []
        for tag in bundle.script_tags:
            script_lines.append(tag)
        for updated in updated_scripts:
            script_lines.append(updated)

        redis.hset(redis_key, mapping={"progress": 80})

        # ───── Stage 6: Final QA Pass (send truncated inputs) ────────────────
        full_body_html = f"<body>\n{body_html}\n</body>"
        stage6_msgs = p_final(structure_json_str, full_body_html, css_compiled)
        final_html_raw = chat(stage6_msgs, model="gpt-4.1", max_tokens=20000)
        redis.hset(redis_key, mapping={"progress": 90})

        # ───── Assemble head + body ──────────────────────────────────────────
        head_lines: list[str] = [
            '<meta charset="UTF-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            *[m for m in bundle.meta_tags if m and "charset" not in m.lower() and "viewport" not in m.lower()],
            *[f'<link rel="icon" href="{href}">' for href in bundle.link_icons],
            *[f'<link rel="stylesheet" href="{href}">' for href in bundle.css_links],
            style_block,
            *script_lines
        ]

        if final_html_raw.strip().lower().startswith("<!doctype html>"):
            final_html = final_html_raw
        else:
            final_html = assemble(head_lines, final_html_raw, url)

        html_fp = tmp_dir / "index.html"
        html_fp.write_text(final_html, "utf-8")

        redis.hset(redis_key, mapping={
            "status":    "complete",
            "progress":  "100",
            "html_path": str(html_fp),
        })

    except Exception as exc:
        # If anything goes wrong, record the traceback & mark status="error"
        tb = traceback.format_exc()
        redis.hset(redis_key, mapping={
            "status": "error",
            "detail": tb,
        })
        raise