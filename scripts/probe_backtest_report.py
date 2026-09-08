#!/usr/bin/env python3
"""Probe a generated self-contained report.html in headless Chromium."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PAGE_SIZE = 25


def _record_count(page: Any) -> int:
    text = page.locator("#trace-page").inner_text()
    match = re.search(r"· ([0-9]+) records$", text)
    if match is None:
        raise RuntimeError(f"unrecognized trace pager: {text!r}")
    return int(match.group(1))


def _assert_render_bound(page: Any) -> int:
    count = page.locator("#traces details").count()
    if count > PAGE_SIZE:
        raise RuntimeError(f"rendered evaluation bound exceeded: {count}")
    return count


def _timed_action(page: Any, action: Callable[[], object]) -> tuple[float, int]:
    page.evaluate("window.__decodedEvaluationCount = 0")
    started = time.perf_counter()
    action()
    page.evaluate("async () => await renderTraces()")
    elapsed = time.perf_counter() - started
    decoded = int(page.evaluate("window.__decodedEvaluationCount"))
    if decoded > PAGE_SIZE:
        raise RuntimeError(f"decoded evaluation bound exceeded: {decoded}")
    return elapsed, decoded


def probe(report: Path, *, chromium_executable: Path | None = None) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "Playwright is required; run `uv sync --locked --all-groups`, then use "
            "`uv run --locked python scripts/probe_backtest_report.py ...`"
        ) from error

    console_errors: list[str] = []
    timings: dict[str, float] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(chromium_executable) if chromium_executable else None,
        )
        page = browser.new_page()
        page.add_init_script(
            """
            window.__decodedEvaluationCount = 0;
            const originalJsonParse = JSON.parse;
            JSON.parse = function (...args) {
              const value = originalJsonParse.apply(this, args);
              if (value && !Array.isArray(value) && value.decision && value.trace) {
                window.__decodedEvaluationCount += 1;
              } else if (
                Array.isArray(value) &&
                value.length &&
                value.every(item => item && item.decision && item.trace)
              ) {
                window.__decodedEvaluationCount += value.length;
              }
              return value;
            };
            """
        )
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        started = time.perf_counter()
        page.goto(report.resolve().as_uri(), wait_until="domcontentloaded", timeout=300_000)
        page.locator("html[data-report-ready='true']").wait_for(timeout=300_000)
        timings["load"] = time.perf_counter() - started

        compatibility_hidden = page.locator("#compatibility").get_attribute("hidden") is not None
        if not compatibility_hidden:
            raise RuntimeError(page.locator("#compatibility").inner_text())
        payload_nodes = page.locator("#backtest-evidence").count()
        total = _record_count(page)
        if total < 1:
            raise RuntimeError("report did not expose any evaluations")
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        element_counts = {"initial": page.locator("*").count()}
        rendered_counts = {"initial": _assert_render_bound(page)}
        decoded_counts = {"initial": int(page.evaluate("window.__decodedEvaluationCount"))}
        if decoded_counts["initial"] > PAGE_SIZE:
            raise RuntimeError(
                f"initial decoded evaluation bound exceeded: {decoded_counts['initial']}"
            )
        access: dict[str, str] = {"first": page.locator("#traces summary").first.inner_text()}

        middle_page = (total_pages + 1) // 2
        timings["middle_page"], decoded_counts["middle"] = _timed_action(
            page,
            lambda: (
                page.locator("#trace-page-input").fill(str(middle_page)),
                page.locator("#trace-go").click(),
            ),
        )
        access["middle"] = page.locator("#traces summary").first.inner_text()
        rendered_counts["middle"] = _assert_render_bound(page)

        timings["last_page"], decoded_counts["last"] = _timed_action(
            page, lambda: page.locator("#trace-last").click()
        )
        access["last"] = page.locator("#traces summary").last.inner_text()
        rendered_counts["last"] = _assert_render_bound(page)

        timings["upper_clamp"], decoded_counts["upper_clamp"] = _timed_action(
            page,
            lambda: (
                page.locator("#trace-page-input").fill(str(total_pages + 100)),
                page.locator("#trace-go").click(),
            ),
        )
        if page.locator("#trace-page-input").input_value() != str(total_pages):
            raise RuntimeError("upper page navigation did not clamp")

        def clear_filters() -> None:
            page.evaluate(
                """async () => {
                  for (const id of [
                    'instrument-filter', 'evaluation-filter', 'failed-step-filter', 'reason-filter'
                  ]) document.getElementById(id).value = '';
                  document.getElementById('direction-filter').value = '';
                  document.getElementById('status-filter').value = '';
                  pages.trace = 0;
                  await renderTraces();
                }"""
            )

        filters: dict[str, int] = {}
        filter_actions: tuple[tuple[str, Callable[[], None]], ...] = (
            ("instrument", lambda: page.locator("#instrument-filter").fill("ETH-USDT-PERP")),
            ("direction", lambda: page.locator("#direction-filter").select_option("SHORT")),
            ("disposition", lambda: page.locator("#status-filter").select_option("UNAVAILABLE")),
            ("reason", lambda: page.locator("#reason-filter").fill("SYNTHETIC_UNAVAILABLE")),
            (
                "first_rejection",
                lambda: page.locator("#failed-step-filter").fill("synthetic.signal-0"),
            ),
        )
        for name, action in filter_actions:
            clear_filters()
            timings[f"filter_{name}"], decoded_counts[f"filter_{name}"] = _timed_action(
                page, action
            )
            filters[name] = _record_count(page)
            if filters[name] < 1:
                raise RuntimeError(f"{name} filter exposed no evaluations")
            rendered_counts[f"filter_{name}"] = _assert_render_bound(page)

        clear_filters()
        first_evaluation = access["first"].split(" · ")[1]
        timings["filter_evaluation"], decoded_counts["filter_evaluation"] = _timed_action(
            page, lambda: page.locator("#evaluation-filter").fill(first_evaluation)
        )
        filters["evaluation"] = _record_count(page)
        if filters["evaluation"] < 1:
            raise RuntimeError("evaluation filter exposed no evaluations")
        rendered_counts["filter_evaluation"] = _assert_render_bound(page)

        clear_filters()
        filters["clear"] = _record_count(page)
        if filters["clear"] != total:
            raise RuntimeError("clearing filters did not restore every evaluation")
        rendered_counts["clear"] = _assert_render_bound(page)
        element_counts["maximum_observed"] = max(
            element_counts["initial"], page.locator("*").count()
        )
        if page.locator("#backtest-evidence").count() != payload_nodes:
            raise RuntimeError("embedded payload node count changed during interaction")
        browser.close()

    if console_errors:
        raise RuntimeError(f"browser console errors: {console_errors}")
    return {
        "schema_version": 1,
        "report": str(report.resolve()),
        "chromium_executable": (
            str(chromium_executable.resolve()) if chromium_executable is not None else None
        ),
        "evaluation_count": total,
        "page_size": PAGE_SIZE,
        "payload_node_count": payload_nodes,
        "compatibility_message_hidden": compatibility_hidden,
        "rendered_evaluation_counts": rendered_counts,
        "decoded_evaluation_counts": decoded_counts,
        "element_counts": element_counts,
        "access": access,
        "filters": filters,
        "timings_seconds": timings,
        "console_errors": console_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="path to the generated report.html")
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument(
        "--chromium-executable",
        type=Path,
        help="use an existing Chromium executable instead of Playwright's managed browser",
    )
    args = parser.parse_args()
    result = probe(args.report, chromium_executable=args.chromium_executable)
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    args.result_json.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
