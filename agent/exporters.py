"""Write results to CSV (opens cleanly in Excel), JSON and a Word document."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path

from .models import FIELDNAMES, Conference

log = logging.getLogger(__name__)

# Conference names and URLs are typed by third parties; a leading '=' etc. would run as a
# formula when the CSV is opened in a spreadsheet, so such cells are prefixed with a quote.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def export(conferences: list[Conference], output_dir: Path, stem: str, formats: set[str], meta: dict) -> list[Path]:
    """Write each requested format and return the files written.

    A format that fails (folder missing, disk full, file locked, ...) is reported and skipped, so the
    others are still saved; a closing line names everything that wasn't.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("Could not create the output folder %s: %s", output_dir, exc)
    written: list[Path] = []
    missing: list[str] = []
    for fmt, write in _WRITERS.items():
        if fmt not in formats:
            continue
        path = output_dir / f"{stem}.{fmt}"
        try:
            saved = write(conferences, path, meta)
        except (OSError, ValueError) as exc:
            log.error("Could not write %s: %s", path.name, exc)
            saved = False
        if saved:
            written.append(path)
        else:
            missing.append(fmt)
    if missing:
        log.error("Not saved: %s file(s); see the messages above.", ", ".join(missing))
    return written


def _write_csv(conferences: list[Conference], path: Path, meta: dict) -> bool:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for conference in conferences:
            writer.writerow({key: csv_safe(value) for key, value in asdict(conference).items()})
    _write_keywords_expanded(conferences, path)
    return True


def _write_keywords_expanded(conferences: list[Conference], base_path: Path) -> None:
    """Write a companion CSV with one row per (conference, keyword) pair."""
    kw_path = base_path.parent / (base_path.stem + "_by_keyword.csv")
    fieldnames_kw = FIELDNAMES + ["keyword"]
    try:
        with kw_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames_kw)
            writer.writeheader()
            for conference in conferences:
                base_row = {key: csv_safe(value) for key, value in asdict(conference).items()}
                kws = [kw.strip() for kw in conference.keywords.split(",") if kw.strip()]
                if kws:
                    for kw in kws:
                        writer.writerow({**base_row, "keyword": csv_safe(kw)})
                else:
                    writer.writerow({**base_row, "keyword": ""})
        log.info("Keywords-expanded CSV written: %s", kw_path.name)
    except OSError as exc:
        log.warning("Could not write keywords CSV %s: %s", kw_path, exc)


def _write_json(conferences: list[Conference], path: Path, meta: dict) -> bool:
    payload = {"meta": meta, "conferences": [asdict(c) for c in conferences]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def _write_docx(conferences: list[Conference], path: Path, meta: dict) -> bool:
    try:
        from .word_export import write_docx
    except ImportError:
        log.warning("Word file skipped: install python-docx first (pip install python-docx).")
        return False
    log.info("Writing the Word document (%d rows)...", len(conferences))
    write_docx(conferences, path, meta)
    return True


_WRITERS = {"csv": _write_csv, "json": _write_json, "docx": _write_docx}
FORMATS = tuple(_WRITERS)


def export_files(conferences, output_dir, stem: str, formats: set[str], meta: dict) -> list[Path]:
    """Convenience wrapper used by the LLM agent."""
    return export(conferences, Path(output_dir), stem, formats, meta)
