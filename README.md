# LocalLedger OCR

Turn batches of PDF invoices and credit notes into a ready-to-import upload sheet for
**Sage 50, Xero, QuickBooks, FreeAgent** or any custom layout. Everything runs on your own
machine; no cloud APIs and no data leaves your laptop.

- Reads digital PDFs instantly; scanned ones via Tesseract OCR; messy layouts via an optional local LLM
- Matches suppliers/clients to your master list, and remembers your corrections
- Checks the arithmetic (Net + VAT = Gross, VAT rate vs code) and scores every row HIGH / MEDIUM / LOW
- Catches duplicates three ways (same file, same invoice number, same amount ± 3 days)
- Review each document side by side with the original PDF; split invoices across nominal codes
- Exports in your accounting package's format and files the PDFs into `Archive/YYYY-MM/`
- Monthly dashboard: revenue vs costs, nominal trends, spend alerts, VAT position, top suppliers

## Quick start

Get the code:

```bash
git clone https://github.com/grack126/LocalLedger.git
cd LocalLedger
```

(No git? Use **Code › Download ZIP** on GitHub and unzip it.) Then start the app:

**Windows:** double-click **`run.bat`**
**macOS / Linux:** in a terminal, run **`./run.sh`** (or `bash run.sh`)

The first run creates a private Python environment in `.venv`, installs the dependencies
(a few minutes; about 500 MB on disk, mostly pandas, PyArrow and Streamlit), creates sample data, and opens the app in your browser. Later runs
start in seconds. Keep the window open while you work; close it (or press Ctrl+C) to stop.

