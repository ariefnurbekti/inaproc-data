import base64
import csv
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, call, patch

from scraper import (
    API_PATH,
    DEFAULT_SOURCE_URL,
    DEFAULT_REALISASI_SOURCE_URL,
    REALISASI_API_PATH,
    apply_filter_overrides,
    build_api_url,
    build_execution_status_report,
    build_realisasi_api_url,
    build_signed_headers,
    export_filter_reference,
    export_rows_csv,
    fetch_realisasi_rows,
    fetch_rows,
    fetch_filter_reference,
    fetch_filter_options,
    prepare_realisasi_records,
    prepare_records,
    read_rows_csv,
    sync_records,
    test_supabase_connection,
)


class ScraperTests(unittest.TestCase):
    @patch("scraper._fetch_filter_items")
    def test_fetch_filter_options_builds_dependent_query(self, mock_fetch_items):
        mock_fetch_items.return_value = [{"kode": "S1", "nama": "Satker A"}]

        options = fetch_filter_options(
            "satker",
            year="2026",
            kind_code="4",
            instansi_code="D276",
        )

        self.assertEqual(options, [{"kode": "S1", "nama": "Satker A"}])
        mock_fetch_items.assert_called_once_with(
            "/dashboard-api/profil-pengadaan/satker",
            {"tahun": "2026", "jenis": "4", "instansi": "D276"},
        )

    def test_fetch_filter_options_requires_dependent_values(self):
        with self.assertRaisesRegex(ValueError, "Year and kind"):
            fetch_filter_options("instansi", year="2026")

    def test_build_api_url_preserves_filters(self):
        url = build_api_url(DEFAULT_SOURCE_URL)
        self.assertTrue(url.startswith(f"https://data.inaproc.id{API_PATH}?"))
        self.assertIn("tahun=2026", url)
        self.assertIn("limit=100", url)
        self.assertIn("sort_by=total_nilai", url)

    def test_build_api_url_includes_cursor_for_following_page(self):
        query = parse_qs(
            urlsplit(
                build_api_url(
                    DEFAULT_SOURCE_URL, page=2, cursor="cursor token"
                )
            ).query
        )
        self.assertEqual(query["limit"], ["100"])
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["cursor"], ["cursor token"])

    def test_build_realisasi_api_url_maps_filters_and_pagination(self):
        query = parse_qs(
            urlsplit(
                build_realisasi_api_url(
                    DEFAULT_SOURCE_URL, page=2, cursor="next token"
                )
            ).query
        )
        self.assertTrue(
            build_realisasi_api_url(DEFAULT_SOURCE_URL).startswith(
                f"https://data.inaproc.id{REALISASI_API_PATH}?"
            )
        )
        self.assertEqual(query["jenis"], ["4"])
        self.assertNotIn("jenis_klpd", query)
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["cursor"], ["next token"])
        self.assertEqual(query["limit"], ["100"])

    def test_build_api_url_rejects_other_hosts(self):
        with self.assertRaises(ValueError):
            build_api_url("https://example.com/rup?tahun=2026")

    def test_filter_overrides_replace_existing_values(self):
        url = apply_filter_overrides(
            DEFAULT_SOURCE_URL,
            ["tahun=2025", "instansi=D123", "sumber_dana=APBD"],
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["tahun"], ["2025"])
        self.assertEqual(query["instansi"], ["D123"])
        self.assertEqual(query["sumber_dana"], ["APBD"])
        self.assertEqual(query["satker"], ["1.02.0.00.0.00.01.0000"])

    def test_filter_overrides_map_jenis_alias_to_source_page(self):
        rup_query = parse_qs(
            urlsplit(
                apply_filter_overrides(DEFAULT_SOURCE_URL, ["jenis=5"])
            ).query
        )
        realisasi_query = parse_qs(
            urlsplit(
                apply_filter_overrides(
                    DEFAULT_REALISASI_SOURCE_URL, ["jenis_klpd=5"]
                )
            ).query
        )
        self.assertEqual(rup_query["jenis_klpd"], ["5"])
        self.assertNotIn("jenis", rup_query)
        self.assertEqual(realisasi_query["jenis"], ["5"])
        self.assertNotIn("jenis_klpd", realisasi_query)

    def test_filter_overrides_allow_repeated_multi_select_values(self):
        url = apply_filter_overrides(
            DEFAULT_SOURCE_URL,
            ["jenis_klpd=4", "jenis_klpd=5", "sumber_dana=APBD", "sumber_dana=APBN"],
        )
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["jenis_klpd"], ["4", "5"])
        self.assertEqual(query["sumber_dana"], ["APBD", "APBN"])

    def test_filter_overrides_allow_multiple_instansi_for_both_datasets(self):
        rup_query = parse_qs(
            urlsplit(
                apply_filter_overrides(
                    DEFAULT_SOURCE_URL,
                    ["instansi=D276", "instansi=D277"],
                )
            ).query
        )
        realisasi_query = parse_qs(
            urlsplit(
                apply_filter_overrides(
                    DEFAULT_REALISASI_SOURCE_URL,
                    ["instansi=D276", "instansi=D277"],
                )
            ).query
        )

        self.assertEqual(rup_query["instansi"], ["D276", "D277"])
        self.assertEqual(realisasi_query["instansi"], ["D276", "D277"])

    def test_filter_overrides_reject_unknown_filter(self):
        with self.assertRaises(ValueError):
            apply_filter_overrides(DEFAULT_SOURCE_URL, ["unknown=value"])

    @patch("scraper._fetch_filter_items")
    def test_fetch_filter_reference_deduplicates_codes_and_keeps_context(
        self, mock_fetch_items
    ):
        def items_for(path, query=None):
            if path.endswith("/jenis"):
                return [{"kode": "1", "nama": "KEMENTERIAN"}]
            if path.endswith("/instansi"):
                return [{"kode": "K1", "nama": "KEMENTERIAN A"}]
            if path.endswith("/satker"):
                return [{"kode": "S1", "nama": "SATKER A"}]
            if path.endswith("/eselon"):
                return [{"kode": "00", "nama": "ESELON A"}]
            if path.endswith("/cara-pengadaan"):
                return [{"kode": "Penyedia", "nama": "Pengadaan Penyedia"}]
            if path.endswith("/sumber-dana"):
                return [{"kode": "APBN", "nama": "APBN"}]
            self.fail(f"Unexpected filter endpoint: {path}")

        mock_fetch_items.side_effect = items_for
        reference = fetch_filter_reference("2026")

        self.assertEqual(reference["year"], "2026")
        self.assertEqual(reference["unique_codes"]["instansi"][0]["kode"], "K1")
        self.assertEqual(reference["unique_codes"]["satker"][0]["kode"], "S1")
        self.assertEqual(
            reference["filters"]["satker_by_instansi"]["1:K1"][0]["kode"], "S1"
        )
        self.assertEqual(mock_fetch_items.call_count, 6)

    def test_export_filter_reference_writes_json(self):
        reference = {"year": "2026", "unique_codes": {"jenis_klpd": []}}
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "filters.json"
            result = export_filter_reference(reference, output_path)
            exported = json.loads(result.read_text(encoding="utf-8"))

        self.assertEqual(exported, reference)

    def test_supabase_connection_checks_both_tables(self):
        client = Mock()
        rup_table = Mock()
        realisasi_table = Mock()
        client.table.side_effect = [rup_table, realisasi_table]
        supabase_module = SimpleNamespace(
            create_client=Mock(return_value=client)
        )
        with patch.dict(sys.modules, {"supabase": supabase_module}):
            tables = test_supabase_connection(
                "https://example.supabase.co", "service-role-key"
            )

        self.assertEqual(tables, ["inaproc_rup", "inaproc_realisasi"])
        self.assertEqual(
            client.table.call_args_list,
            [call("inaproc_rup"), call("inaproc_realisasi")],
        )
        rup_table.select.assert_called_once_with("kode_rup")
        rup_table.select.return_value.limit.assert_called_once_with(1)
        rup_table.select.return_value.limit.return_value.execute.assert_called_once()
        realisasi_table.select.assert_called_once_with("kode_paket")
        realisasi_table.select.return_value.limit.assert_called_once_with(1)
        realisasi_table.select.return_value.limit.return_value.execute.assert_called_once()

    def test_signed_headers_match_dashboard_format(self):
        headers = build_signed_headers("123456789")
        self.assertEqual(headers["x-request-time"], "123456789")
        self.assertEqual(
            headers["x-secret"],
            base64.b64encode(b"987654321").decode("ascii"),
        )

    def test_prepare_records_keeps_raw_row_and_upserts_by_code(self):
        row = {"kode_rup": 43645265, "nama_paket": "Contoh paket"}
        records = prepare_records(
            [row], DEFAULT_SOURCE_URL, scraped_at="2026-10-19T00:00:00+00:00"
        )
        self.assertEqual(
            records,
            [
                {
                    "kode_rup": "43645265",
                    "data": row,
                    "source_url": DEFAULT_SOURCE_URL,
                    "scraped_at": "2026-10-19T00:00:00+00:00",
                }
            ],
        )

    def test_prepare_records_rejects_missing_code(self):
        with self.assertRaises(ValueError):
            prepare_records([{"nama_paket": "Tanpa kode"}], DEFAULT_SOURCE_URL)

    def test_prepare_realisasi_records_keeps_status_and_provider_data(self):
        row = {
            "kode_paket": "PKT-1",
            "kode_rup": "RUP-1",
            "status_paket": "ON PROCESS",
            "nama_penyedia": "Penyedia A",
        }
        records = prepare_realisasi_records(
            [row], DEFAULT_REALISASI_SOURCE_URL, scraped_at="2026-10-19T00:00:00+00:00"
        )
        self.assertEqual(records[0]["kode_paket"], "PKT-1")
        self.assertEqual(records[0]["kode_rup"], "RUP-1")
        self.assertEqual(records[0]["data"], row)

    def test_prepare_realisasi_records_rejects_missing_package_code(self):
        with self.assertRaises(ValueError):
            prepare_realisasi_records([{"kode_rup": "RUP-1"}], DEFAULT_REALISASI_SOURCE_URL)

    def test_build_execution_status_report_compares_rup_codes(self):
        rup_rows = [
            {"kode_rup": 1, "nama_paket": "Ada kontrak"},
            {"kode_rup": 2, "nama_paket": "Belum kontrak"},
        ]
        realization_rows = [
            {
                "kode_rup": "1",
                "kode_paket": "PKT-1",
                "status_paket": "ON PROCESS",
                "tahapan_pengadaan": "Kontrak",
                "nama_penyedia": "Penyedia A",
            },
            {
                "kode_rup": 1,
                "kode_paket": "PKT-2",
                "status_paket": "SELESAI",
                "tahapan_pengadaan": "Kontrak",
                "nama_penyedia": "Penyedia B",
            },
        ]

        report = build_execution_status_report(rup_rows, realization_rows)

        self.assertEqual(report[0]["status_pelaksanaan"], "ADA_REKAMAN_REALISASI")
        self.assertEqual(report[0]["jumlah_paket_realisasi"], 2)
        self.assertEqual(report[0]["status_paket_realisasi"], "ON PROCESS; SELESAI")
        self.assertEqual(
            report[0]["nama_penyedia_realisasi"], "Penyedia A; Penyedia B"
        )
        self.assertEqual(
            report[1]["status_pelaksanaan"], "BELUM_ADA_REKAMAN_REALISASI"
        )

    def test_export_rows_csv_writes_all_columns_and_rows(self):
        rows = [
            {"kode_rup": "1", "nama_paket": "Paket, satu", "metadata": {"a": 1}},
            {"kode_rup": "2", "sumber_dana": "APBDP"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "nested" / "rup.csv"
            result = export_rows_csv(rows, output_path)
            with result.open(encoding="utf-8-sig", newline="") as csv_file:
                exported = list(csv.DictReader(csv_file))

        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[0]["nama_paket"], "Paket, satu")
        self.assertEqual(exported[0]["metadata"], '{"a": 1}')
        self.assertEqual(exported[1]["sumber_dana"], "APBDP")
        self.assertEqual(exported[0]["sumber_dana"], "")

    def test_read_rows_csv_restores_json_fields_and_checks_code_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "rup.csv"
            input_path.write_text(
                'kode_rup,nama_paket,metadata\n'
                '123,Paket,"{""sumber"": ""APBD""}"\n',
                encoding="utf-8-sig",
            )
            rows = read_rows_csv(input_path, "kode_rup")

            self.assertEqual(
                rows,
                [
                    {
                        "kode_rup": "123",
                        "nama_paket": "Paket",
                        "metadata": {"sumber": "APBD"},
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "kode_paket"):
                read_rows_csv(input_path, "kode_paket")

    def test_sync_records_upserts_batches_to_dataset_table(self):
        client = Mock()
        records = [{"kode_rup": str(index)} for index in range(501)]

        saved_count = sync_records(client, "rup", records)

        self.assertEqual(saved_count, 501)
        self.assertEqual(client.table.call_count, 2)
        client.table.assert_called_with("inaproc_rup")
        calls = client.table.return_value.upsert.call_args_list
        self.assertEqual(len(calls[0].args[0]), 500)
        self.assertEqual(len(calls[1].args[0]), 1)
        self.assertEqual(calls[0].kwargs["on_conflict"], "kode_rup")

    @patch("scraper.urlopen")
    def test_fetch_rows_follows_cursors_until_last_page(self, mock_urlopen):
        responses = [
            {
                "tableRows": [{"kode_rup": "1"}],
                "tablePagination": {"hasMore": True, "nextCursor": "next-page"},
            },
            {
                "tableRows": [{"kode_rup": "2"}],
                "tablePagination": {"hasMore": False, "nextCursor": ""},
            },
        ]

        def response_for_request(request, timeout):
            payload = responses.pop(0)
            return BytesIO(json.dumps(payload).encode("utf-8"))

        mock_urlopen.side_effect = response_for_request
        rows = fetch_rows(DEFAULT_SOURCE_URL)

        self.assertEqual([row["kode_rup"] for row in rows], ["1", "2"])
        self.assertEqual(mock_urlopen.call_count, 2)
        second_request = mock_urlopen.call_args_list[1].args[0]
        second_query = parse_qs(urlsplit(second_request.full_url).query)
        self.assertEqual(second_query["page"], ["2"])
        self.assertEqual(second_query["cursor"], ["next-page"])

    @patch("scraper.urlopen")
    def test_fetch_realisasi_rows_follows_cursor(self, mock_urlopen):
        responses = [
            {
                "tableRows": [{"kode_paket": "PKT-1", "status_paket": "ON PROCESS"}],
                "tablePagination": {"hasMore": True, "nextCursor": "next-page"},
            },
            {
                "tableRows": [{"kode_paket": "PKT-2", "nama_penyedia": "Penyedia"}],
                "tablePagination": {"hasMore": False, "nextCursor": ""},
            },
        ]

        def response_for_request(request, timeout):
            return BytesIO(json.dumps(responses.pop(0)).encode("utf-8"))

        mock_urlopen.side_effect = response_for_request
        rows = fetch_realisasi_rows(DEFAULT_REALISASI_SOURCE_URL)

        self.assertEqual([row["kode_paket"] for row in rows], ["PKT-1", "PKT-2"])
        self.assertEqual(mock_urlopen.call_count, 2)
        second_query = parse_qs(
            urlsplit(mock_urlopen.call_args_list[1].args[0].full_url).query
        )
        self.assertEqual(second_query["page"], ["2"])
        self.assertEqual(second_query["cursor"], ["next-page"])


if __name__ == "__main__":
    unittest.main()
