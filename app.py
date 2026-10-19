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
    "Pilih dataset dan filter dari katalog opsi INAPROC. Daftar instansi, "
    "satker, dan filter lainnya akan menyesuaikan pilihan Anda."
)
if not available_years:
    st.error(
        "Katalog filter belum tersedia. Tambahkan file "
        "reference/inaproc_filter_codes_<tahun>.json ke repository."
    )
    st.stop()

dataset_label = st.radio(
    "Dataset",
    options=("RUP", "Realisasi"),
    horizontal=True,
)
dataset = "rup" if dataset_label == "RUP" else "realisasi"
year = st.selectbox(
    "Tahun anggaran",
    options=available_years,
)
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

instansi_options: list[tuple[str, dict]] = []
for kind in selected_kinds:
    instansi_options.extend(
        (kind["kode"], item)
        for item in filter_options["instansi_by_jenis"].get(kind["kode"], [])
    )

default_instansi = next(
    (
        option
        for option in instansi_options
        if option[1]["kode"] == "D276" and option[0] == "4"
    ),
    None,
)
selected_instansi = st.selectbox(
    "Instansi",
    options=[None, *instansi_options],
    index=(
        instansi_options.index(default_instansi) + 1
        if default_instansi is not None
        else 0
    ),
    format_func=lambda option: (
        "Semua instansi"
        if option is None
        else f"{option[1]['nama']} ({option[1]['kode']})"
    ),
)

dependent_options: dict[str, list[dict]] = {"satker": [], "eselon": []}
if selected_instansi is not None:
    kind_code, instansi = selected_instansi
    instansi_key = f"{kind_code}:{instansi['kode']}"
    dependent_options["satker"] = filter_options["satker_by_instansi"].get(
        instansi_key, []
    )
    if kind_code in {"1", "2"}:
        dependent_options["eselon"] = filter_options["eselon_by_instansi"].get(
            instansi_key, []
        )

column_left, column_right = st.columns(2)
with column_left:
    selected_satker = st.selectbox(
        "Satuan kerja",
        options=[None, *dependent_options["satker"]],
        format_func=lambda option: (
            "Semua satuan kerja" if option is None else _label(option)
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
        options=[None, *dependent_options["eselon"]],
        format_func=lambda option: "Semua eselon" if option is None else _label(option),
        disabled=not dependent_options["eselon"],
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
    search_field = (
        "search_rup" if dataset == "rup" else "search_paket"
    )
    search_value = st.text_input(
        "Cari berdasarkan kode/nama paket",
        key=f"{dataset}_search",
    )
    advanced_text = st.text_area(
        "Filter API lain, satu KEY=VALUE per baris",
        help=(
            "Opsional. Contoh: search_penyedia=Nama Penyedia. Daftar kunci "
            f"yang didukung: {', '.join(sorted(FILTER_KEYS))}."
        ),
        height=100,
    )

filter_pairs: list[tuple[str, str]] = [("tahun", year)]
kind_key = "jenis_klpd" if dataset == "rup" else "jenis"
filter_pairs.extend((kind_key, item["kode"]) for item in selected_kinds)
if selected_instansi is not None:
    kind_code, instansi = selected_instansi
    filter_pairs.append(("instansi", instansi["kode"]))
    if selected_eselon is not None:
        filter_pairs.append(("eselon", selected_eselon["kode"]))
    if selected_satker is not None:
        filter_pairs.append(("satker", selected_satker["kode"]))
filter_pairs.extend(
    ("sumber_dana", item["kode"]) for item in selected_sources
)
filter_pairs.extend(
    ("filter_metode", item["kode"]) for item in selected_procurement
)

if status_paket.strip():
    filter_pairs.extend(
        ("status_paket", value)
        for value in _filter_values(status_paket, "status_paket")
    )
if search_value.strip():
    filter_pairs.append((search_field, search_value.strip()))

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
    filter_pairs.append((key, filter_value.strip()))

filters = [f"{key}={value}" for key, value in filter_pairs]
source_url = (
    "https://data.inaproc.id/rup"
    if dataset == "rup"
    else "https://data.inaproc.id/realisasi"
)
try:
    filtered_source_url = apply_filter_overrides(source_url, filters)
except ValueError as exc:
    st.error(f"Filter tidak valid: {exc}")
    st.stop()

st.caption(f"Filter aktif: {len(filters)}")
st.warning(
    "Scraping dapat mengambil banyak data jika hanya tahun yang dipilih. "
    "Pilih instansi atau satuan kerja untuk membatasi hasil."
)

sync_clicked = st.button(
    "Ambil data dan simpan ke Supabase",
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

    try:
        with st.spinner("Mengambil seluruh halaman data INAPROC..."):
            rows = (
                fetch_rows(filtered_source_url)
                if dataset == "rup"
                else fetch_realisasi_rows(filtered_source_url)
            )
        if not rows:
            st.info("Tidak ada data yang cocok dengan filter.")
        else:
            records = (
                prepare_records(rows, filtered_source_url)
                if dataset == "rup"
                else prepare_realisasi_records(rows, filtered_source_url)
            )
            with st.spinner(f"Menyimpan {len(records)} baris ke Supabase..."):
                from supabase import create_client

                client = create_client(supabase_url, supabase_key)
                saved_count = sync_records(client, dataset, records)
            table = "inaproc_rup" if dataset == "rup" else "inaproc_realisasi"
            st.success(
                f"Berhasil menyimpan {saved_count} baris ke public.{table}."
            )
            st.dataframe(rows[:100], use_container_width=True)
            if len(rows) > 100:
                st.caption("Pratinjau dibatasi 100 baris pertama.")
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
                "Unduh hasil sebagai CSV",
                data=csv_path.read_bytes(),
                file_name=csv_path.name,
                mime="text/csv",
            )
    except (APIError, HTTPError, OSError, RuntimeError, SupabaseException, ValueError) as exc:
        st.error(f"Proses gagal: {exc}")
