# backend/scraper.py

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import tempfile
import json
import uuid
import time
import re

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
    saved_at: float

    css_links: list[str]
    font_links: list[str]
    meta_tags: list[str]
    link_icons: list[str]
    script_tags: list[str]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "dom_html": self.dom_html,
            "palette": self.palette,
            "screenshot_path": str(self.screenshot_path),
            "saved_at": self.saved_at,
            "css_links": self.css_links,
            "font_links": self.font_links,
            "meta_tags": self.meta_tags,
            "link_icons": self.link_icons,
            "script_tags": self.script_tags,
        }


def _extract_palette(img: Image.Image, k: int = 5) -> list[str]:
    arr = np.array(img).reshape(-1, 3)
    km = KMeans(n_clusters=k, n_init="auto").fit(arr)
    centers = km.cluster_centers_.astype(int)
    return [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in centers]


def scrape(url: str) -> ScrapeBundle:
    job_dir = Path(tempfile.gettempdir()) / f"orchids_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        try:
            page.goto(url, timeout=45_000)
            page.wait_for_load_state("networkidle")
            dom_html = page.content()
            shot_fp = job_dir / "hero.png"
            page.screenshot(path=str(shot_fp), full_page=False)
        except PwTimeout:
            browser.close()
            raise RuntimeError("Page load timed-out")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    palette = _extract_palette(Image.open(shot_fp))

    css_links = re.findall(
        r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
        dom_html,
        flags=re.I
    )

    font_links: list[str] = []  # extend if needed

    meta_tags = re.findall(r'(<meta\b[^>]*>)', dom_html, flags=re.I)

    link_icons = re.findall(
        r'<link[^>]+rel=["\']icon["\'][^>]*href=["\']([^"\']+)["\']',
        dom_html,
        flags=re.I
    )

    script_tags = re.findall(
        r'(<script\b[^>]*?>.*?</script>)',
        dom_html,
        flags=re.I | re.S
    )

    return ScrapeBundle(
        url=url,
        dom_html=dom_html,
        palette=palette,
        screenshot_path=shot_fp,
        saved_at=time.time(),
        css_links=css_links,
        font_links=font_links,
        meta_tags=meta_tags,
        link_icons=link_icons,
        script_tags=script_tags,
    )
