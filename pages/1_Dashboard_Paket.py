"""Browse scraped INAPROC packages and their recorded Realisasi status."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from httpx import HTTPError
from postgrest.exceptions import APIError
from supabase import SupabaseException, create_client

from dashboard_data import (
    DASHBOARD_MAX_ROWS_PER_TABLE,
    build_dashboard_rows,
    fetch_dataset_rows,
    format_indonesian_currency,
    format_indonesian_number,
)

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference"

st.set_page_config(page_title="Dashboard Paket INAPROC", page_icon="📋", layout="wide")
load_dotenv()
st.markdown(
    """
    <style>
    .stApp {
        background: #f4f6fa;
    }
    [data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 14px 18px;
        box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
    }
    [data-testid="stMetricLabel"] {
        color: #526176;
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid #dce3ed;
        border-radius: 8px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _get_supabase_credentials() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return url, key
    try:
        return (
            str(st.secrets["SUPABASE_URL"]).strip(),
            str(st.secrets["SUPABASE_SERVICE_ROLE_KEY"]).strip(),
        )
    except (KeyError, FileNotFoundError):
        return "", ""


def _load_available_years() -> list[str]:
    years = sorted(
        (
            path.stem.removeprefix("inaproc_filter_codes_")
            for path in REFERENCE_DIR.glob("inaproc_filter_codes_*.json")
            if path.stem.removeprefix("inaproc_filter_codes_").isdigit()
        ),
        reverse=True,
    )
    if not years:
        raise FileNotFoundError("Katalog filter tahun tidak ditemukan.")
    return years


@st.cache_data(ttl=86_400, show_spinner=False)
def _load_instansi_names(year: str) -> list[str]:
    path = REFERENCE_DIR / f"inaproc_filter_codes_{year}.json"
    with path.open(encoding="utf-8") as reference_file:
        reference = json.load(reference_file)
    return sorted(
        {
            instansi["nama"]
            for items in reference["filters"]["instansi_by_jenis"].values()
            for instansi in items
        }
    )


st.title("Dashboard Paket INAPROC")
st.caption(
    "Ringkasan dan eksplorasi paket pengadaan — filter, pilih kolom, atur "
    "urutannya, lalu unduh hasil analisis."
)
st.info(
    "Label sudah/belum klik menunjukkan status pencocokan paket dengan data "
    "Realisasi yang discrape, bukan bukti pasti pekerjaan sudah atau belum "
    "dilakukan. Penyedia yang "
    "ditampilkan adalah nama penyedia dari sumber INAPROC, bukan verifikasi "
    "pemenang kontrak secara terpisah."
)

try:
    years = _load_available_years()
except (OSError, json.JSONDecodeError) as exc:
    st.error(f"Tidak dapat memuat daftar tahun: {exc}")
    st.stop()

selected_year = st.selectbox("Tahun anggaran", years)
if "dashboard_rows" not in st.session_state:
    st.session_state.dashboard_rows = None
    st.session_state.dashboard_year = None

if st.button("Muat / perbarui data dari Supabase", type="primary"):
    supabase_url, supabase_key = _get_supabase_credentials()
    if not supabase_url or not supabase_key:
        st.error(
            "Kredensial Supabase tidak tersedia. Atur SUPABASE_URL dan "
            "SUPABASE_SERVICE_ROLE_KEY di environment atau Streamlit Secrets."
        )
        st.stop()
    try:
        client = create_client(supabase_url, supabase_key)
        with st.spinner(f"Membaca data tahun {selected_year} dari Supabase..."):
            rup_rows, rup_truncated = fetch_dataset_rows(
                client, "inaproc_rup", selected_year
            )
            realization_rows, realization_truncated = fetch_dataset_rows(
                client, "inaproc_realisasi", selected_year
            )
        st.session_state.dashboard_rows = build_dashboard_rows(
            rup_rows, realization_rows
        )
        st.session_state.dashboard_year = selected_year
        st.session_state.dashboard_rup_truncated = rup_truncated
        st.session_state.dashboard_realization_truncated = realization_truncated
        st.session_state.dashboard_rup_count = len(rup_rows)
        st.session_state.dashboard_realization_count = len(realization_rows)
    except (
        APIError,
        HTTPError,
        OSError,
        RuntimeError,
        SupabaseException,
        ValueError,
    ) as exc:
        st.error(f"Gagal membaca data Supabase: {exc}")

dashboard_rows = st.session_state.dashboard_rows
if dashboard_rows is None:
    st.caption("Pilih tahun, lalu muat data untuk melihat dashboard.")
    st.stop()
if st.session_state.dashboard_year != selected_year:
    st.warning("Tahun berubah. Muat ulang data agar dashboard memakai tahun tersebut.")
    st.stop()

rup_truncated = st.session_state.dashboard_rup_truncated
realization_truncated = st.session_state.dashboard_realization_truncated
if rup_truncated or realization_truncated:
    st.warning(
        "Tampilan dibatasi hingga "
        f"{format_indonesian_number(DASHBOARD_MAX_ROWS_PER_TABLE)} baris per tabel. "
        "Persempit tahun atau filter data agar laporan tidak terpotong."
    )

total_rup = len(dashboard_rows)
with_records = sum(row["jumlah_paket_realisasi"] > 0 for row in dashboard_rows)
completed = sum(
    row["status_realisasi_selesai"]
    for row in dashboard_rows
)
not_recorded = total_rup - with_records

metric_columns = st.columns(4)
metric_columns[0].metric("Paket RUP", format_indonesian_number(total_rup))
metric_columns[1].metric("Sudah klik", format_indonesian_number(with_records))
metric_columns[2].metric(
    "Status tercatat selesai", format_indonesian_number(completed)
)
metric_columns[3].metric("Belum klik", format_indonesian_number(not_recorded))

try:
    instansi_names = _load_instansi_names(selected_year)
except (OSError, json.JSONDecodeError, KeyError) as exc:
    st.error(f"Tidak dapat memuat filter instansi: {exc}")
    st.stop()

filter_columns = st.columns([2, 2, 1])
with filter_columns[0]:
    selected_instansi_names = st.multiselect(
        "Filter instansi",
        options=instansi_names,
    )
with filter_columns[1]:
    search_text = st.text_input(
        "Cari kode atau nama paket",
        placeholder="Contoh: kode RUP, nama paket, atau penyedia",
    ).strip().casefold()
with filter_columns[2]:
    status_choices = ["Semua", "Selesai", "Sudah klik", "Belum klik"]
    selected_status = st.selectbox("Status pekerjaan", status_choices)

filtered_rows = dashboard_rows
if selected_instansi_names:
    selected_names = set(selected_instansi_names)
    filtered_rows = [
        row for row in filtered_rows if row.get("nama_instansi") in selected_names
    ]
if search_text:
    searchable_keys = (
        "kode_rup",
        "nama_paket",
        "nama_instansi",
        "nama_satker",
        "kode_paket_realisasi",
        "status_paket_realisasi",
        "tahapan_pengadaan_realisasi",
        "nama_penyedia_realisasi",
    )
    filtered_rows = [
        row
        for row in filtered_rows
        if any(search_text in str(row.get(key, "")).casefold() for key in searchable_keys)
    ]
if selected_status == "Selesai":
    filtered_rows = [
        row
        for row in filtered_rows
        if row["status_realisasi_selesai"]
    ]
elif selected_status == "Sudah klik":
    filtered_rows = [
        row for row in filtered_rows if row["jumlah_paket_realisasi"] > 0
    ]
elif selected_status == "Belum klik":
    filtered_rows = [
        row for row in filtered_rows if row["jumlah_paket_realisasi"] == 0
    ]

display_columns = {
    "kode_rup": "Kode RUP",
    "nama_paket": "Nama paket",
    "nama_instansi": "Instansi",
    "nama_satker": "Satuan kerja",
    "tahun_anggaran": "Tahun",
    "total_nilai": "Nilai RUP",
    "status_pelaksanaan": "Status klik",
    "jumlah_paket_realisasi": "Jumlah paket Realisasi",
    "kode_paket_realisasi": "Kode paket Realisasi",
    "status_paket_realisasi": "Status Realisasi",
    "tahapan_pengadaan_realisasi": "Tahapan pengadaan",
    "nama_penyedia_realisasi": "Penyedia tercatat",
}
st.subheader("Ringkasan hasil filter")
status_chart = pd.DataFrame(
    {
        "Jumlah paket": [
            sum(row["jumlah_paket_realisasi"] > 0 for row in filtered_rows),
            sum(row["jumlah_paket_realisasi"] == 0 for row in filtered_rows),
            sum(row["status_realisasi_selesai"] for row in filtered_rows),
        ]
    },
    index=["Sudah klik", "Belum klik", "Status tercatat selesai"],
)
st.bar_chart(status_chart, height=220)

st.subheader(f"Hasil paket ({format_indonesian_number(len(filtered_rows))})")
if filtered_rows:
    table = pd.DataFrame(filtered_rows)
    available_columns = [
        column for column in display_columns if column in table.columns
    ]
    if "dashboard_column_order" not in st.session_state:
        st.session_state.dashboard_column_order = available_columns.copy()
    column_order = [
        column
        for column in st.session_state.dashboard_column_order
        if column in available_columns
    ]
    column_order.extend(
        column for column in available_columns if column not in column_order
    )
    st.session_state.dashboard_column_order = column_order

    selected_column_key = "dashboard_selected_columns"
    if selected_column_key not in st.session_state:
        st.session_state[selected_column_key] = available_columns.copy()
    else:
        st.session_state[selected_column_key] = [
            column
            for column in st.session_state[selected_column_key]
            if column in available_columns
        ]

    st.multiselect(
        "Kolom yang ditampilkan",
        options=available_columns,
        format_func=display_columns.__getitem__,
        key=selected_column_key,
        help="Pilih kolom yang ingin dilihat pada tabel.",
    )
    selected_columns = st.session_state[selected_column_key]
    selected_order = [
        column for column in column_order if column in selected_columns
    ]

    if len(selected_order) > 1:
        if st.session_state.get("dashboard_column_to_move") not in selected_order:
            st.session_state.dashboard_column_to_move = selected_order[0]
        move_columns = st.columns([3, 1, 1, 5])
        with move_columns[0]:
            column_to_move = st.selectbox(
                "Kolom yang digeser",
                options=selected_order,
                format_func=display_columns.__getitem__,
                key="dashboard_column_to_move",
            )
        with move_columns[1]:
            if st.button("← Geser", use_container_width=True):
                visible_index = selected_order.index(column_to_move)
                if visible_index > 0:
                    previous_column = selected_order[visible_index - 1]
                    current_index = column_order.index(column_to_move)
                    previous_index = column_order.index(previous_column)
                    column_order[current_index], column_order[previous_index] = (
                        column_order[previous_index],
                        column_order[current_index],
                    )
                    st.session_state.dashboard_column_order = column_order
                    st.rerun()
        with move_columns[2]:
            if st.button("Geser →", use_container_width=True):
                visible_index = selected_order.index(column_to_move)
                if visible_index < len(selected_order) - 1:
                    next_column = selected_order[visible_index + 1]
                    current_index = column_order.index(column_to_move)
                    next_index = column_order.index(next_column)
                    column_order[current_index], column_order[next_index] = (
                        column_order[next_index],
                        column_order[current_index],
                    )
                    st.session_state.dashboard_column_order = column_order
                    st.rerun()

    selected_order = [
        column for column in column_order if column in selected_columns
    ]
    page_controls = st.columns([2, 2, 2, 5])
    with page_controls[0]:
        page_size = st.selectbox(
            "Baris per halaman",
            options=[25, 50, 100],
            key="dashboard_page_size",
        )
    page_count = max(1, (len(filtered_rows) + page_size - 1) // page_size)
    if "dashboard_page" not in st.session_state:
        st.session_state.dashboard_page = 0
    st.session_state.dashboard_page = min(
        st.session_state.dashboard_page, page_count - 1
    )
    with page_controls[1]:
        if st.button(
            "← Sebelumnya",
            disabled=st.session_state.dashboard_page == 0,
            use_container_width=True,
        ):
            st.session_state.dashboard_page -= 1
            st.rerun()
    with page_controls[2]:
        if st.button(
            "Selanjutnya →",
            disabled=st.session_state.dashboard_page >= page_count - 1,
            use_container_width=True,
        ):
            st.session_state.dashboard_page += 1
            st.rerun()
    start_row = st.session_state.dashboard_page * page_size
    end_row = min(start_row + page_size, len(filtered_rows))
    st.caption(
        f"Halaman {format_indonesian_number(st.session_state.dashboard_page + 1)} "
        f"dari {format_indonesian_number(page_count)} · Menampilkan baris "
        f"{format_indonesian_number(start_row + 1)}–"
        f"{format_indonesian_number(end_row)} dari "
        f"{format_indonesian_number(len(filtered_rows))}"
    )

    if selected_order:
        visible_table = table.iloc[start_row:end_row][selected_order].copy()
        if "total_nilai" in visible_table.columns:
            visible_table["total_nilai"] = visible_table["total_nilai"].map(
                format_indonesian_currency
            )
        st.dataframe(
            visible_table.rename(columns=display_columns),
            use_container_width=True,
            hide_index=True,
            height=min(620, 38 + (end_row - start_row) * 35),
        )
        st.download_button(
            "Unduh hasil filter sebagai CSV",
            data=table[selected_order].rename(columns=display_columns).to_csv(
                index=False
            ).encode("utf-8-sig"),
            file_name=f"inaproc_status_{selected_year}.csv",
            mime="text/csv",
        )
    else:
        st.info("Pilih setidaknya satu kolom untuk ditampilkan.")
else:
    st.info("Tidak ada paket yang cocok dengan filter.")

st.caption(
    "Data termuat dari Supabase: "
    f"{format_indonesian_number(st.session_state.dashboard_rup_count)} RUP, "
    f"{format_indonesian_number(st.session_state.dashboard_realization_count)} "
    "Realisasi."
)
