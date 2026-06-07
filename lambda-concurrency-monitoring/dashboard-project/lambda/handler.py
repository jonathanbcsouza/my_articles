"""CloudWatch custom-widget Lambda for the Lambda concurrency dashboard.

The function is action-routed based on the incoming event:

- ``describe``                -> returns markdown documentation for the widget
- ``render`` (default)        -> returns an HTML table listing ALL functions in
                                 the Region with their reserved (RC) and
                                 provisioned (PC) concurrency and their peak
                                 concurrent executions over the dashboard's
                                 selected time range, sorted with RC holders
                                 first, then by PC, then by peak usage
- ``request_quota_increase``  -> submits a Service Quotas increase request for
                                 Lambda concurrent executions (idempotent) using
                                 a user-selected increment, returning HTML for the
                                 confirmation popup

Reserved and provisioned concurrency are *configuration*, not CloudWatch
metrics, so they are read from the Lambda control-plane APIs. Peak concurrent
executions are read from CloudWatch over the dashboard's selected time range.
"""

import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SERVICE_CODE = "lambda"
QUOTA_CODE = "L-B99A9384"  # Concurrent executions
DEFAULT_INCREMENT = 10

lambda_client = boto3.client("lambda")
quotas = boto3.client("service-quotas")
cloudwatch = boto3.client("cloudwatch")

DOCS = """
## All functions: allocated concurrency + peak usage

Lists every Lambda function in the Region with its reserved (RC) and provisioned
(PC) concurrency, plus its peak `ConcurrentExecutions` over the dashboard's
selected time range.

Sort order (default): functions with reserved concurrency first (highest RC
first), then by provisioned concurrency, then by peak concurrent executions.
Click any column header to sort by that column; click again to reverse.

RC and PC are configuration (read from the Lambda APIs); peak concurrency is
read from CloudWatch for the selected time range. The **Total allocated** row is
the allocated-concurrency term of
`ClaimedAccountConcurrency = UnreservedConcurrentExecutions + Allocated`.

```
{ "action": "render" }
```
"""


def _region_account(event):
    ctx = event.get("widgetContext", {}) or {}
    region = ctx.get("region") or boto3.session.Session().region_name or "us-east-1"
    account = ctx.get("accountId", "")
    return region, account


def _function_links(region, name):
    base = f"https://{region}.console.aws.amazon.com/lambda/home?region={region}#/functions/{name}"
    rc = f"{base}/edit/concurrency?tab=configure"
    return base, rc


# Open console links in a new tab so the dashboard stays open.
_NEW_TAB = ' target="_blank" rel="noopener noreferrer"'

SORT_COLUMNS = frozenset({"name", "rc", "pc", "peak"})


def _time_range(event):
    """Return (start_ms, end_ms) from the widget context, defaulting to 3h."""
    ctx = event.get("widgetContext", {}) or {}
    tr = ctx.get("timeRange", {}) or {}
    start = tr.get("start")
    end = tr.get("end")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return int(start), int(end)
    # Fallback: last 3 hours.
    import time

    now_ms = int(time.time() * 1000)
    return now_ms - 3 * 60 * 60 * 1000, now_ms


def _peak_concurrency_by_function(start_ms, end_ms):
    """Peak ConcurrentExecutions per function over the range, via Metrics
    Insights. Returns {function_name: max_value}. Functions without datapoints
    are absent (treated as 0 by the caller).

    MAX is used rather than AVG because concurrency limits are about simultaneous
    executions at peak, not average load over the period.
    """
    peaks = {}
    query = (
        'SELECT MAX(ConcurrentExecutions) FROM SCHEMA("AWS/Lambda", FunctionName) '
        "GROUP BY FunctionName ORDER BY MAX() DESC"
    )
    try:
        paginator = cloudwatch.get_paginator("get_metric_data")
        for page in paginator.paginate(
            MetricDataQueries=[
                {
                    "Id": "q1",
                    "Expression": query,
                    "Period": 60,
                }
            ],
            StartTime=start_ms / 1000.0,
            EndTime=end_ms / 1000.0,
        ):
            for result in page.get("MetricDataResults", []):
                name = result.get("Label", "")
                values = result.get("Values", [])
                if values:
                    peaks[name] = max(values)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("get_metric_data for peak concurrency failed: %s", exc)
    return peaks


