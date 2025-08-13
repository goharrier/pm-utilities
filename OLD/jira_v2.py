#!/usr/bin/env python3
import os, sys, csv, argparse, requests

# ---------- Env helpers ----------
def env(name):
    v = os.getenv(name)
    if not v:
        print(f"ERROR: env var {name} is required", file=sys.stderr); sys.exit(1)
    return v

def jira_session():
    s = requests.Session()
    s.auth = (env("JIRA_EMAIL"), env("JIRA_API_TOKEN"))
    s.headers.update({"Accept": "application/json"})
    base = env("JIRA_BASE_URL").rstrip("/")
    return s, base

# ---------- Utilities ----------
def csv_list(s):
    # split on commas but allow quoted items; keep simple for typical input
    return [i.strip() for i in s.split(",") if i.strip()]

def quote_if_needed(value):
    # Quote if contains spaces or special chars
    if any(ch.isspace() for ch in value) or value.lower() in {"in","was","during","and","or","not"}:
        return f'"{value}"'
    return value

def wrap_users_for_jql(users, use_accountid=False):
    out = []
    for u in users:
        if use_accountid:
            out.append(f'accountId("{u}")')
        else:
            out.append(quote_if_needed(u))
    return ", ".join(out)

def wrap_projects_for_jql(projects):
    return ", ".join(projects)

def wrap_statuses_for_jql(statuses):
    return ", ".join([quote_if_needed(s) for s in statuses])

# ---------- JQL builders ----------
def build_activity_jql(projects, date_from, date_to, inprog_statuses,
                       users=None, use_accountid=False, extra=None):
    """
    Select issues in any of the projects where:
      (status CHANGED DURING window) OR
      (status WAS IN (<in-progress-statuses>) DURING window)
    Optionally also require that assignee WAS IN (users) DURING window.
    """
    project_clause = f"project in ({wrap_projects_for_jql(projects)})"
    window = f'("{date_from}", "{date_to}")'

    status_changed = f"status CHANGED DURING {window}"
    in_progress_was = f"status WAS IN ({wrap_statuses_for_jql(inprog_statuses)}) DURING {window}"

    activity_clause = f"({status_changed} OR {in_progress_was})"

    clauses = [project_clause, activity_clause]

    if users:
        users_expr = wrap_users_for_jql(users, use_accountid=use_accountid)
        clauses.append(f"assignee WAS IN ({users_expr}) DURING {window}")

    if extra:
        clauses.append(f"({extra})")

    return " AND ".join(clauses) + " ORDER BY updated DESC"

# ---------- Enhanced search (new API) ----------
def search_issues(session, base, jql, fields=None, batch=200):
    """
    POST /rest/api/3/search/jql with pagination via nextPageToken.
    """
    url = f"{base}/rest/api/3/search/jql"
    next_token = None
    while True:
        body = {"jql": jql, "maxResults": min(batch, 1000)}
        if fields:
            body["fields"] = fields
        if next_token:
            body["nextPageToken"] = next_token

        r = session.post(url, json=body)
        if r.status_code != 200:
            print(f"ERROR {r.status_code} from enhanced search: {r.text}", file=sys.stderr)
            sys.exit(2)

        data = r.json()
        for issue in data.get("issues", []):
            yield issue

        if data.get("isLast", False):
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(
        description="Find issues by project where status changed OR was in 'in-progress' during a window, optionally for specific assignees."
    )
    ap.add_argument("--projects", required=True,
                    help="Comma-separated project keys, e.g. TBR,IONG")
    ap.add_argument("--from", dest="date_from", required=True, help='YYYY-MM-DD (inclusive)')
    ap.add_argument("--to", dest="date_to", required=True, help='YYYY-MM-DD (inclusive)')

    ap.add_argument("--in-progress-statuses", required=True,
                    help='Comma-separated status names considered "in progress", e.g. "In Progress,QA in progress,Code Review"')

    ap.add_argument("--users", default="",
                    help="Optional: comma-separated users (display names/usernames) or accountIds (with --use-accountid).")
    ap.add_argument("--use-accountid", action="store_true",
                    help="Treat --users as accountIds (uses accountId(\"...\") in JQL).")

    ap.add_argument("--extra-jql", help="Optional extra JQL, e.g. 'issuetype in (Bug,Story)'")
    ap.add_argument("--csv", default="", help="If set (e.g. output.csv), writes results to CSV.")

    args = ap.parse_args()

    projects = csv_list(args.projects)
    if not projects:
        print("ERROR: --projects cannot be empty", file=sys.stderr); sys.exit(1)

    inprog_statuses = csv_list(args.in_progress_statuses)
    if not inprog_statuses:
        print("ERROR: --in-progress-statuses cannot be empty", file=sys.stderr); sys.exit(1)

    users = csv_list(args.users) if args.users else None

    jql = build_activity_jql(
        projects=projects,
        date_from=args.date_from,
        date_to=args.date_to,
        inprog_statuses=inprog_statuses,
        users=users,
        use_accountid=args.use_accountid,
        extra=args.extra_jql,
    )

    session, base = jira_session()

    fields = ["key", "summary", "assignee", "status", "issuetype", "project", "updated", "created", "resolutiondate"]
    rows = []
    for it in search_issues(session, base, jql, fields=fields, batch=300):
        f = it.get("fields", {})
        assignee = f.get("assignee") or {}
        project = f.get("project") or {}
        rows.append({
            "key": it["key"],
            "summary": f.get("summary", ""),
            "project": project.get("key", ""),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "assignee_display": assignee.get("displayName") or "",
            "assignee_accountId": assignee.get("accountId") or "",
            "created": f.get("created", ""),
            "updated": f.get("updated", ""),
            "resolved": f.get("resolutiondate", "") or "",
        })

    print(f"\nJQL:\n{jql}\n")
    print(f"Total issues: {len(rows)}\n")
    for r in rows:
        print(f"{r['key']} — {r['summary']}  "
              f"[{r['project']} / {r['type']}]  "
              f"Status: {r['status']}  "
              f"Assignee: {r['assignee_display']}  "
              f"Updated: {r['updated']}")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV written: {args.csv}")

if __name__ == "__main__":
    # Requires env vars:
    #   JIRA_BASE_URL  (e.g., https://your-domain.atlassian.net)
    #   JIRA_EMAIL
    #   JIRA_API_TOKEN
    main()