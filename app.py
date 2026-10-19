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
    apply_filter_overrides,
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

year = st.selectbox("Tahun anggaran", options=available_years)
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
    selected_satker = st.selectbox(
        "Satuan kerja",
        options=[None, *satker_by_key],
        format_func=lambda key: (
            "Semua satuan kerja"
            if key is None
            else _dependent_label(key, satker_by_key)
        ),
    )
    selected_sources = st.multiselect(
        "Sumber dana",
        options=source_options,
        format_func=_label,
    )
with column_right:
    selected_eselon = st.selectbox(
        "Eselon",
        options=[None, *eselon_by_key],
        format_func=lambda key: (
            "Semua eselon"
            if key is None
            else _dependent_label(key, eselon_by_key)
        ),
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

common_filter_pairs: list[tuple[str, str]] = [("tahun", year)]
common_filter_pairs.extend(
    ("instansi", instansi_by_key[key][1]["kode"])
    for key in selected_instansi_keys
)
common_filter_pairs.extend(("sumber_dana", item["kode"]) for item in selected_sources)
common_filter_pairs.extend(
    ("filter_metode", item["kode"]) for item in selected_procurement
)
if status_paket.strip():
    common_filter_pairs.extend(
        ("status_paket", value)
        for value in _filter_values(status_paket, "status_paket")
    )
if selected_satker is not None:
    common_filter_pairs.append(("satker", satker_by_key[selected_satker]["kode"]))
if selected_eselon is not None:
    common_filter_pairs.append(("eselon", eselon_by_key[selected_eselon]["kode"]))

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

dataset_plans: list[tuple[str, str, int]] = []
for dataset, source_url, kind_key, search_key in (
    ("rup", "https://data.inaproc.id/rup", "jenis_klpd", "search_rup"),
    ("realisasi", "https://data.inaproc.id/realisasi", "jenis", "search_paket"),
):
    filter_pairs = list(common_filter_pairs)
    filter_pairs.extend((kind_key, item["kode"]) for item in selected_kinds)
    if search_value.strip():
        filter_pairs.append((search_key, search_value.strip()))
    filter_pairs.extend(advanced_filter_pairs)
    filters = [f"{key}={value}" for key, value in filter_pairs]
    try:
        filtered_source_url = apply_filter_overrides(source_url, filters)
    except ValueError as exc:
        st.error(f"Filter tidak valid untuk {dataset.upper()}: {exc}")
        st.stop()
    dataset_plans.append((dataset, filtered_source_url, len(filters)))

st.caption(
    "Filter aktif — "
    + "; ".join(
        f"{dataset.upper()}: {count}"
        for dataset, _, count in dataset_plans
    )
)
st.warning(
    "Satu kali proses menjalankan RUP dan Realisasi secara berurutan. "
    "Scraping dapat mengambil banyak data jika cakupan instansi terlalu luas."
)

sync_clicked = st.button(
    "Jalankan RUP + Realisasi dan simpan ke Supabase",
    type="primary",
    use_container_width=True,
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
    results: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    for dataset, filtered_source_url, _ in dataset_plans:
        st.subheader(f"Dataset {dataset.upper()}")
        try:
            with st.spinner(f"Mengambil semua halaman {dataset.upper()}..."):
                rows = (
                    fetch_rows(filtered_source_url)
                    if dataset == "rup"
                    else fetch_realisasi_rows(filtered_source_url)
                )
            if not rows:
                st.info(f"Tidak ada data {dataset.upper()} yang cocok.")
                results[dataset] = []
                continue
            records = (
                prepare_records(rows, filtered_source_url)
                if dataset == "rup"
                else prepare_realisasi_records(rows, filtered_source_url)
            )
            with st.spinner(
                f"Menyimpan {len(records)} baris {dataset.upper()} ke Supabase..."
            ):
                saved_count = sync_records(client, dataset, records)
            results[dataset] = rows
            table = "inaproc_realisasi" if dataset == "realisasi" else "inaproc_rup"
            st.success(
                f"Berhasil menyimpan {saved_count} baris ke public.{table}."
            )
        except (
            APIError,
            HTTPError,
            OSError,
            RuntimeError,
            SupabaseException,
            ValueError,
        ) as exc:
            failures[dataset] = str(exc)
            st.error(f"Dataset {dataset.upper()} gagal: {exc}")

    for dataset, rows in results.items():
        if not rows:
            continue
        st.dataframe(rows[:100], use_container_width=True)
        if len(rows) > 100:
            st.caption(f"Pratinjau {dataset.upper()} dibatasi 100 baris pertama.")
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
    if failures:
        st.error(
            "Sebagian dataset gagal. Dataset yang berhasil tetap sudah "
            "disimpan; periksa pesan error masing-masing di atas."
        )