def _sort_key(row):
    """RC holders first, then by RC value, PC, peak concurrent executions."""
    has_rc = row["rc"] is not None
    return (
        0 if has_rc else 1,
        -(row["rc"] or 0),
        -row["pc"],
        -row["peak"],
        row["name"],
    )


def _read_sort_state(event):
    """Sort state from a header click payload or persisted form fields."""
    ctx = event.get("widgetContext", {}) or {}
    forms = (ctx.get("forms", {}) or {}).get("all", {}) or {}

    sort_by = event.get("sortBy") or forms.get("sortBy") or None
    sort_dir = event.get("sortDir") or forms.get("sortDir") or "desc"

    if not sort_by or sort_by == "default":
        sort_by = None
    elif sort_by not in SORT_COLUMNS:
        sort_by = None
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    return sort_by, sort_dir


def _column_sort_key(column):
    """Value extractor for user-selected column sort."""

    def key(row):
        if column == "name":
            return row["name"].lower()
        if column == "rc":
            return row["rc"] if row["rc"] is not None else -1
        if column == "pc":
            return row["pc"]
        return row["peak"]

    return key


def _apply_sort(rows, sort_by, sort_dir):
    if sort_by:
        rows.sort(key=_column_sort_key(sort_by), reverse=(sort_dir == "desc"))
    else:
        rows.sort(key=_sort_key)
    return rows


def _next_sort_dir(column, sort_by, sort_dir):
    """Toggle direction when re-clicking the active column."""
    if sort_by == column:
        return "asc" if sort_dir == "desc" else "desc"
    return "asc" if column == "name" else "desc"


def _render_sort_header(self_arn, column, label, sort_by, sort_dir):
    """Clickable column header via cwdb-action (refreshes widget in place).

    cwdb-action attaches to the *previous* HTML element, so the anchor must
    come first, then the cwdb-action tag (see AWS custom-widget interactivity docs).
    """
    active = sort_by == column
    next_dir = _next_sort_dir(column, sort_by if active else None, sort_dir if active else "desc")
    indicator = ""
    if active:
        indicator = " &#9660;" if sort_dir == "desc" else " &#9650;"  # ▼ ▲

    payload = (
        f'{{ "action": "render", "sortBy": "{column}", "sortDir": "{next_dir}" }}'
    )
    # btn class is required for cwdb-action; inline styles keep it header-like.
    btn_style = (
        "background:none;border:none;box-shadow:none;padding:0;"
        "color:inherit;font-weight:bold;text-decoration:underline;cursor:pointer;"
    )
    return (
        f'<th style="white-space:nowrap;">'
        f'<a class="btn" style="{btn_style}">{label}{indicator}</a>'
        f'<cwdb-action action="call" display="widget" endpoint="{self_arn}">'
        f"{payload}"
        f"</cwdb-action>"
        f"</th>"
    )


