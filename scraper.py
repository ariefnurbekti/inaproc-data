"""Fetch INAPROC RUP or Realisasi data, export CSV, and sync to Supabase."""

from __future__ import annotations

import argparse
import base64
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
import json
import os
import threading
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_SOURCE_URL = (
    "https://data.inaproc.id/rup?tahun=2026&jenis_klpd=4&instansi=D276"
    "&satker=1.02.0.00.0.00.01.0000&sumber_dana=APBDP&limit=20"
    "&sort_by=total_nilai&sort_dir=desc"
)
DEFAULT_REALISASI_SOURCE_URL = (
    "https://data.inaproc.id/realisasi?tahun=2026&jenis=4&instansi=D276"
    "&satker=1.02.0.00.0.00.01.0000&sumber_dana=APBDP"
    "&sort_by=nilai_umk&sort_dir=desc"
)
API_PATH = "/dashboard-api/rup/table"
REALISASI_API_PATH = "/dashboard-api/realisasi/table"
API_HOST = "data.inaproc.id"
PROFILE_API_PATH = "/dashboard-api/profil-pengadaan"
RUP_FILTER_API_PATH = "/dashboard-api/rup/filters"
PAGE_SIZE = 100
SUPABASE_BATCH_SIZE = 500
DEFAULT_EXPORT_PATH = Path("exports") / "inaproc_rup.csv"
DEFAULT_REALISASI_EXPORT_PATH = Path("exports") / "inaproc_realisasi.csv"
DEFAULT_COMPARISON_EXPORT_PATH = Path("exports") / "inaproc_status_pelaksanaan.csv"
DEFAULT_FILTER_REFERENCE_PATH = Path("reference") / "inaproc_filter_codes_2026.json"
AVAILABLE_YEARS = ("2022", "2023", "2024", "2025", "2026")
_FILTER_REQUEST_LOCK = threading.Lock()
_LAST_FILTER_REQUEST_AT = 0.0
_FILTER_REQUEST_INTERVAL_SECONDS = 0.2
FILTER_KEYS = {
    "tahun",
    "jenis",
    "jenis_klpd",
    "instansi",
    "eselon",
    "satker",
    "sumber",
    "metode_pengadaan",
    "sumber_dana",
    "status_paket",
    "search_kode",
    "search_penyedia",
    "kd_penyedia",
    "filter_metode",
    "filter_jenis",
    "filter_pdn",
    "view_table",
    "search_rup",
    "search_paket",
    "search_q",
    "sort_by",
    "sort_dir",
}
MULTI_VALUE_FILTERS = {
    "jenis",
    "jenis_klpd",
    "instansi",
    "sumber_dana",
    "status_paket",
}


def build_filter_combinations(
    filter_groups: list[list[list[tuple[str, str]]]],
) -> list[list[tuple[str, str]]]:
    """Build one filter set per Cartesian combination of selected values."""
    active_groups = [group for group in filter_groups if group]
    if not active_groups:
        return [[]]
    return [
        [
            filter_pair
            for selected_option in combination
            for filter_pair in selected_option
        ]
        for combination in product(*active_groups)
    ]


def build_sequential_dataset_plan(
    combinations: list[list[tuple[str, str]]],
    *,
    search_value: str = "",
    additional_filters: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str, int]]:
    """Build serial RUP-then-Realisasi jobs for every filter combination."""
    dataset_sources = (
        ("rup", "https://data.inaproc.id/rup", "search_rup"),
        ("realisasi", "https://data.inaproc.id/realisasi", "search_paket"),
    )
    plan = []
    for combination_index, combination in enumerate(combinations, start=1):
        for dataset, source_url, search_key in dataset_sources:
            filter_pairs = list(combination)
            if search_value:
                filter_pairs.append((search_key, search_value))
            filter_pairs.extend(additional_filters or [])
            filters = [f"{key}={value}" for key, value in filter_pairs]
            plan.append(
                (
                    dataset,
                    apply_filter_overrides(source_url, filters),
                    combination_index,
                )
            )
    return plan


