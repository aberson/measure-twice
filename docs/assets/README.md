# README visuals

The diagrams follow [skill-mesh's workflow style](https://github.com/aberson/skill-mesh):
monospaced labels, rounded panels, blue workflow arrows, muted supporting text, and separate light
and dark SVGs selected by GitHub's `<picture>` rendering. They describe the implemented model-sweep
and claim-ledger workflows; planned coding-agent execution is not shown as a working command.

## Screenshot provenance

`report-overview-{light,dark}.png` and `report-item-{light,dark}.png` are Chromium/Playwright
captures of the application's unmodified HTML report, regenerated from an existing local run.
They are cropped report views, not mockups. Browser theme emulation selects the report's own
light/dark CSS; search and item expansion use its real controls.

| Field | Value |
|---|---|
| Source run | `run_20260830T071944Z_948385` |
| Run start | `2026-08-30T07:19:44Z` |
| Suite | `tier-judging-v0`, 100 authored verdict items |
| Item hash | `070bfe9faf2e308491dde20033edd74370f001d1ebe0275fcb9854af14df9973` |
| Roster | `haiku`, `sonnet` |
| Sampling | One sample per model per item; 200 stored cells |
| Integrity check | All 200 verdict-scored cells reproduced from stored raw responses |
| Detail selection | Search `tjv0-085`, then expand its row |
| Capture viewport | 1280 × 1000 CSS pixels; device scale factor 2 |

This was an exploratory instrument check. The screenshots do not establish a validated routing
recommendation or general model ranking. The report retains its constant-answer baseline, parsing
failure breakdown, and scorer explanations. No inference calls were made to create these images;
no claim status was changed. The source run is gitignored local data and is not shipped with the
screenshots.

## Recapture

From a checkout with that run present:

```powershell
uv run mt report run_20260830T071944Z_948385 --html
uv run --with playwright python -m playwright install chromium
uv run --with playwright python scripts/capture-readme.py data/reports/run_20260830T071944Z_948385.html --item tjv0-085
```

The capture helper uses Playwright's [screenshot](https://playwright.dev/python/docs/screenshots)
and [theme emulation](https://playwright.dev/python/docs/emulation) APIs. Playwright is an ephemeral
documentation dependency, not a package dependency. To illustrate a different run, pass its report
path and an item ID, then update this provenance and the README captions together.
