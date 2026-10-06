"""Capture dashboard screenshots for the reports.

    python -m uvicorn server:app --port 8077     # in one terminal
    python docs/capture_screens.py               # in another

Writes docs/shot-*.png. Light theme deliberately: the reports are meant to be
printed, and the dashboard's dark default turns into a page of solid ink.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8077"
WIDTH, HEIGHT = 1500, 940


def grab(page, name: str, card_titled: str | None = None,
         selector: str | None = None) -> Path:
    """Whole viewport, or just the card carrying a given heading.

    Cards are located by their heading text rather than by position: a
    `.card:last-of-type` selector matches on element type, not class, so it
    silently misses as soon as a non-card div follows the last card.
    """
    # A field left focused renders with a selection highlight, which reads as a
    # rendering artefact in a printed report.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    out = HERE / f"shot-{name}.png"
    if selector:
        page.locator(selector).first.screenshot(path=str(out))
    elif card_titled:
        page.locator(".card").filter(has_text=card_titled).first.screenshot(
            path=str(out))
    else:
        page.screenshot(path=str(out))
    print(f"  wrote {out.name}")
    return out


# The operator's own mobile is the default test destination, so it shows up on
# the dashboard and all through the call log. These images go into documents
# that get shared and committed, so the number is replaced with a reserved 555
# one first. Display only - nothing in the database is touched, and the report
# says the screenshots are masked.
MASK_JS = """
(() => {
  const RE = /(\+?1[\s.\-]?)?\(?425\)?[\s.\-]?205[\s.\-]?5800/g;
  const SUB = '(555) 010-0199';
  document.querySelectorAll('input, textarea').forEach((el) => {
    if (RE.test(el.value || '')) { RE.lastIndex = 0; el.value = '+15550100199'; }
  });
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const hits = [];
  while (walk.nextNode()) {
    RE.lastIndex = 0;
    if (RE.test(walk.currentNode.nodeValue || '')) hits.push(walk.currentNode);
  }
  hits.forEach((n) => { RE.lastIndex = 0; n.nodeValue = n.nodeValue.replace(RE, SUB); });
  return hits.length;
})()
"""


def mask_personal_numbers(page) -> None:
    page.evaluate(MASK_JS)


def crop_height(path: Path, pixels: int) -> None:
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        if im.height <= pixels:
            return
        im.crop((0, 0, im.width, pixels)).save(path)
    print(f"  cropped {path.name} to {pixels}px tall")


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            browser = p.chromium.launch()  # bundled build, if one is installed
        ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT},
                                  device_scale_factor=2)  # crisp in print
        page = ctx.new_page()

        # Light theme before the app boots, so nothing flashes dark.
        page.add_init_script(
            "localStorage.setItem('robocall-theme', 'light');")

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)
        mask_personal_numbers(page)
        grab(page, "dashboard")

        page.click('.nav-item[data-view="calls"]')
        page.wait_for_timeout(900)
        mask_personal_numbers(page)
        shot = grab(page, "calllog", selector="#view-calls .card")
        # The log grows without limit, and a full-height image forces a
        # half-empty page in the reports. The newest rows are the interesting
        # ones and they are at the top, so keep the header and the first few.
        crop_height(shot, 530)

        # The scheduling step, with the window fields and the no-end-date
        # warning showing - this is what the brief is actually about.
        page.click('.nav-item[data-view="new"]')
        page.wait_for_timeout(700)
        page.select_option("#c-frequency", "daily")
        page.dispatch_event("#c-frequency", "change")
        page.fill("#c-name", "Spring sale announcement")
        page.fill("#c-message",
                  "Our spring sale starts Friday, with twenty percent off everything "
                  "in store.")
        page.fill("#c-contacts", "+1 617 555 0142, Dana Whitfield\n"
                                 "617-555-0199, Marcus Bell\n(415) 555 0100, Priya Raman")
        page.dispatch_event("#c-contacts", "input")
        page.wait_for_timeout(600)
        grab(page, "schedule", card_titled="When to call")

        # Same card with a window filled in, so the pair reads as before/after.
        page.fill("#c-starts-at", "2026-10-05T10:00")
        page.fill("#c-ends-at", "2026-11-02T10:00")
        page.dispatch_event("#c-ends-at", "input")
        page.wait_for_timeout(500)
        grab(page, "schedule-bounded", card_titled="When to call")

        ctx.close()
        browser.close()
    print("\n  done - rebuild the reports to pick these up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
