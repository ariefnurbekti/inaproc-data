"""Interactive INAPROC scraper and Supabase sync UI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from httpx import HTTPError
from postgrest.exceptions import APIError
from supabase import SupabaseException

from scraper import (
    FILTER_KEYS,
    build_filter_combinations,
    build_sequential_dataset_plan,
    export_rows_csv,
    fetch_realisasi_rows,
    fetch_rows,
    prepare_realisasi_records,
    prepare_records,
    sync_records,
)

REFERENCE_DIR = Path(__file__).parent / "reference"

st.set_page_config(page_title="INAPROC → Supabase", page_icon="📊", layout="wide")
load_dotenv()


@st.cache_data(ttl=86_400, show_spinner=False)
def load_filter_reference(year: str) -> dict:
    path = REFERENCE_DIR / f"inaproc_filter_codes_{year}.json"
    with path.open(encoding="utf-8") as reference_file:
        return json.load(reference_file)


def _label(item: dict) -> str:
    return f"{item['nama']} ({item['kode']})"


def _filter_values(value: str, key: str) -> list[str]:
    values = [part.strip() for part in value.split(",") if part.strip()]
    if key not in FILTER_KEYS:
        raise ValueError(f"Filter tidak dikenal: {key}")
    return values


def _get_supabase_credentials() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return url, key

    try:
        url = str(st.secrets["SUPABASE_URL"]).strip()
        key = str(st.secrets["SUPABASE_SERVICE_ROLE_KEY"]).strip()
    except (KeyError, FileNotFoundError):
        return "", ""
    return url, key


available_years = sorted(
    (
        path.stem.removeprefix("inaproc_filter_codes_")
        for path in REFERENCE_DIR.glob("inaproc_filter_codes_*.json")
        if path.stem.removeprefix("inaproc_filter_codes_").isdigit()
    ),
    reverse=True,
)

st.title("INAPROC → Supabase")
st.write(
    "Pilih filter sekali untuk menjalankan scraping RUP dan Realisasi. "
    "Anda dapat memilih beberapa instansi sekaligus."
)
if not available_years:
    st.error(
        "Katalog filter belum tersedia. Tambahkan file "
        "reference/inaproc_filter_codes_<tahun>.json ke repository."
    )
    st.stop()

selected_years = st.multiselect(
    "Tahun anggaran (pilih satu atau lebih)",
    options=available_years,
    default=available_years[:1],
)
if not selected_years:
    st.info("Pilih setidaknya satu tahun anggaran.")
    st.stop()

year = selected_years[0]
try:
    filter_reference = load_filter_reference(year)
except (OSError, json.JSONDecodeError) as exc:
    st.error(f"Tidak dapat membaca katalog filter tahun {year}: {exc}")
    st.stop()

filter_options = filter_reference["filters"]
kind_options = filter_options["jenis_klpd"]
source_options = filter_options["sumber_dana"]
procurement_options = filter_options["cara_pengadaan"]

kind_index = next(
    (index for index, item in enumerate(kind_options) if item["kode"] == "4"),
    None,
)
selected_kinds = st.multiselect(
    "Jenis instansi",
    options=kind_options,
    default=kind_options[kind_index : kind_index + 1] if kind_index is not None else [],
    format_func=_label,
)

instansi_options = [
    (kind["kode"], instansi)
    for kind in selected_kinds
    for instansi in filter_options["instansi_by_jenis"].get(kind["kode"], [])
]
instansi_by_key = {
    f"{kind_code}:{instansi['kode']}": (kind_code, instansi)
    for kind_code, instansi in instansi_options
}
default_instansi = next(
    (
        key
        for key, (kind_code, instansi) in instansi_by_key.items()
        if kind_code == "4" and instansi["kode"] == "D276"
    ),
    None,
)
selected_instansi_keys = st.multiselect(
    "Instansi (pilih satu atau lebih)",
    options=list(instansi_by_key),
    default=[default_instansi] if default_instansi is not None else [],
    format_func=lambda key: _label(instansi_by_key[key][1]),
)

satker_by_key: dict[str, dict] = {}
eselon_by_key: dict[str, dict] = {}
for instansi_key in selected_instansi_keys:
    kind_code, instansi = instansi_by_key[instansi_key]
    dependent_key = f"{kind_code}:{instansi['kode']}"
    for satker in filter_options["satker_by_instansi"].get(dependent_key, []):
        option_key = f"{dependent_key}:{satker['kode']}"
        satker_by_key[option_key] = {
            **satker,
            "instansi_nama": instansi["nama"],
        }
    if kind_code in {"1", "2"}:
        for eselon in filter_options["eselon_by_instansi"].get(dependent_key, []):
            option_key = f"{dependent_key}:{eselon['kode']}"
            eselon_by_key[option_key] = {
                **eselon,
                "instansi_nama": instansi["nama"],
            }


def _dependent_label(option_key: str, options: dict[str, dict]) -> str:
    option = options[option_key]
    return (
        f"{option['nama']} ({option['kode']}) — {option['instansi_nama']}"
    )


column_left, column_right = st.columns(2)
with column_left:
    selected_satker_keys = st.multiselect(
        "Satuan kerja",
        options=list(satker_by_key),
        format_func=lambda key: _dependent_label(key, satker_by_key),
    )
    selected_sources = st.multiselect(
        "Sumber dana",
        options=source_options,
        format_func=_label,
    )
with column_right:
    selected_eselon_keys = st.multiselect(
        "Eselon",
        options=list(eselon_by_key),
        format_func=lambda key: _dependent_label(key, eselon_by_key),
        disabled=not eselon_by_key,
    )
    selected_procurement = st.multiselect(
        "Cara pengadaan",
        options=procurement_options,
        format_func=_label,
    )

with st.expander("Filter tambahan dan pencarian"):
    status_paket = st.text_input(
        "Status paket (pisahkan beberapa nilai dengan koma)",
        placeholder="Contoh: ON PROCESS, SELESAI",
    )
    search_value = st.text_input(
        "Cari berdasarkan kode/nama paket (berlaku untuk kedua dataset)"
    )
    advanced_text = st.text_area(
        "Filter API lain, satu KEY=VALUE per baris",
        help=(
            "Opsional. Contoh: search_penyedia=Nama Penyedia. Daftar kunci "
            f"yang didukung: {', '.join(sorted(FILTER_KEYS))}."
        ),
        height=100,
    )

selected_statuses = (
    _filter_values(status_paket, "status_paket") if status_paket.strip() else []
)

advanced_filter_pairs: list[tuple[str, str]] = []
for line_number, line in enumerate(advanced_text.splitlines(), start=1):
    value = line.strip()
    if not value:
        continue
    key, separator, filter_value = value.partition("=")
    if not separator or key not in FILTER_KEYS or not filter_value.strip():
        st.error(
            f"Filter tambahan baris {line_number} harus berformat "
            "KEY=VALUE dengan KEY yang didukung."
        )
        st.stop()
    advanced_filter_pairs.append((key, filter_value.strip()))

scope_groups: list[list[tuple[str, str]]] = []
if selected_instansi_keys:
    for instansi_key in selected_instansi_keys:
        kind_code, instansi = instansi_by_key[instansi_key]
        dependent_key = f"{kind_code}:{instansi['kode']}"
        selected_satkers = [
            satker_by_key[key]
            for key in selected_satker_keys
            if key.startswith(f"{dependent_key}:")
        ]
        selected_eselons = [
            eselon_by_key[key]
            for key in selected_eselon_keys
            if key.startswith(f"{dependent_key}:")
        ]
        if selected_satkers and selected_eselons:
            local_scopes = [
                [("satker", satker["kode"]), ("eselon", eselon["kode"])]
                for satker in selected_satkers
                for eselon in selected_eselons
            ]
        elif selected_satkers:
            local_scopes = [[("satker", satker["kode"])] for satker in selected_satkers]
        elif selected_eselons:
            local_scopes = [[("eselon", eselon["kode"])] for eselon in selected_eselons]
        elif selected_satker_keys or selected_eselon_keys:
            continue
        else:
            local_scopes = [[]]
        for scope in local_scopes:
            scope_groups.append(
                [
                    ("jenis_klpd", kind_code),
                    ("instansi", instansi["kode"]),
                    *scope,
                ]
            )
elif selected_kinds:
    scope_groups.extend(
        [("jenis_klpd", kind["kode"])] for kind in selected_kinds
    )
else:
    scope_groups.append([])

filter_groups: list[list[list[tuple[str, str]]]] = [
    [[("tahun", selected_year)] for selected_year in selected_years],
    scope_groups or [[]],
    [[("sumber_dana", item["kode"])] for item in selected_sources],
    [[("filter_metode", item["kode"])] for item in selected_procurement],
    [[("status_paket", value)] for value in selected_statuses],
]
combinations = build_filter_combinations(filter_groups)

try:
    dataset_plans = build_sequential_dataset_plan(
        combinations,
        search_value=search_value.strip(),
        additional_filters=advanced_filter_pairs,
    )
except ValueError as exc:
    st.error(f"Filter tidak valid: {exc}")
    st.stop()

st.caption(
    f"{len(combinations)} kombinasi filter; "
    f"{len(dataset_plans)} proses dataset (RUP + Realisasi per kombinasi)."
)
st.warning(
    "Setiap kombinasi diproses satu per satu: RUP terlebih dahulu, kemudian "
    "Realisasi. Banyak pilihan filter dapat menghasilkan banyak permintaan."
)
confirmed = st.checkbox(
    f"Saya memahami bahwa {len(dataset_plans)} proses dataset akan berjalan berurutan."
)

sync_clicked = st.button(
    "Jalankan RUP + Realisasi dan simpan ke Supabase",
    type="primary",
    use_container_width=True,
    disabled=not confirmed,
)
if sync_clicked:
    supabase_url, supabase_key = _get_supabase_credentials()
    if not supabase_url or not supabase_key:
        st.error(
            "Kredensial belum tersedia. Tambahkan SUPABASE_URL dan "
            "SUPABASE_SERVICE_ROLE_KEY di Streamlit secrets."
        )
        st.stop()

    from supabase import create_client

    try:
        client = create_client(supabase_url, supabase_key)
    except SupabaseException as exc:
        st.error(f"Tidak dapat menghubungkan ke Supabase: {exc}")
        st.stop()
    results: dict[str, dict[str, dict]] = {"rup": {}, "realisasi": {}}
    saved_counts = {"rup": 0, "realisasi": 0}
    failures: list[str] = []
    progress = st.progress(0, text="Menyiapkan proses...")
    for process_index, (dataset, filtered_source_url, combination_index) in enumerate(
        dataset_plans, start=1
    ):
        progress.progress(
            (process_index - 1) / len(dataset_plans),
            text=(
                f"Kombinasi {combination_index}/{len(combinations)} — "
                f"{dataset.upper()} ({process_index}/{len(dataset_plans)})"
            ),
        )
        try:
            with st.spinner(
                f"Kombinasi {combination_index}/{len(combinations)}: "
                f"mengambil {dataset.upper()}..."
            ):
                rows = (
                    fetch_rows(filtered_source_url)
                    if dataset == "rup"
                    else fetch_realisasi_rows(filtered_source_url)
                )
            if not rows:
                st.info(
                    f"Kombinasi {combination_index}: tidak ada data "
                    f"{dataset.upper()} yang cocok."
                )
                continue
            records = (
                prepare_records(rows, filtered_source_url)
                if dataset == "rup"
                else prepare_realisasi_records(rows, filtered_source_url)
            )
            with st.spinner(
                f"Menyimpan {len(records)} baris {dataset.upper()} "
                f"(kombinasi {combination_index}) ke Supabase..."
            ):
                saved_count = sync_records(client, dataset, records)
            code_column = "kode_rup" if dataset == "rup" else "kode_paket"
            results[dataset].update(
                (record[code_column], row)
                for record, row in zip(records, rows, strict=True)
            )
            saved_counts[dataset] += saved_count
            table = "inaproc_realisasi" if dataset == "realisasi" else "inaproc_rup"
            st.success(
                f"Kombinasi {combination_index}: berhasil menyimpan "
                f"{saved_count} baris ke public.{table}."
            )
        except (
            APIError,
            HTTPError,
            OSError,
            RuntimeError,
            SupabaseException,
            ValueError,
        ) as exc:
            failures.append(
                f"Kombinasi {combination_index} {dataset.upper()}: {exc}"
            )
            st.error(f"Kombinasi {combination_index} {dataset.upper()} gagal: {exc}")

    progress.progress(1.0, text="Semua kombinasi selesai.")
    for dataset, dataset_rows in results.items():
        rows = list(dataset_rows.values())
        if not rows:
            continue
        st.dataframe(rows[:100], use_container_width=True)
        if len(rows) > 100:
            st.caption(
                f"Pratinjau {dataset.upper()} dibatasi 100 baris pertama "
                f"dari {len(rows)} kode unik."
            )
        csv_path = export_rows_csv(
            rows,
            Path("exports")
            / (
                "inaproc_rup.csv"
                if dataset == "rup"
                else "inaproc_realisasi.csv"
            ),
        )
        st.download_button(
            f"Unduh {dataset.upper()} sebagai CSV",
            data=csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
            key=f"download_{dataset}",
        )
    st.info(
        "Total operasi upsert — "
        + "; ".join(
            f"{dataset.upper()}: {count}"
            for dataset, count in saved_counts.items()
        )
    )
    if failures:
        st.error(
            f"{len(failures)} proses gagal. Proses lainnya tetap berjalan. "
            "Periksa pesan error masing-masing di atas."
        )
