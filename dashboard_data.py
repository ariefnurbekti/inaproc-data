"""Read and combine INAPROC datasets for the Streamlit dashboard."""

from __future__ import annotations

from scraper import build_execution_status_report

DASHBOARD_PAGE_SIZE = 1000
DASHBOARD_MAX_ROWS_PER_TABLE = 10_000
COMPLETED_STATUSES = {"SELESAI", "COMPLETED", "DONE", "FINISHED"}


def fetch_dataset_rows(client, table_name: str, year: str) -> tuple[list[dict], bool]:
    """Fetch a bounded, year-filtered dataset using PostgREST range pagination."""
    if table_name not in {"inaproc_rup", "inaproc_realisasi"}:
        raise ValueError(f"Unsupported dashboard table: {table_name}")

    rows: list[dict] = []
    primary_key = "kode_rup" if table_name == "inaproc_rup" else "kode_paket"
    for offset in range(
        0, DASHBOARD_MAX_ROWS_PER_TABLE + DASHBOARD_PAGE_SIZE, DASHBOARD_PAGE_SIZE
    ):
        page_size = min(
            DASHBOARD_PAGE_SIZE, DASHBOARD_MAX_ROWS_PER_TABLE - offset
        )
        if page_size == 0:
            page_size = 1
        response = (
            client.table(table_name)
            .select(f"{primary_key},data")
            .filter("data->>tahun_anggaran", "eq", year)
            .order(primary_key)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        page_rows = response.data
        if offset == DASHBOARD_MAX_ROWS_PER_TABLE:
            return rows, bool(page_rows)
        rows.extend(
            row["data"]
            for row in page_rows
            if isinstance(row.get("data"), dict)
        )
        if len(page_rows) < page_size:
            return rows, False
    return rows, False


def build_dashboard_rows(rup_rows: list[dict], realization_rows: list[dict]) -> list[dict]:
    """Compare RUP plans with Realisasi and derive a conservative display status."""
    report = build_execution_status_report(rup_rows, realization_rows)
    for row in report:
        statuses = [
            status.strip().upper()
            for status in row["status_paket_realisasi"].split(";")
            if status.strip()
        ]
        if not row["jumlah_paket_realisasi"]:
            row["indikasi_penyelesaian"] = "Belum ada rekaman Realisasi"
        elif statuses and all(status in COMPLETED_STATUSES for status in statuses):
            row["indikasi_penyelesaian"] = "Selesai (menurut status INAPROC)"
        else:
            row["indikasi_penyelesaian"] = "Ada rekaman Realisasi"
    return report