def _collect_allocations(event):
    """Return per-function RC/PC + peak concurrency for ALL functions.

    RC-over-PC rule: if a function has both RC and PC, only RC counts toward
    allocated concurrency (RC is always >= PC), so PC is shown but not
    double-counted in the total.
    """
    start_ms, end_ms = _time_range(event)
    peaks = _peak_concurrency_by_function(start_ms, end_ms)

    rows = []
    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            name = fn["FunctionName"]

            rc = None
            try:
                cc = lambda_client.get_function_concurrency(FunctionName=name)
                rc = cc.get("ReservedConcurrentExecutions")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("get_function_concurrency failed for %s: %s", name, exc)

            pc_total = 0
            try:
                pc_paginator = lambda_client.get_paginator(
                    "list_provisioned_concurrency_configs"
                )
                for pc_page in pc_paginator.paginate(FunctionName=name):
                    for cfg in pc_page.get("ProvisionedConcurrencyConfigs", []):
                        pc_total += cfg.get("RequestedProvisionedConcurrentExecutions", 0)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "list_provisioned_concurrency_configs failed for %s: %s", name, exc
                )

            allocated = rc if rc else pc_total
            rows.append(
                {
                    "name": name,
                    "rc": rc,
                    "pc": pc_total,
                    "allocated": allocated,
                    "peak": peaks.get(name, 0.0),
                }
            )

    return rows


def _render_quota_panel(self_arn, quota_link, sort_by, sort_dir):
    """Right-hand panel: increment input + quota increase action."""
    sort_by_val = sort_by or ""
    sort_dir_val = sort_dir if sort_by else ""
    return (
        '<div style="border:1px solid #d5dbdb;border-radius:8px;padding:16px;'
        'background:#fafafa;min-width:240px;max-width:280px;">'
        '<div style="font-weight:600;font-size:14px;margin-bottom:14px;">'
        "Request limit increase</div>"
        f'<input type="hidden" name="sortBy" value="{sort_by_val}" />'
        f'<input type="hidden" name="sortDir" value="{sort_dir_val}" />'
        '<label for="increment" style="display:block;font-size:13px;'
        'color:#545b64;margin-bottom:6px;">Increase amount</label>'
        f'<input type="number" id="increment" name="increment" '
        f'value="{DEFAULT_INCREMENT}" min="1" step="1" '
        'style="width:100%;box-sizing:border-box;margin-bottom:14px;" />'
        '<a class="btn btn-primary" style="display:block;text-align:center;'
        'margin-bottom:12px;">Request limit increase</a>'
        '<cwdb-action action="call" display="popup" '
        'confirmation="Submit a Service Quotas increase request for Lambda '
        'concurrent executions?" '
        f'endpoint="{self_arn}">'
        '{ "action": "request_quota_increase" }'
        "</cwdb-action>"
        f'<div style="margin-top:14px;font-size:13px;">'
        f'<a href="{quota_link}"{_NEW_TAB}>Open Service Quotas console</a></div>'
        "</div>"
    )


def _render_table(event, self_arn):
    region, _account = _region_account(event)
    sort_by, sort_dir = _read_sort_state(event)
    rows = _apply_sort(_collect_allocations(event), sort_by, sort_dir)

    quota_link = (
        f"https://{region}.console.aws.amazon.com/servicequotas/home/"
        f"services/lambda/quotas/{QUOTA_CODE}"
    )

    quota_panel = _render_quota_panel(self_arn, quota_link, sort_by, sort_dir)

    if not rows:
        table_html = "<p>No Lambda functions found in this Region.</p>"
    else:
        total_allocated = sum(r["allocated"] for r in rows)
        parts = [
            '<table style="width:100%;">',
            "<thead><tr>",
            _render_sort_header(self_arn, "name", "Function", sort_by, sort_dir),
            _render_sort_header(self_arn, "rc", "Reserved", sort_by, sort_dir),
            _render_sort_header(self_arn, "pc", "Provisioned", sort_by, sort_dir),
            _render_sort_header(self_arn, "peak", "Peak concurrency", sort_by, sort_dir),
            "<th>Actions</th>",
            "</tr></thead>",
            "<tbody>",
        ]
        for r in rows:
            overview, rc_link = _function_links(region, r["name"])
            rc_disp = r["rc"] if r["rc"] else "-"
            pc_disp = r["pc"] if r["pc"] else "-"
            peak_disp = f"{r['peak']:.0f}" if r["peak"] else "0"
            parts.append(
                "<tr>"
                f'<td><a href="{overview}"{_NEW_TAB}>{r["name"]}</a></td>'
                f"<td>{rc_disp}</td>"
                f"<td>{pc_disp}</td>"
                f"<td>{peak_disp}</td>"
                f'<td><a href="{overview}"{_NEW_TAB}>View</a> | '
                f'<a href="{rc_link}"{_NEW_TAB}>Set RC</a></td>'
                "</tr>"
            )
        parts.append(
            f'<tr><td colspan="5" style="padding-top:8px;">'
            f"<b>Total allocated concurrency: {total_allocated}</b></td></tr>"
        )
        parts.append("</tbody></table>")
        table_html = "".join(parts)

    # Two-column layout: functions table (left) + quota controls (right).
    # Single form keeps sort state and increment value across widget refreshes.
    return (
        "<form>"
        '<table cellpadding="0" cellspacing="0" style="width:100%;border:none;">'
        "<tr>"
        f'<td style="vertical-align:top;padding-right:24px;">{table_html}</td>'
        f'<td style="vertical-align:top;width:280px;">{quota_panel}</td>'
        "</tr></table>"
        "</form>"
    )