def apply_filter_overrides(source_url: str, filters: list[str]) -> str:
    """Apply validated KEY=VALUE filters, preserving repeated multi-selects."""
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise ValueError(f"Source URL must use https://{API_HOST}")

    overrides: dict[str, list[str]] = {}
    for filter_value in filters:
        key, separator, value = filter_value.partition("=")
        if not separator or key not in FILTER_KEYS:
            available = ", ".join(sorted(FILTER_KEYS))
            raise ValueError(
                f"Invalid filter {filter_value!r}; use KEY=VALUE with one of: "
                f"{available}"
            )
        normalized_key = key
        if parsed.path == "/rup" and key == "jenis":
            normalized_key = "jenis_klpd"
        elif parsed.path == "/realisasi" and key == "jenis_klpd":
            normalized_key = "jenis"
        if normalized_key in MULTI_VALUE_FILTERS:
            overrides.setdefault(normalized_key, []).append(value)
        else:
            overrides[normalized_key] = [value]

    query_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in overrides
    ]
    query_params.extend(
        (key, value)
        for key, values in overrides.items()
        for value in values
    )
    query = urlencode(query_params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def build_api_url(
    source_url: str,
    *,
    page: int = 1,
    cursor: str = "",
    page_size: int = PAGE_SIZE,
) -> str:
    """Map a public RUP page URL to a JSON table endpoint page."""
    return _build_table_url(
        source_url,
        API_PATH,
        page=page,
        cursor=cursor,
        page_size=page_size,
        filter_aliases={"jenis": "jenis_klpd"},
    )


def build_realisasi_api_url(
    source_url: str,
    *,
    page: int = 1,
    cursor: str = "",
    page_size: int = PAGE_SIZE,
) -> str:
    """Map a public Realisasi page URL to a JSON table endpoint page."""
    return _build_table_url(
        source_url,
        REALISASI_API_PATH,
        page=page,
        cursor=cursor,
        page_size=page_size,
        filter_aliases={"jenis_klpd": "jenis"},
    )


def _build_table_url(
    source_url: str,
    endpoint_path: str,
    *,
    page: int,
    cursor: str,
    page_size: int,
    filter_aliases: dict[str, str],
) -> str:
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise ValueError(f"Source URL must use https://{API_HOST}")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Source URL must not include credentials or a custom port")
    if not 1 <= page_size <= PAGE_SIZE:
        raise ValueError(f"Page size must be between 1 and {PAGE_SIZE}")
    if page < 1:
        raise ValueError("Page number must be at least 1")
    if page > 1 and not cursor:
        raise ValueError("A cursor is required for pages after the first")

    query_params = [
        (filter_aliases.get(key, key), value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"limit", "page", "cursor"}
    ]
    query_params.append(("limit", str(page_size)))
    if page > 1:
        query_params.extend((("page", str(page)), ("cursor", cursor)))
    query = urlencode(query_params)
    return urlunsplit(("https", API_HOST, endpoint_path, query, ""))


def build_signed_headers(timestamp_ms: str | None = None) -> dict[str, str]:
    """Create the request headers expected by the INAPROC dashboard API."""
    timestamp = timestamp_ms or str(int(time.time() * 1000))
    secret = base64.b64encode(timestamp[::-1].encode("ascii")).decode("ascii")
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; InaprocRupScraper/1.0)",
        "x-request-time": timestamp,
        "x-secret": secret,
    }


def _fetch_filter_items(path: str, query: dict[str, str] | None = None) -> list[dict]:
    query_string = urlencode(query or {})
    url = f"https://{API_HOST}{path}"
    if query_string:
        url = f"{url}?{query_string}"
    request = Request(url, headers=build_signed_headers())
    payload = _fetch_payload(request, timeout_seconds=30)
    items = payload.get("items")
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("kode"), str)
        and isinstance(item.get("nama"), str)
        for item in items
    ):
        raise RuntimeError(f"INAPROC returned malformed filter options for {url}")
    return items


