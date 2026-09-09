"""Capture two README report views in both themes from a real stored-run HTML file.

Run with an ephemeral docs dependency (no change to runtime dependencies):
    uv run --with playwright python scripts/capture-readme.py <report.html> --item tjv0-085

Install the browser first if needed:
    uv run --with playwright python -m playwright install chromium

Only browser navigation, report controls, and screenshot crops are used; no report data or CSS
is changed. See docs/assets/README.md for the committed screenshots' provenance.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from playwright.sync_api import Locator, Page, expect, sync_playwright


def capture_region(page: Page, first: Locator, last: Locator, destination: Path) -> None:
    """Crop consecutive report sections with a little page background around them."""
    page.evaluate("window.scrollTo(0, 0)")
    start = first.bounding_box()
    end = last.bounding_box()
    if start is None or end is None:
        raise RuntimeError("Report sections must be visible before capture")
    page.screenshot(
        path=str(destination),
        full_page=True,
        clip={
            "x": math.floor(start["x"] - 24),
            "y": math.floor(start["y"] - 24),
            "width": math.ceil(start["width"] + 48),
            "height": math.ceil(end["y"] + end["height"] - start["y"] + 48),
        },
        animations="disabled",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--item", required=True, help="Exact item ID to expand in the detail shot")
    parser.add_argument("--out", type=Path, default=Path("docs/assets"))
    args = parser.parse_args()
    report_path = args.report.resolve(strict=True)
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for theme in ("light", "dark"):
                page = browser.new_page(
                    viewport={"width": 1280, "height": 1000},
                    device_scale_factor=2,
                    color_scheme=theme,
                    reduced_motion="reduce",
                )
                errors: list[str] = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                # Reports are self-contained: requests cannot call models or load remote assets.
                page.route("http://**/*", lambda route: route.abort())
                page.route("https://**/*", lambda route: route.abort())
                page.goto(report_path.as_uri())
                expect(page.locator("#readout .card").first).to_be_visible()
                page.evaluate("document.fonts.ready")
                headline = page.locator("section").filter(
                    has=page.get_by_role("heading", name="Suite score per model", exact=True)
                )
                anatomy = page.locator("section").filter(
                    has=page.get_by_role(
                        "heading", name="Three different kinds of zero", exact=True
                    )
                )
                capture_region(page, headline, anatomy, args.out / f"report-overview-{theme}.png")

                # Use the actual search and expansion controls rather than changing the DOM.
                page.get_by_role("searchbox", name="Search item prompts").fill(args.item)
                item = page.locator("#ledger details")
                expect(item).to_have_count(1)
                expect(item.locator(".item-id")).to_have_text(args.item)
                item.locator("summary").click()
                expect(item.locator(".item-body")).to_be_visible()
                ledger = page.locator("section").filter(has=page.locator("#ledger"))
                capture_region(page, ledger, ledger, args.out / f"report-item-{theme}.png")
                if errors:
                    raise RuntimeError("Browser errors: " + "; ".join(errors))
                print(f"Captured {theme}: overview and {args.item}; no browser errors")
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
