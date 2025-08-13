# pm-utilities

A collection of lightweight Python utilities to aid in **project** and **product management** activities.  
The goal is to make common reporting and analysis tasks easier without heavy tooling. Current scripts focus on **Jira** data extraction, transformation, and reporting.

---

## Contents

### `jira_issues_workedOn.py`
Retrieve all Jira issues **worked on** by a specific set of people in a date window (typically a month). Outputs a count, a readable list, and (optionally) a CSV file.

**Highlights**
- Filter by **users** (display names or accountIds)
- Filter by **date range** (`--from` / `--to`, inclusive)
- Optional **project** filter (`--project` supports comma-separated keys)
- Optional **extra JQL** for fine-grained constraints
- Writes an optional **CSV** for further analysis

---

## Requirements

- Python **3.9+**
- [`requests`](https://pypi.org/project/requests/) library
- Jira Cloud (API token) or Jira Server/DC (credentials) with permission to view the issues

---

## Installation

1. **Clone the repo**
   ```bash
   git clone git@github.com:goharrier/pm-utilities.git
   cd pm-utilities
   ```

2. **Create and activate a virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   python -m pip install --upgrade pip
   python -m pip install requests
   ```

---

## Jira API setup

1. **Create an API token (Jira Cloud)**
   - https://id.atlassian.com/manage/api-tokens → **Create API token**, give it a label, copy it.

2. **Set environment variables**
   ```bash
   export JIRA_BASE_URL="https://your-domain.atlassian.net"
   export JIRA_EMAIL="your.email@example.com"
   export JIRA_API_TOKEN="your_api_token_here"
   ```
   > To make these permanent, add the above lines to `~/.zshrc` (macOS) or `~/.bashrc`, then `source` the file.

---

## Usage — `jira_issues_workedOn.py`

**Basic example (names):**
```bash
python jira_issues_workedOn.py   --users "Jane Doe,John Smith"   --from 2025-07-01 --to 2025-07-31   --project TBR   --csv july_worked_on.csv
```

**Using accountIds:**
```bash
python jira_issues_workedOn.py   --users "5f8a1...abc,60b7c...def"   --use-accountid   --from 2025-07-01 --to 2025-07-31   --csv july_worked_on.csv
```

**Multiple projects:**
```bash
python jira_issues_workedOn.py   --users "Jane Doe"   --from 2025-07-01 --to 2025-07-31   --project "TBR,IONG,OPS"   --csv july_multi_projects.csv
```

**Extra JQL guard (example):**
```bash
--extra-jql "statusCategory != Done AND issuetype in (Bug, Story)"
```

---

## Arguments

| Argument          | Required | Description |
|-------------------|----------|-------------|
| `--users`         | ✅       | Comma-separated list of display names, or accountIds when using `--use-accountid`. |
| `--from`          | ✅       | Start date `YYYY-MM-DD` (inclusive). |
| `--to`            | ✅       | End date `YYYY-MM-DD` (inclusive). |
| `--project`       | ❌       | One or more project keys, comma-separated (e.g., `TBR,IONG`). |
| `--extra-jql`     | ❌       | Extra JQL appended to the query (wrap in quotes). |
| `--use-accountid` | ❌       | Treat `--users` values as accountIds. |
| `--csv`           | ❌       | Path to write results as CSV (e.g., `july_worked_on.csv`). |

---

## Output

**Console**
- Prints the effective JQL.
- Total matching issues.
- Per-issue line: `KEY — Summary — [optional fields]`.

**CSV (if `--csv` provided)**
- Columns typically include: `key`, `summary`, `assignee`, `status`, `type`, `created`, `updated` (may vary by script version).

---

## Tips

- If names are masked in Jira Cloud (privacy settings), use **accountIds** with `--use-accountid`.
- If you see auth errors, verify `JIRA_EMAIL` + `JIRA_API_TOKEN` and that your user can view the projects.
- Prefer running in a **virtual environment** to avoid system Python restrictions (PEP 668).

---

## Contributing

This repo is intentionally small and practical. PRs are welcome for:
- Additional Jira utilities (status/throughput reporting, epic rollups, release notes)
- CSV/Excel helpers
- PM/PO automation scripts