def _has_pending_request():
    paginator = quotas.get_paginator(
        "list_requested_service_quota_change_history_by_quota"
    )
    for page in paginator.paginate(ServiceCode=SERVICE_CODE, QuotaCode=QUOTA_CODE):
        for r in page.get("RequestedQuotas", []):
            if r["Status"] in ("PENDING", "CASE_OPENED"):
                return True
    return False


def _selected_increment(event):
    """Read the increment chosen in the widget's number input (form field).

    CloudWatch passes form fields under widgetContext.forms.all.
    """
    ctx = event.get("widgetContext", {}) or {}
    forms = ctx.get("forms", {}) or {}
    value = (forms.get("all", {}) or {}).get("increment")
    try:
        increment = int(float(value))
        if increment > 0:
            return increment
    except (TypeError, ValueError):
        pass
    return DEFAULT_INCREMENT


def _requests_link(region):
    """Service Quotas 'Requested quota increases' page for the given Region."""
    return (
        f"https://{region}.console.aws.amazon.com/servicequotas/home/requests"
        f"?region={region}"
    )


def _view_requests_button(region):
    """An action-style link button to the Service Quotas requests page."""
    return (
        f'<br/><br/><a class="btn" href="{_requests_link(region)}"{_NEW_TAB}>'
        "View quota requests</a>"
    )


def _request_quota_increase(event):
    region, _account = _region_account(event)

    if _has_pending_request():
        return (
            "<p>A quota increase request is already pending. No action taken.</p>"
            f"{_view_requests_button(region)}"
        )

    increment = _selected_increment(event)
    current = quotas.get_service_quota(
        ServiceCode=SERVICE_CODE, QuotaCode=QUOTA_CODE
    )
    current_value = current["Quota"]["Value"]
    desired_value = current_value + increment

    response = quotas.request_service_quota_increase(
        ServiceCode=SERVICE_CODE,
        QuotaCode=QUOTA_CODE,
        DesiredValue=desired_value,
    )
    status = response["RequestedQuota"]["Status"]
    logger.info(
        "Requested increase: %s -> %s (+%s) | Status: %s",
        current_value,
        desired_value,
        increment,
        status,
    )
    return (
        f"<p>Requested concurrency limit increase: "
        f"<b>{int(current_value)}</b> &rarr; <b>{int(desired_value)}</b> "
        f"(+{increment}).<br/>Status: {status}</p>"
        f"{_view_requests_button(region)}"
    )


def lambda_handler(event, context):
    if event.get("describe") is not None:
        return DOCS

    action = event.get("action", "render")
    logger.info("Custom widget action: %s", action)

    if action == "request_quota_increase":
        return _request_quota_increase(event)

    self_arn = getattr(context, "invoked_function_arn", "")
    return _render_table(event, self_arn)
