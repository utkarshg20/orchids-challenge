from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import tempfile, json, uuid, time, subprocess
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from PIL import Image
import numpy as np
from sklearn.cluster import KMeans


@dataclass
class ScrapeBundle:
    url: str
    dom_html: str
    palette: list[str]
    screenshot_path: Path
    critical_css: str
    css_links: list[str]
    script_srcs: list[str]
    inline_scripts: list[str]
    img_srcs: list[str]
    bg_image_urls: list[str]
    meta_tags: list[str]
    link_icons: list[str]
    font_links: list[str]
    inline_svgs: list[str]
    srcset_urls: list[str]
    picture_sources: list[str]
    saved_at: float

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["screenshot_path"] = str(self.screenshot_path)
        return d


def _extract_palette(img: Image.Image, k: int = 5) -> list[str]:
    arr = np.array(img).reshape(-1, 3)
    km = KMeans(n_clusters=k, n_init="auto").fit(arr)
    centers = km.cluster_centers_.astype(int)
    return [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in centers]


def _critical_css(html_path: Path, out: Path) -> str:
    """
    Use `npx critical` (no --minify flag) to grab above‑the‑fold CSS.
    Silently fallback to "" on failure.
    """
    try:
        subprocess.run(
            [
                "npx",
                "critical",
                "--inline",
                "false",
                "--extract",
                "--width",
                "1280",
                "--height",
                "800",
                "--base",
                str(html_path.parent),
                "--dest",
                str(out),
                str(html_path),
            ],
            check=True,
        )
        return out.read_text(encoding="utf-8")
    except subprocess.CalledProcessError:
        return ""


def scrape(url: str) -> ScrapeBundle:
    job_dir = Path(tempfile.gettempdir()) / f"orchids_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.goto(url, timeout=45_000)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
        except PwTimeout:
            browser.close()
            raise RuntimeError("Playwright navigation timed‑out")

        dom_html = page.content()
        shot_fp = job_dir / "hero.png"
        page.screenshot(path=str(shot_fp), full_page=False)

        parsed = urlparse(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # assets
        css_links = [l.get_attribute("href") for l in page.query_selector_all("link[rel='stylesheet']") if l.get_attribute("href")]
        font_links = [
            l.get_attribute("href")
            for l in page.query_selector_all("link[rel='stylesheet']")
            if l.get_attribute("href") and "fonts.googleapis.com" in l.get_attribute("href")
        ]
        scripts = page.query_selector_all("script")
        script_srcs, inline_scripts = [], []
        for s in scripts:
            src = s.get_attribute("src")
            if src:
                script_srcs.append(src)
            else:
                inline_scripts.append(s.inner_html())

        img_srcs = []
        for img in page.query_selector_all("img"):
            src = img.get_attribute("src")
            if not src:
                continue
            img_srcs.append(src if src.startswith("http") else f"{origin}/{src.lstrip('/')}")

        bg_image_urls = [
            b.strip().replace('url("', "").replace('")', "")
            for b in page.evaluate("""Array.from(document.querySelectorAll('*')).map(e=>getComputedStyle(e).backgroundImage).filter(x=>x&&x!=='none')""")
            if b.startswith("url(")
        ]

        meta_tags = [m.get_attribute("outerHTML") for m in page.query_selector_all("meta")]
        link_icons = [l.get_attribute("outerHTML") for l in page.query_selector_all("link[rel*='icon']")]

        inline_svgs = [s.inner_html() for s in page.query_selector_all("svg")][:20]
        srcset_urls = page.evaluate("""Array.from(document.querySelectorAll('img[srcset]')).map(i=>i.srcset)""")
        picture_sources = page.evaluate("""Array.from(document.querySelectorAll('source[srcset]')).map(s=>s.srcset)""")

        browser.close()

    html_path = job_dir / "page.html"
    html_path.write_text(dom_html, encoding="utf-8")
    critical_css = _critical_css(html_path, job_dir / "critical.css") or "/* no critical css */"
    palette = _extract_palette(Image.open(shot_fp))

    return ScrapeBundle(
        url=url,
        dom_html=dom_html,
        palette=palette,
        screenshot_path=shot_fp,
        critical_css=critical_css,
        css_links=css_links,
        script_srcs=script_srcs,
        inline_scripts=inline_scripts,
        img_srcs=img_srcs,
        bg_image_urls=bg_image_urls,
        meta_tags=meta_tags,
        link_icons=link_icons,
        font_links=font_links,
        inline_svgs=inline_svgs,
        srcset_urls=srcset_urls,
        picture_sources=picture_sources,
        saved_at=time.time(),
    )
