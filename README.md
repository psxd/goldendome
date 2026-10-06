# Congressional News Scraper & Stance Classifier

An automated pipeline that searches Google News RSS for legislative news concerning Congressional members, analyzes stance and context using a local **Ollama** model (`gemma3:1b`), and updates a **Google Sheet** via a deployed Apps Script Web App.

---

## 📌 Features

- **Keyword Matching:** Filters news related to missile defense, spectrum auctions, and radar interference.
- **Local AI Analysis:** Evaluates articles using `gemma3:1b` via Ollama for stance (*Support*, *Oppose*, *Neutral*) and generates keyword summaries.
- **Google Sheets Integration:** Dynamically appends non-duplicate 5-column source blocks (`Date`, `Name`, `Stance`, `Summary`, `URL`).
- **Rate-Limit Safe:** Uses configurable pauses and an automated backoff schedule ($1\text{ min} \rightarrow 5\text{ mins} \rightarrow 10\text{ mins}$) when encountering rate limits.
- **GitHub Actions Workflow:** Automatically processes legislators in batches of 20 and recursively triggers subsequent batches.

---

## 🛠️ Project Structure

```text
.
├── .github/
│   └── workflows/
│       └── congress_scraper.yml    # GitHub Actions workflow file
├── sct2.py                         # Python scraper & Ollama analyzer
└── requirements.txt                # Python dependencies