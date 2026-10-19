# INAPROC RUP and Realisasi scraper

Scraper ini mengambil data dari endpoint JSON yang digunakan halaman Data
INAPROC, lalu melakukan upsert ke tabel Supabase berdasarkan `kode_rup`.
Scraper meminta 100 baris per halaman (batas yang didukung endpoint), mengikuti
cursor ke semua halaman sampai habis, lalu menyimpan data API lengkap sebagai
JSON di kolom `data`. Setiap halaman diambil berurutan karena cursor halaman
berikutnya baru tersedia dari respons halaman sebelumnya.

Selain daftar rencana, scraper juga dapat mengambil data pada halaman
Realisasi, termasuk `status_paket`, `tahapan_pengadaan`, `nama_penyedia`,
`kode_paket`, dan nilai transaksi.

## Persiapan

1. Di Supabase SQL Editor, jalankan isi [`schema.sql`](./schema.sql).
2. Buat virtual environment dan pasang dependensi:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   py -m pip install -r requirements.txt
   ```

3. Salin `.env.example` menjadi `.env`, lalu isi `SUPABASE_URL` dan
   `SUPABASE_SERVICE_ROLE_KEY` dari pengaturan proyek Supabase. Jangan
   membagikan service role key di chat atau memasukkannya ke Git.
4. Uji koneksi dan akses tabel tanpa mengambil data INAPROC:

   ```powershell
   py scraper.py --test-supabase
   ```

   Perintah ini memeriksa kredensial serta akses baca ke tabel `inaproc_rup`
   dan `inaproc_realisasi`. Jika tabel belum ada, jalankan `schema.sql` di
   Supabase SQL Editor terlebih dahulu.
5. Jalankan scraper:

   ```powershell
   py scraper.py
   ```

Untuk mengambil dan memeriksa hasil terlebih dahulu tanpa Supabase, ekspor ke
CSV yang dapat dibuka dengan Excel:

```powershell
py scraper.py --export-csv
```

Secara bawaan, file hasil berada di `exports\inaproc_rup.csv`. Lokasi khusus
juga dapat diberikan, misalnya `py scraper.py --export-csv data\rup.csv`.
Mode ekspor hanya mengambil dan menulis file; mode ini tidak mengirim data ke
Supabase.

Untuk mengirim CSV hasil ekspor yang sudah ada ke Supabase tanpa mengambil
ulang data dari INAPROC:

```powershell
py scraper.py --sync-csv
py scraper.py --dataset realisasi --sync-csv
```

Perintah tersebut menggunakan `exports\inaproc_rup.csv` atau
`exports\inaproc_realisasi.csv` sesuai dataset. Path CSV khusus juga dapat
diberikan, misalnya `py scraper.py --dataset realisasi --sync-csv data\realisasi.csv`.
CSV status perbandingan bukan dataset dasar dan tidak dapat diimpor dengan opsi
ini. Data dimasukkan sebagai JSON di kolom `data`, lalu di-upsert berdasarkan
`kode_rup` atau `kode_paket`; source URL yang tersimpan berasal dari
`SOURCE_URL` atau URL bawaan dataset.

Filter juga dapat diatur langsung saat menjalankan scraper. `--filter KEY=VALUE`
dapat diulang; nilai dari opsi akan menggantikan filter dengan kunci yang sama
dari URL bawaan atau `SOURCE_URL` di `.env`:

```powershell
py scraper.py --export-csv --filter tahun=2026 --filter jenis_klpd=4 --filter instansi=D276 --filter satker=1.02.0.00.0.00.01.0000 --filter sumber_dana=APBDP
```

Filter yang tersedia mencakup `tahun`, `jenis`/`jenis_klpd`, `instansi`,
`eselon`, `satker`, `sumber`, `sumber_dana`, `metode_pengadaan`,
`status_paket`, `search_kode`, `search_paket`, `search_penyedia`,
`kd_penyedia`, `filter_metode`, `filter_jenis`, `filter_pdn`, `view_table`,
`search_rup`, `search_q`, `sort_by`, dan `sort_dir`. Untuk filter multi-pilih,
ulangi opsinya, misalnya
`--filter "status_paket=ON PROCESS" --filter status_paket=SELESAI`.

## UI web melalui GitHub dan Streamlit

`app.py` menyediakan UI web dengan pilihan dataset dan dropdown yang dapat
dicari. Pilihan jenis, instansi, eselon, dan satker dimuat bertahap dari API
INAPROC. Sumber dana dan cara pengadaan juga berupa pilihan. Status paket dan
filter pencarian yang nilainya bebas diisi pada bagian **Filter tambahan dan
pencarian**.

GitHub menyimpan source code; Streamlit Community Cloud menjalankan aplikasi
Python dan UI. GitHub Pages saja tidak dapat menjalankan scraper Python atau
menyimpan kredensial server secara aman.

Untuk menjalankan lokal:

```powershell
py -m pip install -r requirements.txt
py -m streamlit run app.py
```

Untuk deploy:

1. Push source code ke repository GitHub. Jangan push `.env` atau file
   `.streamlit/secrets.toml`.
2. Buat aplikasi di Streamlit Community Cloud dan hubungkan ke repository,
   pilih branch, lalu set **Main file path** ke `app.py`.
3. Di pengaturan aplikasi Streamlit, isi **Secrets**:

   ```toml
   SUPABASE_URL = "https://<project-ref>.supabase.co"
   SUPABASE_SERVICE_ROLE_KEY = "sb_secret_..."
   ```

4. Deploy atau restart aplikasi. Pilih dataset, atur dropdown filter, lalu
   klik **Ambil data dan simpan ke Supabase**. Hasil bisa diunduh sebagai CSV.

Filter jenis instansi, instansi, satker, eselon, sumber dana, dan cara
pengadaan menggunakan pilihan kode/nama dari API INAPROC. Beberapa filter
pencarian atau status yang nilainya ditentukan sendiri tetap berupa input teks.
Pilih instansi atau satker untuk menghindari permintaan data yang terlalu luas.
Secret Supabase hanya disimpan di Streamlit Secrets; jangan masukkan ke kode,
repository, atau pesan chat.

## Data Realisasi

Ekspor transaksi Realisasi ke CSV untuk diperiksa:

```powershell
py scraper.py --dataset realisasi --export-csv
```

Secara bawaan file tersimpan di `exports\inaproc_realisasi.csv`; filter default
mengikuti tautan Realisasi pada task ini. Filter bisa diubah dengan `--filter`,
misalnya:

```powershell
py scraper.py --dataset realisasi --export-csv --filter tahun=2026 --filter jenis=4 --filter instansi=D276 --filter satker=1.02.0.00.0.00.01.0000 --filter sumber_dana=APBDP
```

Untuk membandingkan paket RUP dengan data yang tercatat di Realisasi dan
menghasilkan CSV status per RUP:

```powershell
py scraper.py --compare-realisasi
```

Laporan bawaan tersimpan di `exports\inaproc_status_pelaksanaan.csv`. Baris
`ADA_REKAMAN_REALISASI` mempunyai satu atau lebih kode paket Realisasi, status,
tahapan pengadaan, dan penyedia. Baris `BELUM_ADA_REKAMAN_REALISASI` berarti
tidak ditemukan paket yang cocok berdasarkan `kode_rup` pada hasil Realisasi;
ini bukan bukti mutlak bahwa pekerjaan belum dilakukan, karena pencatatan atau
cakupan sumber Realisasi dapat berbeda. Kolom status dan penyedia Realisasi
dibiarkan kosong jika tidak ada data pasangannya.

Untuk menyimpan daftar kode pilihan filter yang tersedia di situs sebagai
referensi JSON:

```powershell
py scraper.py --export-filter-reference
```

Secara bawaan daftar disimpan di
`reference\inaproc_filter_codes_2026.json`. File mencantumkan kode dan nama
jenis instansi, instansi, eselon, satuan kerja, cara pengadaan, dan sumber dana.
Daftar instansi, eselon, dan satker disertai konteks induknya karena pilihan
tersebut bergantung pada jenis instansi atau instansi yang dipilih. Tahun
anggaran yang tersedia di dropdown juga disertakan. Untuk mengambil opsi tahun
lain, misalnya:

```powershell
py scraper.py --export-filter-reference --reference-year 2025 reference\inaproc_filter_codes_2025.json
```

Scraper mengambil seluruh halaman yang cocok dengan filter tersebut, 100 baris
per permintaan secara bawaan. Ukuran halaman dapat diatur dengan
`--page-size` (maksimal 100). Untuk seluruh data tanpa filter, API RUP menunjukkan
lebih dari 4,5 juta paket bahkan pada tahun default 2026; gunakan filter agar
proses dan file hasil tetap terkendali. Setiap baris diperbarui berdasarkan
`kode_rup` untuk RUP atau `kode_paket` untuk Realisasi, sehingga menjalankan
scraper berulang kali tidak membuat duplikat. `schema.sql` menyiapkan tabel
Supabase terpisah `inaproc_rup` dan `inaproc_realisasi`.

## Pengujian

```powershell
py -m unittest discover -s tests -v
```
