# Hyderabad GCC Job Alert System

This project checks the Hyderabad GCC company list for newly posted roles and
sends an alert so you can apply the same day.

It is built to run on GitHub Actions for free. This chat cannot host a
persistent background job, so the code needs to live in your GitHub repository
or another scheduler.

## What It Does

- Checks all 50 companies from `Hyderabad_GCC_Companies.xlsx`.
- Uses first-class careers APIs where configured:
  - Microsoft careers API
  - Amazon jobs API
  - Workday CXS APIs
  - Greenhouse, Lever, SmartRecruiters, Ashby support
- Falls back to official careers/search pages for companies without a known API.
- Filters for `Hyderabad` in title, location, or description.
- Stores seen jobs in `data/seen_jobs.json`.
- Sends only new jobs by email, with optional SMS through Twilio.
- Writes each run summary to `data/latest_report.json`.
- Runs four times per day on GitHub Actions: 08:00, 12:00, 16:00, and 20:00 IST.

## Important Accuracy Note

The reliable sources are the API sources in `companies.json`. HTML fallback
sources are best-effort because many company career pages render jobs with
JavaScript or block automated requests.

That means this system is useful immediately, but it will become much stronger
as you replace fallback pages with real company ATS/API entries. The script is
already ready for Workday, Greenhouse, Lever, SmartRecruiters, Ashby, RSS, and
plain HTML sources.

## Files

- `check_jobs.py` - main checker and notification script.
- `companies.json` - company list and career sources.
- `requirements.txt` - Python dependencies.
- `.github/workflows/daily_check.yml` - scheduled GitHub Actions workflow.
- `data/seen_jobs.json` - persistent memory of already-seen jobs.
- `data/latest_report.json` - latest run summary, created after the first run.

## GitHub Actions Setup

1. Create a GitHub repository.
2. Upload this folder into the repository.
3. Go to repository Settings -> Secrets and variables -> Actions.
4. Add these repository secrets for email alerts:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_gmail_app_password
ALERT_TO_EMAIL=your_email@gmail.com
```

For Gmail, create an app password from Google Account -> Security -> 2-Step
Verification -> App passwords. Do not use your normal Gmail password.

5. Optional: add Twilio SMS secrets:

```text
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=...
ALERT_TO_PHONE=...
```

6. Open the Actions tab and run `Daily Hyderabad Job Check` manually once.

## First Run Recommendation

If you want the first run to only initialize the database without sending all
currently open jobs, run this once from the GitHub Actions workflow by editing
the workflow command temporarily:

```bash
python check_jobs.py --baseline-only
```

Commit the generated `data/seen_jobs.json`, then change the command back to:

```bash
python check_jobs.py
```

If you prefer to receive all currently open Hyderabad jobs immediately, skip
baseline mode and run the workflow normally.

## Run Locally

```bash
python -m pip install -r requirements.txt
python check_jobs.py --no-notify
```

Set email variables locally if you want to test email delivery:

```bash
set SMTP_HOST=smtp.gmail.com
set SMTP_PORT=587
set SMTP_USER=your_email@gmail.com
set SMTP_PASS=your_app_password
set ALERT_TO_EMAIL=your_email@gmail.com
python check_jobs.py
```

PowerShell users can use:

```powershell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="your_email@gmail.com"
$env:SMTP_PASS="your_app_password"
$env:ALERT_TO_EMAIL="your_email@gmail.com"
python check_jobs.py
```

## Adding Or Improving A Company Source

Open `companies.json` and add a source to the company.

Workday example:

```json
{
  "type": "workday",
  "name": "Company Workday",
  "base_url": "https://company.wd5.myworkdayjobs.com",
  "tenant": "company",
  "site": "External"
}
```

Greenhouse example:

```json
{
  "type": "greenhouse",
  "name": "Company Greenhouse",
  "board": "company"
}
```

Lever example:

```json
{
  "type": "lever",
  "name": "Company Lever",
  "company": "company"
}
```

HTML fallback example:

```json
{
  "type": "html",
  "name": "Company careers page",
  "url": "https://company.com/careers?location=Hyderabad"
}
```

## Useful Options

```bash
python check_jobs.py --no-notify
python check_jobs.py --baseline-only
python check_jobs.py --location Hyderabad
python check_jobs.py --keyword intern
python check_jobs.py --fail-on-source-error
```

`--keyword` can be repeated if you want a stricter alert, for example:

```bash
python check_jobs.py --keyword analyst --keyword data
```

## Practical Advice

For your highest-priority companies, use their official job alert signup as a
backup too. This script gives you one central alert system, but company-owned
alerts can catch postings from pages that block scraping.