You need **Python 3.11+** ([python.org](https://www.python.org/downloads/); on Windows tick
*"Add python.exe to PATH"*). If you use [uv](https://docs.astral.sh/uv/) instead, the launcher
will find it and set Python up for you.

### Try it with the sample data

The first run writes `sample_data/` (re-create it any time with `python generate_sample_data.py`):

1. **Clients & Suppliers › Import** — choose `sample_data/master_list.csv`
2. **Settings › Business** — set your company to *Northwind Trading Ltd* (the sample invoices' customer)
3. **Upload & Review** — drop the four PDFs from `sample_data/invoices/` (or use *Import a folder*)

| Sample | What you should see |
|---|---|
| `01_ACME_clean_invoice.pdf` | 🟢 HIGH — matched to ACME001 by VAT number, nominal/VAT codes from the master list, description `Sep 2026 - Office rent… ACME Property Services Ltd` |
| `02_BOLT_messy_math_error.pdf` | 🔴 LOW — 500.00 + 100.00 ≠ 650.00, amounts shown in red; export is blocked until fixed |
| `03_GLOBEX_credit_note_USD.pdf` | Flipped to **PC** (purchase credit), USD with the ECB exchange rate for its date |
| `04_ACME_invoice_resent.pdf` | ⚠ Duplicate — byte-identical to 01 under another name; dismiss or remove it to continue |

To see the **Analytics** page populated, load six months of demo history:

```bash
python generate_sample_data.py --load-demo     # adds the master list + demo history
python generate_sample_data.py --remove-demo   # removes the demo history again
```

(Use the Python inside `.venv` — `.venv\Scripts\python` on Windows, `.venv/bin/python` elsewhere.)

## Pages

| Page | What it does |
|---|---|
| **Upload & Review** | Drop PDFs (or import a folder), review each row beside the original PDF, fix fields (red = error, amber = low confidence), save corrections as supplier defaults, split invoices across nominal/VAT codes, add manual rows, resolve duplicate flags, then export |
| **History** | Every exported document; void one to drop it from analytics and duplicate checks |
| **Analytics** | Revenue vs costs, nominal breakdown with month-on-month variance, run-rate alerts, VAT liability, top suppliers/clients — by invoice date or service period |
| **Clients & Suppliers** | Import your master list (CSV/Excel), edit defaults, see learned aliases |
| **Export Templates** | Built-in Sage 50 / Xero / QuickBooks / FreeAgent presets; duplicate one to customise columns, formats and value maps |
| **Description Rules** | Token rules for the Description column with a live preview |
| **Settings** | Your company, OCR/LLM engines, validation thresholds, archive folder and naming, exchange rates, VAT codes, backups |

The staging batch is saved in SQLite as you work, so closing the browser loses nothing.

## Optional engines

The app works with digital (text-based) PDFs out of the box. Two optional extras help with harder documents:

| Engine | What it adds | Install |
|---|---|---|
| **Tesseract OCR** | Reads scanned / photographed invoices | Windows: [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) · macOS: `brew install tesseract` · Linux: `sudo apt install tesseract-ocr` |
| **Local LLM** (Ollama, LM Studio, llama.cpp) | Fills fields the regex parser misses on messy layouts | e.g. `ollama pull llama3.1:8b`, then enable it in Settings |

The LLM runs in *fallback* mode by default: it is only called when the regex parser cannot find the
invoice number, date, supplier, or totals that add up. LLM amounts are accepted only when they
reconcile **and** appear in the document text, so a supplier's genuine math error is still flagged.
Endpoints outside your machine or local network are refused unless you explicitly allow them.

Exchange rates come from [Frankfurter](https://frankfurter.dev) (European Central Bank reference rates,
free, no API key). Each rate is cached in SQLite after the first lookup, so it works offline from
then on. Manual rates override fetched ones.

## Privacy

- All data — master list, history, settings, the current batch — lives in one SQLite file, `data/localledger.db`.
- The app listens on `localhost` only, and Streamlit's usage statistics are switched off (`.streamlit/config.toml`).
- The only network calls are the exchange-rate lookup (currency code + date, no document data; can be
  turned off) and, if you enable it, your own local LLM.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Python 3.11 or newer is required" | Install Python from python.org (tick *Add to PATH*), then run the launcher again |
| `./run.sh: Permission denied` | `chmod +x run.sh`, or run `bash run.sh` |
| Linux: "Could not create the virtual environment" | `sudo apt install python3-venv` (or `python3.12-venv`) |
| Something went wrong during the first install | Delete the `.venv` folder and run the launcher again, or run it with `--reinstall` |
| Port 8501 is busy | The launcher picks the next free port automatically; or `run.bat --port 8600` |
| Scanned PDFs show "OCR unavailable" | Install Tesseract (see above); set its path in Settings › Extraction if it isn't on PATH |
| Use a different data folder (e.g. a synced drive) | Set `LOCALLEDGER_DB` (database file) or `LOCALLEDGER_DATA_DIR` before launching |

## Project layout

```
LocalLedger/
├── run.bat / run.sh               # one-click launchers (find Python 3.11+, create .venv, start)
├── launcher.py                    # installs requirements when they change, first-run samples, starts Streamlit
├── app.py                         # Streamlit entry point (multi-page navigation)
├── generate_sample_data.py        # sample master CSVs + 4 demo PDFs (+ optional demo history)
├── requirements.txt / requirements-dev.txt / pyproject.toml
├── .streamlit/config.toml         # localhost only, no telemetry
├── localledger/
│   ├── config.py                  # paths (override with LOCALLEDGER_DATA_DIR / LOCALLEDGER_DB)
│   ├── schema.sql                 # SQLite schema (v3) + analytics view
│   ├── models.py                  # StagingRow, DocType, money/date parsing, normalisers
│   ├── db.py                      # persistence, migrations, entity resolution, vendor memory, history
│   ├── rules.py                   # templates/presets, description builder, credit notes, file naming
│   ├── validator.py               # math checks, confidence scoring, 3-tier duplicates
│   ├── extraction/
│   │   ├── pipeline.py            # native text -> OCR fallback -> optional local LLM -> StagingRow
│   │   ├── native.py              # pdfplumber / PyMuPDF text + page rendering for the preview
│   │   ├── ocr.py                 # Tesseract / EasyOCR (optional)
│   │   ├── llm.py                 # Ollama / LM Studio / any OpenAI-compatible *local* endpoint
│   │   ├── parsers.py             # regex fields; totals picked where Net + VAT = Gross
│   │   └── base.py                # shared dataclasses
│   ├── fx.py                      # Frankfurter (ECB) rates, SQLite cache, manual + offline fallback
│   ├── staging.py                 # batch persistence, split/merge, manual rows, edits
│   ├── exporter.py                # upload sheet + archive/rename + history logging (with rollback)
│   └── analytics.py               # monthly KPIs, MoM variance, run-rate alerts, VAT summary, Plotly figures
├── ui/                            # Streamlit pages
│   ├── staging.py                 # Upload & Review: grid + split-screen PDF review + export
│   ├── common.py                  # shared helpers (DB handle, batch state, formatting)
│   └── history.py · analytics.py · entities.py · templates.py · rules_editor.py · settings.py
├── tests/                         # pytest suite (incl. headless Streamlit tests)
├── sample_data/                   # generated on first run (git-ignored)
└── data/                          # created at runtime (git-ignored)
    ├── localledger.db
    ├── inbox/  exports/  backups/  Archive/YYYY-MM/
```

## Key conventions

| Topic | Convention |
|---|---|
| Money in SQLite | Integer minor units (pence) |
| Credit notes | Amounts stored **positive**; `PC`/`SC` type carries the sign. Templates choose `positive` (Sage) or `negative_for_credits` (Xero/FreeAgent) on export |
| Exchange rate | Units of document currency per 1 base unit (`1 GBP = 1.27 USD` → `1.27`); base = amount ÷ rate |
| Confidence | 40 % math · 25 % entity · 25 % completeness · 10 % clarity. Math failure → LOW (< 50) |
| Duplicates | T1 file SHA-256 · T2 account + invoice no. · T3 account + gross ± 3 days |

## Development

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
streamlit run app.py
```

`launcher.py --check` installs and verifies everything without starting the server (handy in CI).

Built-in presets follow each vendor's published import template. Import formats change
between versions, so duplicate a preset and adjust it if your software differs.

## License

MIT — see [LICENSE](LICENSE).
