#!/usr/bin/env python3
import os, sys, csv, argparse, requests
from datetime import datetime, timezone, timedelta

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
    return [i.strip() for i in s.split(",") if i.strip()]

def quote_if_needed(value):
    if not value:
        return '""'
    # Quote if contains spaces or reserved words
    if any(ch.isspace() for ch in value) or value.lower() in {"in","was","during","and","or","not"}:
        return f'"{value}"'
    return value

def wrap_users_for_jql(users, use_accountid=False):
    out = []
    for u in users:
        out.append(f'accountId("{u}")' if use_accountid else quote_if_needed(u))
    return ", ".join(out)

def wrap_projects_for_jql(projects):
    return ", ".join(projects)

def wrap_statuses_for_jql(statuses):
    return ", ".join([quote_if_needed(s) for s in statuses])

def parse_api_dt(s):
    # Jira dates like "2025-07-15T09:12:34.123+0000"
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)

def parse_date_ymd_utc(s, end_of_day=False):
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        return dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return dt

def overlaps(a_start, a_end, b_start, b_end):
    return (a_start <= b_end) and (b_start <= a_end)

# ---------- JQL builders ----------
def build_activity_jql(projects, date_from, date_to, inprog_statuses,
                       users=None, use_accountid=False, extra=None):
    """
    Issues in projects where:
      (status CHANGED DURING window) OR
      (status WAS IN in-progress DURING window)
    Optional: assignee WAS IN users DURING window
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

# ---------- Changelog paging (assignee history) ----------
def get_assignee_changes(session, base, issue_key):
    """
    Returns a list of assignee change entries sorted by created asc.
    Each entry: {
      'created_dt': datetime (UTC),
      'from_id': str or None,
      'from_name': str or None,
      'to_id': str or None,
      'to_name': str or None
    }
    """
    url = f"{base}/rest/api/3/issue/{issue_key}/changelog"
    start_at = 0
    max_results = 100
    changes = []
    while True:
        r = session.get(url, params={"startAt": start_at, "maxResults": max_results})
        if r.status_code != 200:
            print(f"ERROR {r.status_code} fetching changelog for {issue_key}: {r.text}", file=sys.stderr)
            break
        data = r.json()
        histories = data.get("values") or data.get("histories") or []
        for h in histories:
            created = h.get("created")
            if not created:
                continue
            created_dt = parse_api_dt(created)
            for item in h.get("items", []):
                if item.get("field") == "assignee":
                    changes.append({
                        "created_dt": created_dt,
                        "from_id": item.get("from"),
                        "from_name": item.get("fromString"),
                        "to_id": item.get("to"),
                        "to_name": item.get("toString"),
                    })
        total = data.get("total")
        if total is None:
            # Cloud format returns isLast + nextPage parameters sometimes
            if not data.get("isLast", True):
                start_at += max_results
                continue
            break
        start_at += max_results
        if start_at >= total:
            break

    changes.sort(key=lambda x: x["created_dt"])
    return changes

def compute_assignee_window_info(issue_fields, assignee_changes, window_start, window_end):
    """
    Build assignment intervals from changes and find who held the assignee field
    overlapping [window_start, window_end].
    Returns dict with:
      - assignees_during_window_display (comma string)
      - assignees_during_window_accountIds (comma string)
      - matched_assignees_during_window (set provided later)
      - first_assignee_in_window
      - last_assignee_in_window
    """
    intervals = []
    if assignee_changes:
        # Before first change: from_id/from_name is the assignee
        first = assignee_changes[0]
        intervals.append({
            "start": datetime.min.replace(tzinfo=timezone.utc),
            "end": first["created_dt"] - timedelta(microseconds=1),
            "id": first["from_id"],
            "name": first["from_name"],
        })
        # Between changes: the 'to' of i is effective until next change
        for i in range(len(assignee_changes) - 1):
            cur = assignee_changes[i]
            nxt = assignee_changes[i + 1]
            intervals.append({
                "start": cur["created_dt"],
                "end": nxt["created_dt"] - timedelta(microseconds=1),
                "id": cur["to_id"],
                "name": cur["to_name"],
            })
        # After last change: last 'to' is current until infinity
        last = assignee_changes[-1]
        intervals.append({
            "start": last["created_dt"],
            "end": datetime.max.replace(tzinfo=timezone.utc),
            "id": last["to_id"],
            "name": last["to_name"],
        })
    else:
        # No changes at all: treat current assignee as the holder for all time
        cur = (issue_fields.get("assignee") or {})
        intervals.append({
            "start": datetime.min.replace(tzinfo=timezone.utc),
            "end": datetime.max.replace(tzinfo=timezone.utc),
            "id": cur.get("accountId"),
            "name": cur.get("displayName"),
        })

    # Find overlaps with the window
    holders = []
    for iv in intervals:
        if overlaps(iv["start"], iv["end"], window_start, window_end):
            # compute actual overlap range inside window for ordering
            overlap_start = max(iv["start"], window_start)
            overlap_end = min(iv["end"], window_end)
            holders.append({
                "id": iv["id"],
                "name": iv["name"],
                "overlap_start": overlap_start,
                "overlap_end": overlap_end,
            })

    # Unique by id/name keeping order by first overlap_start
    seen = set()
    holders_sorted = sorted(holders, key=lambda h: (h["overlap_start"], h["overlap_end"]))
    unique = []
    for h in holders_sorted:
        key = (h["id"], h["name"])
        if key not in seen:
            seen.add(key)
            unique.append(h)

    assignees_display = ", ".join([h["name"] for h in unique if h["name"]])
    assignees_ids = ", ".join([h["id"] for h in unique if h["id"]])

    first_name = unique[0]["name"] if unique else ""
    last_name = unique[-1]["name"] if unique else ""

    return {
        "assignees_during_window_display": assignees_display,
        "assignees_during_window_accountIds": assignees_ids,
        "first_assignee_in_window": first_name,
        "last_assignee_in_window": last_name,
        "holders": unique,  # keep for matching step
    }

def match_holders_to_user_filter(holders, users, use_accountid):
    if not users:
        return ""
    out = []
    users_norm = [u.strip().lower() for u in users]
    for h in holders:
        if use_accountid:
            if h["id"] and h["id"].lower() in users_norm:
                out.append(h["name"] or h["id"])
        else:
            nm = (h["name"] or "").lower()
            if nm and nm in users_norm:
                out.append(h["name"])
    # preserve order, unique
    seen = set(); out_unique = []
    for x in out:
        if x not in seen:
            seen.add(x); out_unique.append(x)
    return ", ".join(out_unique)

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(
        description="Find issues by project where status changed OR was in 'in-progress' during a window; compute assignee(s) during that window."
    )
    ap.add_argument("--projects", required=True, help="Comma-separated project keys, e.g. TBR,IONG")
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--in-progress-statuses", required=True,
                    help='Comma-separated status names counted as in-progress, e.g. "In Progress,QA in progress,Code Review"')
    ap.add_argument("--users", default="", help="Optional: comma-separated names or accountIds (with --use-accountid)")
    ap.add_argument("--use-accountid", action="store_true", help="Treat --users as accountIds")
    ap.add_argument("--extra-jql", help="Optional extra JQL, e.g. 'issuetype in (Bug,Story)'")
    ap.add_argument("--csv", default="", help="If set, write results to CSV file")

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
    window_start = parse_date_ymd_utc(args.date_from, end_of_day=False)
    window_end = parse_date_ymd_utc(args.date_to, end_of_day=True)

    fields = ["key", "summary", "assignee", "status", "issuetype", "project", "updated", "created", "resolutiondate"]
    rows = []

    for it in search_issues(session, base, jql, fields=fields, batch=300):
        f = it.get("fields", {})
        assignee = f.get("assignee") or {}
        project = f.get("project") or {}

        # Pull assignee changelog and compute who held assignee during the window
        changes = get_assignee_changes(session, base, it["key"])
        win = compute_assignee_window_info(f, changes, window_start, window_end)
        matched_names = match_holders_to_user_filter(win["holders"], users, args.use_accountid) if users else ""

        row = {
            "key": it["key"],
            "summary": f.get("summary", ""),
            "project": project.get("key", ""),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "assignee_display": assignee.get("displayName") or "",           # current assignee (for reference)
            "assignee_accountId": assignee.get("accountId") or "",
            "assignees_during_window_display": win["assignees_during_window_display"],
            "assignees_during_window_accountIds": win["assignees_during_window_accountIds"],
            "matched_assignees_during_window": matched_names,
            "first_assignee_in_window": win["first_assignee_in_window"],
            "last_assignee_in_window": win["last_assignee_in_window"],
            "created": f.get("created", ""),
            "updated": f.get("updated", ""),
            "resolved": f.get("resolutiondate", "") or "",
        }
        rows.append(row)

    print(f"\nJQL:\n{jql}\n")
    print(f"Total issues: {len(rows)}\n")
    for r in rows:
        print(f"{r['key']} — {r['summary']}  "
              f"[{r['project']} / {r['type']}]  "
              f"Status: {r['status']}  "
              f"Assignee (current): {r['assignee_display']}  "
              f"Assignee(s) in window: {r['assignees_during_window_display']}")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV written: {args.csv}")

if __name__ == "__main__":
    # Env vars required:
    #   JIRA_BASE_URL  (e.g., https://your-domain.atlassian.net)
    #   JIRA_EMAIL
    #   JIRA_API_TOKEN
    main()