def fetch_filter_reference(year: str = "2026", workers: int = 4) -> dict:
    """Fetch distinct filter codes and dependent options for one budget year."""
    if not year.isdigit() or len(year) != 4:
        raise ValueError("Reference year must be a four-digit year")
    if workers < 1 or workers > 8:
        raise ValueError("Reference workers must be between 1 and 8")

    kinds = _fetch_filter_items(f"{PROFILE_API_PATH}/jenis")
    instansi_by_jenis: dict[str, list[dict]] = {}
    instances: list[tuple[str, dict]] = []
    for kind in kinds:
        kind_code = kind["kode"]
        instansi = _fetch_filter_items(
            f"{PROFILE_API_PATH}/instansi",
            {"tahun": year, "jenis": kind_code},
        )
        instansi_by_jenis[kind_code] = instansi
        instances.extend((kind_code, item) for item in instansi)

    tasks: dict = {}
    eselon_by_instansi: dict[str, list[dict]] = {}
    satker_by_instansi: dict[str, list[dict]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for kind_code, instansi in instances:
            key = f"{kind_code}:{instansi['kode']}"
            query = {
                "tahun": year,
                "jenis": kind_code,
                "instansi": instansi["kode"],
            }
            tasks[
                executor.submit(
                    _fetch_filter_items, f"{PROFILE_API_PATH}/satker", query
                )
            ] = ("satker", key, instansi["nama"])
            if kind_code in {"1", "2"}:
                tasks[
                    executor.submit(
                        _fetch_filter_items, f"{PROFILE_API_PATH}/eselon", query
                    )
                ] = ("eselon", key, instansi["nama"])

        for index, future in enumerate(as_completed(tasks), start=1):
            category, key, instansi_name = tasks[future]
            if index % 100 == 0 or index == len(tasks):
                print(f"Daftar turunan diproses: {index}/{len(tasks)}.")
            try:
                items = future.result()
            except RuntimeError as exc:
                errors.append(f"{category} {key} ({instansi_name}): {exc}")
                continue
            target = satker_by_instansi if category == "satker" else eselon_by_instansi
            target[key] = items

    if errors:
        preview = "\n".join(errors[:10])
        remaining = len(errors) - min(len(errors), 10)
        suffix = f"\n... and {remaining} more errors" if remaining else ""
        raise RuntimeError(
            f"Could not retrieve {len(errors)} dependent filter lists:\n"
            f"{preview}{suffix}"
        )

    cara_pengadaan = _fetch_filter_items(f"{RUP_FILTER_API_PATH}/cara-pengadaan")
    sumber_dana = _fetch_filter_items(f"{RUP_FILTER_API_PATH}/sumber-dana")

    def unique_items(groups: list[list[dict]]) -> list[dict]:
        values: dict[str, dict] = {}
        for group in groups:
            for item in group:
                values.setdefault(item["kode"], item)
        return list(values.values())

    return {
        "source": f"https://{API_HOST}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "year_options": list(AVAILABLE_YEARS),
        "filters": {
            "jenis_klpd": kinds,
            "instansi_by_jenis": instansi_by_jenis,
            "eselon_by_instansi": eselon_by_instansi,
            "satker_by_instansi": satker_by_instansi,
            "cara_pengadaan": cara_pengadaan,
            "sumber_dana": sumber_dana,
        },
        "unique_codes": {
            "jenis_klpd": unique_items([kinds]),
            "instansi": unique_items(list(instansi_by_jenis.values())),
            "eselon": unique_items(list(eselon_by_instansi.values())),
            "satker": unique_items(list(satker_by_instansi.values())),
            "cara_pengadaan": unique_items([cara_pengadaan]),
            "sumber_dana": unique_items([sumber_dana]),
        },
    }


def fetch_filter_options(
    category: str,
    *,
    year: str | None = None,
    kind_code: str | None = None,
    instansi_code: str | None = None,
) -> list[dict]:
    """Fetch one filter option list for the interactive UI."""
    if category == "jenis_klpd":
        return _fetch_filter_items(f"{PROFILE_API_PATH}/jenis")
    if category == "instansi":
        if not year or not kind_code:
            raise ValueError("Year and kind are required to fetch instansi options")
        return _fetch_filter_items(
            f"{PROFILE_API_PATH}/instansi",
            {"tahun": year, "jenis": kind_code},
        )
    if category in {"satker", "eselon"}:
        if not year or not kind_code or not instansi_code:
            raise ValueError(
                "Year, kind, and instansi are required for dependent options"
            )
        return _fetch_filter_items(
            f"{PROFILE_API_PATH}/{category}",
            {"tahun": year, "jenis": kind_code, "instansi": instansi_code},
        )
    if category in {"cara_pengadaan", "sumber_dana"}:
        return _fetch_filter_items(
            f"{RUP_FILTER_API_PATH}/"
            f"{'cara-pengadaan' if category == 'cara_pengadaan' else 'sumber-dana'}"
        )
    raise ValueError(f"Unknown filter option category: {category}")


def export_filter_reference(
    reference: dict, output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def fetch_rows(
    source_url: str,
    timeout_seconds: int = 30,
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Fetch every cursor-paginated table row for the source URL's filters."""
    return _fetch_table_rows(source_url, build_api_url, timeout_seconds, page_size)


def fetch_realisasi_rows(
    source_url: str,
    timeout_seconds: int = 30,
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Fetch every cursor-paginated Realisasi row, including status and provider."""
    return _fetch_table_rows(
        source_url, build_realisasi_api_url, timeout_seconds, page_size
    )


def _fetch_table_rows(
    source_url: str,
    url_builder: Callable[..., str],
    timeout_seconds: int,
    page_size: int,
) -> list[dict]:
    rows: list[dict] = []
    cursor = ""
    page = 1
    seen_cursors: set[str] = set()

    while True:
        request = Request(
            url_builder(
                source_url, page=page, cursor=cursor, page_size=page_size
            ),
            headers=build_signed_headers(),
        )
        payload = _fetch_payload(request, timeout_seconds)
        page_rows = payload.get("tableRows")
        pagination = payload.get("tablePagination")
        if not isinstance(page_rows, list) or not all(
            isinstance(row, dict) for row in page_rows
        ):
            raise RuntimeError(
                f"INAPROC API returned a malformed tableRows list on page {page}"
            )
        if not isinstance(pagination, dict):
            raise RuntimeError(
                f"INAPROC API response is missing tablePagination on page {page}"
            )
        has_more = pagination.get("hasMore")
        next_cursor = pagination.get("nextCursor")
        if not isinstance(has_more, bool) or not isinstance(next_cursor, str):
            raise RuntimeError(
                f"INAPROC API returned malformed pagination on page {page}"
            )

        rows.extend(page_rows)
        print(f"Halaman {page}: {len(page_rows)} baris (total {len(rows)}).")
        if not has_more:
            return rows
        if not next_cursor:
            raise RuntimeError(
                f"INAPROC API says more rows exist but returned no cursor on page {page}"
            )
        if next_cursor in seen_cursors:
            raise RuntimeError(
                f"INAPROC API repeated a pagination cursor on page {page}"
            )
        if not page_rows:
            raise RuntimeError(
                f"INAPROC API returned an empty page while more rows exist (page {page})"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        page += 1


def _fetch_payload(request: Request, timeout_seconds: int) -> dict:
    for attempt in range(5):
        if (
            request.full_url.startswith(f"https://{API_HOST}{PROFILE_API_PATH}")
            or request.full_url.startswith(f"https://{API_HOST}{RUP_FILTER_API_PATH}")
        ):
            _throttle_filter_request()
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                message = f"INAPROC API returned HTTP {exc.code}"
                if details:
                    message = f"{message}: {details}"
                raise RuntimeError(message) from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except ValueError:
                delay = 2**attempt
            time.sleep(min(max(delay, 1.0), 30.0))
        except URLError as exc:
            raise RuntimeError(f"Could not reach the INAPROC API: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("INAPROC API returned invalid JSON") from exc
    else:
        raise RuntimeError("INAPROC API request failed after retries")

    if not isinstance(payload, dict):
        raise RuntimeError("INAPROC API returned an invalid response")
    return payload


def _throttle_filter_request() -> None:
    global _LAST_FILTER_REQUEST_AT
    with _FILTER_REQUEST_LOCK:
        wait = (
            _FILTER_REQUEST_INTERVAL_SECONDS
            - (time.monotonic() - _LAST_FILTER_REQUEST_AT)
        )
        if wait > 0:
            time.sleep(wait)
        _LAST_FILTER_REQUEST_AT = time.monotonic()


def prepare_records(
    rows: list[dict], source_url: str, scraped_at: str | None = None
) -> list[dict]:
    """Shape API rows for the public.inaproc_rup Supabase table."""
    timestamp = scraped_at or datetime.now(timezone.utc).isoformat()
    records = []
    for row in rows:
        rup_code = row.get("kode_rup")
        if rup_code is None or isinstance(rup_code, bool) or str(rup_code).strip() == "":
            raise ValueError("INAPROC returned a row without a valid kode_rup")
        records.append(
            {
                "kode_rup": str(rup_code),
                "data": row,
                "source_url": source_url,
                "scraped_at": timestamp,
            }
        )
    return records


def prepare_realisasi_records(
    rows: list[dict], source_url: str, scraped_at: str | None = None
) -> list[dict]:
    """Shape Realisasi rows for public.inaproc_realisasi."""
    timestamp = scraped_at or datetime.now(timezone.utc).isoformat()
    records = []
    for row in rows:
        package_code = row.get("kode_paket")
        if (
            package_code is None
            or isinstance(package_code, bool)
            or str(package_code).strip() == ""
        ):
            raise ValueError("INAPROC returned a Realisasi row without kode_paket")
        records.append(
            {
                "kode_paket": str(package_code),
                "kode_rup": str(row["kode_rup"]) if row.get("kode_rup") else None,
                "data": row,
                "source_url": source_url,
                "scraped_at": timestamp,
            }
        )
    return records


def build_execution_status_report(
    rup_rows: list[dict], realization_rows: list[dict]
) -> list[dict]:
    """Compare planned RUP packages with recorded Realisasi package rows."""
    by_rup_code: dict[str, list[dict]] = {}
    for row in realization_rows:
        rup_code = row.get("kode_rup")
        if rup_code is not None and str(rup_code).strip():
            by_rup_code.setdefault(str(rup_code), []).append(row)

    report = []
    for rup_row in rup_rows:
        rup_code = rup_row.get("kode_rup")
        matches = by_rup_code.get(str(rup_code), []) if rup_code is not None else []

        def joined_values(key: str) -> str:
            values = dict.fromkeys(
                str(row[key]).strip()
                for row in matches
                if row.get(key) is not None and str(row[key]).strip()
            )
            return "; ".join(values)

        report.append(
            {
                **rup_row,
                "status_pelaksanaan": (
                    "Sudah klik" if matches else "Belum klik"
                ),
                "jumlah_paket_realisasi": len(matches),
                "kode_paket_realisasi": joined_values("kode_paket"),
                "status_paket_realisasi": joined_values("status_paket"),
                "tahapan_pengadaan_realisasi": joined_values("tahapan_pengadaan"),
                "nama_penyedia_realisasi": joined_values("nama_penyedia"),
            }
        )
    return report


def export_rows_csv(rows: list[dict], output_path: str | Path) -> Path:
    """Write fetched API rows to a spreadsheet-friendly UTF-8 CSV file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        if not columns:
            return path
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )
    return path


def read_rows_csv(input_path: str | Path, code_column: str) -> list[dict]:
    """Read exported INAPROC rows from CSV, restoring JSON object/list fields."""
    path = Path(input_path)
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None or code_column not in reader.fieldnames:
            raise ValueError(
                f"CSV must have a {code_column!r} column: {path}"
            )

        rows = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(
                    f"CSV row {row_number} has more values than its header: {path}"
                )
            normalized_row = {}
            for key, value in row.items():
                if value is not None and value.startswith(("{", "[")):
                    try:
                        parsed_value = json.loads(value)
                    except json.JSONDecodeError:
                        parsed_value = value
                    if isinstance(parsed_value, (dict, list)):
                        value = parsed_value
                normalized_row[key] = value
            rows.append(normalized_row)
    return rows


def sync_records(client, dataset: str, records: list[dict]) -> int:
    """Upsert prepared records to the matching Supabase dataset table."""
    table, conflict_key = {
        "rup": ("inaproc_rup", "kode_rup"),
        "realisasi": ("inaproc_realisasi", "kode_paket"),
    }[dataset]
    unique_records = {
        record[conflict_key]: record for record in records
    }
    deduplicated_records = list(unique_records.values())
    for start in range(0, len(deduplicated_records), SUPABASE_BATCH_SIZE):
        batch = deduplicated_records[start : start + SUPABASE_BATCH_SIZE]
        client.table(table).upsert(batch, on_conflict=conflict_key).execute()
    return len(deduplicated_records)


def test_supabase_connection(supabase_url: str, supabase_key: str) -> list[str]:
    """Verify credentials and read access to both expected Supabase tables."""
    from supabase import create_client

    client = create_client(supabase_url, supabase_key)
    checked_tables = []
    for table, column in (
        ("inaproc_rup", "kode_rup"),
        ("inaproc_realisasi", "kode_paket"),
    ):
        client.table(table).select(column).limit(1).execute()
        checked_tables.append(table)
    return checked_tables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const=str(DEFAULT_EXPORT_PATH),
        metavar="PATH",
        help=(
            "export all fetched rows to CSV instead of syncing Supabase "
            f"(default: {DEFAULT_EXPORT_PATH})"
        ),
    )
    parser.add_argument(
        "--sync-csv",
        nargs="?",
        const="",
        metavar="PATH",
        help=(
            "sync an existing exported CSV to Supabase instead of fetching "
            "INAPROC (default depends on --dataset)"
        ),
    )
    parser.add_argument(
        "--dataset",
        choices=("rup", "realisasi"),
        default="rup",
        help="dataset to fetch and optionally sync to Supabase (default: rup)",
    )
    parser.add_argument(
        "--test-supabase",
        action="store_true",
        help="test credentials and read access to the RUP and Realisasi tables",
    )
    parser.add_argument(
        "--compare-realisasi",
        nargs="?",
        const=str(DEFAULT_COMPARISON_EXPORT_PATH),
        metavar="PATH",
        help=(
            "compare RUP plans with recorded Realisasi packages and export a "
            f"status CSV (default: {DEFAULT_COMPARISON_EXPORT_PATH})"
        ),
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "override a dashboard filter; repeat for multiple filters, "
            "and repeat multi-select filters for multiple selections"
        ),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=PAGE_SIZE,
        help=f"rows per request (1-{PAGE_SIZE}, default: {PAGE_SIZE})",
    )
    parser.add_argument(
        "--export-filter-reference",
        nargs="?",
        const=str(DEFAULT_FILTER_REFERENCE_PATH),
        metavar="PATH",
        help=(
            "download filter codes for one year as JSON "
            f"(default: {DEFAULT_FILTER_REFERENCE_PATH})"
        ),
    )
    parser.add_argument(
        "--reference-year",
        default="2026",
        help="budget year for filter code reference (default: 2026)",
    )
    args = parser.parse_args()
    if args.sync_csv is not None and (
        args.export_csv is not None
        or args.compare_realisasi is not None
        or args.test_supabase
        or args.export_filter_reference is not None
        or args.filter
    ):
        parser.error(
            "--sync-csv cannot be combined with export, comparison, "
            "connection-test, filter-reference, or filter options."
        )
    if args.export_filter_reference is not None:
        try:
            reference = fetch_filter_reference(args.reference_year)
            output_path = args.export_filter_reference
            if output_path == str(DEFAULT_FILTER_REFERENCE_PATH):
                output_path = (
                    Path("reference") / f"inaproc_filter_codes_{args.reference_year}.json"
                )
            path = export_filter_reference(reference, output_path)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        totals = {
            key: len(items)
            for key, items in reference["unique_codes"].items()
        }
        print(f"Referensi filter tersimpan: {path.resolve()}")
        print(f"Tahun: {reference['year']}; kode unik: {totals}")
        return

    needs_supabase = (
        args.export_csv is None
        and args.compare_realisasi is None
        and not args.export_filter_reference
    )
    if needs_supabase or args.test_supabase or Path(".env").is_file():
        from dotenv import load_dotenv

        load_dotenv()
    if args.test_supabase:
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not supabase_url or not supabase_key:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env "
                "or the environment."
            )
        tables = test_supabase_connection(supabase_url, supabase_key)
        print(
            "Koneksi Supabase berhasil. Tabel dapat dibaca: "
            + ", ".join(f"public.{table}" for table in tables)
        )
        return

    default_source_url = (
        DEFAULT_REALISASI_SOURCE_URL
        if args.dataset == "realisasi"
        else DEFAULT_SOURCE_URL
    )
    source_url = os.getenv("SOURCE_URL", default_source_url)
    try:
        source_url = apply_filter_overrides(source_url, args.filter)
        if args.compare_realisasi is not None:
            rup_source_url = source_url
            realization_source_url = source_url
            rup_rows = fetch_rows(rup_source_url, page_size=args.page_size)
            realization_rows = fetch_realisasi_rows(
                realization_source_url, page_size=args.page_size
            )
            rows = build_execution_status_report(rup_rows, realization_rows)
        elif args.sync_csv is not None:
            input_path = args.sync_csv or str(
                DEFAULT_REALISASI_EXPORT_PATH
                if args.dataset == "realisasi"
                else DEFAULT_EXPORT_PATH
            )
            code_column = "kode_paket" if args.dataset == "realisasi" else "kode_rup"
            rows = read_rows_csv(input_path, code_column)
        elif args.dataset == "realisasi":
            rows = fetch_realisasi_rows(source_url, page_size=args.page_size)
        else:
            rows = fetch_rows(source_url, page_size=args.page_size)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.compare_realisasi is not None:
        path = export_rows_csv(rows, args.compare_realisasi)
        with_data = sum(
            row["status_pelaksanaan"] == "Sudah klik" for row in rows
        )
        print(
            f"Laporan perbandingan tersimpan: {path.resolve()} "
            f"({len(rows)} RUP; {with_data} sudah klik; "
            f"{len(rows) - with_data} belum klik)."
        )
        return
    if args.export_csv is not None:
        output_path = args.export_csv
        if (
            args.dataset == "realisasi"
            and output_path == str(DEFAULT_EXPORT_PATH)
        ):
            output_path = str(DEFAULT_REALISASI_EXPORT_PATH)
        path = export_rows_csv(rows, output_path)
        print(f"CSV tersimpan: {path.resolve()} ({len(rows)} baris).")
        return

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        raise SystemExit(
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your environment or .env."
        )

    records = (
        prepare_realisasi_records(rows, source_url)
        if args.dataset == "realisasi"
        else prepare_records(rows, source_url)
    )
    if not records:
        print(f"No {args.dataset} records available; nothing was written.")
        return

    from supabase import create_client

    client = create_client(supabase_url, supabase_key)
    saved_count = sync_records(client, args.dataset, records)
    table = (
        "inaproc_realisasi" if args.dataset == "realisasi" else "inaproc_rup"
    )
    print(f"Berhasil menyimpan {saved_count} paket ke public.{table}.")


if __name__ == "__main__":
    main()
