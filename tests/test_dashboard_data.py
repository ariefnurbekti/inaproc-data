import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from dashboard_data import (
    build_dashboard_rows,
    fetch_dataset_rows,
    format_indonesian_currency,
    format_indonesian_number,
)


class DashboardDataTests(unittest.TestCase):
    def test_format_indonesian_currency_uses_period_grouping(self):
        self.assertEqual(format_indonesian_currency(1234567), "Rp 1.234.567")
        self.assertEqual(format_indonesian_currency(0), "Rp 0")
        self.assertEqual(format_indonesian_currency(None), "—")

    def test_format_indonesian_number_uses_period_grouping(self):
        self.assertEqual(format_indonesian_number(1234567), "1.234.567")

    def test_build_dashboard_rows_distinguishes_click_and_completion_status(self):
        report = build_dashboard_rows(
            [
                {"kode_rup": "RUP-1", "nama_paket": "Selesai"},
                {"kode_rup": "RUP-2", "nama_paket": "Berjalan"},
                {"kode_rup": "RUP-3", "nama_paket": "Belum ada"},
            ],
            [
                {
                    "kode_rup": "RUP-1",
                    "kode_paket": "PKT-1",
                    "status_paket": "SELESAI",
                    "tahapan_pengadaan": "Serah terima",
                    "nama_penyedia": "Penyedia A",
                },
                {
                    "kode_rup": "RUP-2",
                    "kode_paket": "PKT-2",
                    "status_paket": "ON PROCESS",
                    "tahapan_pengadaan": "Kontrak",
                    "nama_penyedia": "Penyedia B",
                },
            ],
        )

        self.assertEqual(
            [row["status_pelaksanaan"] for row in report],
            ["Sudah klik", "Sudah klik", "Belum klik"],
        )
        self.assertEqual(
            [row["status_realisasi_selesai"] for row in report],
            [True, False, False],
        )
        self.assertEqual(report[0]["nama_penyedia_realisasi"], "Penyedia A")

    def test_fetch_dataset_rows_filters_by_year_and_uses_range_pagination(self):
        client = Mock()
        query = client.table.return_value
        query.select.return_value = query
        query.filter.return_value = query
        query.order.return_value = query
        query.range.return_value = query
        query.execute.return_value = SimpleNamespace(
            data=[{"data": {"kode_rup": "RUP-1", "tahun_anggaran": "2026"}}]
        )

        rows, truncated = fetch_dataset_rows(client, "inaproc_rup", "2026")

        self.assertEqual(rows, [{"kode_rup": "RUP-1", "tahun_anggaran": "2026"}])
        self.assertFalse(truncated)
        query.filter.assert_called_once_with(
            "data->>tahun_anggaran", "eq", "2026"
        )
        query.order.assert_called_once_with("kode_rup")
        query.range.assert_called_once_with(0, 999)

    def test_fetch_dataset_rows_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            fetch_dataset_rows(Mock(), "another_table", "2026")


if __name__ == "__main__":
    unittest.main()
