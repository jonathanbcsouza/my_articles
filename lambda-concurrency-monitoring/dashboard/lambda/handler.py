"""CloudWatch custom-widget Lambda for the Lambda concurrency dashboard.

The function is action-routed based on the incoming event:

- ``describe``                -> returns markdown documentation for the widget
- ``render`` (default)        -> returns an HTML table listing ALL functions in
                                 the Region with their reserved (RC) and
                                 provisioned (PC) concurrency, peak concurrent
                                 executions, and invocations / errors /
                                 throttles over the dashboard's selected time
                                 range, sorted with RC holders first, then by
                                 PC, then by peak usage
- ``request_quota_increase``  -> submits a Service Quotas increase request for
                                 Lambda concurrent executions (idempotent) using
                                 a user-selected increment, returning HTML for the
                                 confirmation popup

Reserved and provisioned concurrency are *configuration*, not CloudWatch
metrics, so they are read from the Lambda control-plane APIs. Peak concurrent
executions are read from CloudWatch over the dashboard's selected time range.
"""

import logging
import re

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
## All functions: allocation + activity

Lists every Lambda function in the Region with its reserved (RC) and provisioned
(PC) concurrency, peak `ConcurrentExecutions`, and total invocations, errors,
and throttles over the dashboard's selected time range.

Sort order (default): functions with reserved concurrency first (highest RC
first), then by provisioned concurrency, then by peak concurrent executions.
Click any column header to sort by that column; click again to reverse.

RC and PC are configuration (read from the Lambda APIs). Peak concurrency,
invocations, errors, and throttles are read from CloudWatch for the selected
time range. The **Total allocated** row is the allocated-concurrency term of
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

SORT_COLUMNS = frozenset({"name", "rc", "pc", "peak", "invocations", "errors", "throttles"})


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


def _label_to_function_name(label):
    """Normalize Metrics Insights GROUP BY labels to a function name."""
    if not label:
        return ""
    prefix = "FunctionName "
    if label.startswith(prefix):
        return label[len(prefix) :]
    # Metrics Insights get_metric_data labels: "1 - my-function"
    rank_match = re.match(r"^\d+\s*-\s*(.+)$", label)
    if rank_match:
        return rank_match.group(1).strip()
    return label


def _aggregate_metric_values(values, agg):
    if not values:
        return 0.0
    if agg == "max":
        return float(max(values))
    return float(sum(values))


def _metrics_by_function(start_ms, end_ms, specs, schema_dims="FunctionName"):
    """Fetch per-function CloudWatch metrics via Metrics Insights.

    ``specs`` is a list of ``(result_key, metric_name, stat, agg)`` where
    ``agg`` is how to combine datapoints in the range (``max`` or ``sum``).
    ``schema_dims`` is the SCHEMA dimension list (e.g. ``FunctionName`` or
    ``FunctionName, Resource`` for provisioned-concurrency metrics).
    Returns ``{result_key: {function_name: value}}``.
    """
    if not specs:
        return {}

    out = {result_key: {} for result_key, *_ in specs}

    # CloudWatch allows only ONE Metrics Insights (SELECT ...) query per
    # get_metric_data call, so each spec gets its own request.
    paginator = cloudwatch.get_paginator("get_metric_data")
    for result_key, metric_name, stat, agg in specs:
        query = {
            "Id": "q0",
            "Expression": (
                f'SELECT {stat}({metric_name}) FROM SCHEMA("AWS/Lambda", {schema_dims}) '
                "GROUP BY FunctionName"
            ),
            "Period": 60,
        }
        values_by_name = {}
        try:
            for page in paginator.paginate(
                MetricDataQueries=[query],
                StartTime=start_ms / 1000.0,
                EndTime=end_ms / 1000.0,
            ):
                for result in page.get("MetricDataResults", []):
                    name = _label_to_function_name(result.get("Label", ""))
                    if not name:
                        continue
                    # Pagination can split a function's datapoints across
                    # pages, so collect them all before aggregating.
                    values_by_name.setdefault(name, []).extend(
                        result.get("Values", [])
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("get_metric_data failed for %s: %s", metric_name, exc)

        for name, values in values_by_name.items():
            out[result_key][name] = _aggregate_metric_values(values, agg)

    return out


def _activity_metrics_by_function(start_ms, end_ms):
    """Peak concurrency plus invocations, errors, and throttles for the range."""
    combined = _metrics_by_function(
        start_ms,
        end_ms,
        [
            ("peak", "ConcurrentExecutions", "MAX", "max"),
            ("invocations", "Invocations", "SUM", "sum"),
            ("errors", "Errors", "SUM", "sum"),
            ("throttles", "Throttles", "SUM", "sum"),
        ],
    )
    return (
        combined.get("peak", {}),
        combined.get("invocations", {}),
        combined.get("errors", {}),
        combined.get("throttles", {}),
    )


def _peak_concurrency_by_function(start_ms, end_ms):
    """Peak ConcurrentExecutions per function over the range (Metrics Insights)."""
    peaks, _, _, _ = _activity_metrics_by_function(start_ms, end_ms)
    return peaks


_PC_SCHEMA = "FunctionName, Resource"


def _pc_metrics_by_function(start_ms, end_ms):
    """PC utilization (max %) and spillover invocations (sum) per function."""
    combined = _metrics_by_function(
        start_ms,
        end_ms,
        [
            ("utilization", "ProvisionedConcurrencyUtilization", "MAX", "max"),
            ("spillover", "ProvisionedConcurrencySpilloverInvocations", "SUM", "sum"),
        ],
        schema_dims=_PC_SCHEMA,
    )
    return (
        combined.get("utilization", {}),
        combined.get("spillover", {}),
    )


def _functions_with_pc():
    """Return {function_name: provisioned_concurrency} for functions with PC > 0."""
    out = {}
    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            name = fn["FunctionName"]
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
            if pc_total > 0:
                out[name] = pc_total
    return out


def _pc_utilization_percent(value):
    """Normalize CloudWatch utilization (0–1 or 0–100) to a 0–100 percentage."""
    if value <= 1.0:
        return value * 100.0
    return float(value)


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
        if column == "invocations":
            return row["invocations"]
        if column == "errors":
            return row["errors"]
        if column == "throttles":
            return row["throttles"]
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


def _render_sort_header(
    self_arn, column, label, sort_by, sort_dir, title=None, show_indicator=True
):
    """Clickable column header via cwdb-action (refreshes widget in place).

    cwdb-action attaches to the *previous* HTML element, so the anchor must
    come first, then the cwdb-action tag (see AWS custom-widget interactivity docs).
    """
    active = sort_by == column
    next_dir = _next_sort_dir(column, sort_by if active else None, sort_dir if active else "desc")
    indicator = ""
    if active and show_indicator:
        indicator = " &#9660;" if sort_dir == "desc" else " &#9650;"  # ▼ ▲

    payload = (
        f'{{ "action": "render", "sortBy": "{column}", "sortDir": "{next_dir}" }}'
    )
    # btn class is required for cwdb-action; inline styles keep it header-like.
    btn_style = (
        "background:none;border:none;box-shadow:none;padding:0;"
        "color:inherit;font-weight:bold;text-decoration:underline;cursor:pointer;"
    )
    title_attr = f' title="{title}"' if title else ""
    return (
        f'<th style="white-space:nowrap;"{title_attr}>'
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
    peaks, invocations, errors, throttles = _activity_metrics_by_function(start_ms, end_ms)

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
                    "invocations": invocations.get(name, 0.0),
                    "errors": errors.get(name, 0.0),
                    "throttles": throttles.get(name, 0.0),
                }
            )

    return rows


def _render_allocation_summary(total_rc, total_pc, total_allocated):
    """Account-level RC / PC totals shown below the quota-increase panel."""
    return (
        '<div style="border:1px solid #d5dbdb;border-radius:8px;padding:14px;'
        'background:#fff;margin-top:12px;min-width:240px;max-width:280px;">'
        '<div style="font-weight:600;font-size:13px;margin-bottom:10px;">'
        "Account allocation</div>"
        '<table style="width:100%;font-size:13px;border-collapse:collapse;">'
        f"<tr><td style=\"padding:3px 0;color:#545b64;\">Reserved (RC)</td>"
        f'<td style="text-align:right;font-weight:600;">{total_rc}</td></tr>'
        f"<tr><td style=\"padding:3px 0;color:#545b64;\">Provisioned (PC)</td>"
        f'<td style="text-align:right;font-weight:600;">{total_pc}</td></tr>'
        f'<tr><td colspan="2" style="border-top:1px solid #eaeded;padding-top:6px;">'
        f"</td></tr>"
        f"<tr><td style=\"padding:3px 0;color:#545b64;\">Total claimed (RC+PC)</td>"
        f'<td style="text-align:right;font-weight:700;">{total_allocated}</td></tr>'
        "</table>"
        '<div style="font-size:11px;color:#687078;margin-top:8px;line-height:1.4;">'
        "Counts toward ClaimedAccountConcurrency even when functions are idle."
        "</div></div>"
    )


def _render_quota_panel(
    self_arn, quota_link, sort_by, sort_dir, total_rc, total_pc, total_allocated
):
    """Right-hand panel: increment input + quota increase action + allocation summary."""
    sort_by_val = sort_by or ""
    sort_dir_val = sort_dir if sort_by else ""
    return (
        '<div style="min-width:240px;max-width:280px;">'
        '<div style="border:1px solid #d5dbdb;border-radius:8px;padding:16px;'
        'background:#fafafa;">'
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
        f"{_render_allocation_summary(total_rc, total_pc, total_allocated)}"
        "</div>"
    )


def _render_top_n_bar(event):
    """Horizontal bar chart for top-N functions by throttles or errors."""
    metric = event.get("metric", "throttles")
    if metric not in ("throttles", "errors"):
        metric = "throttles"
    try:
        limit = max(1, min(25, int(event.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10

    label = "Throttles" if metric == "throttles" else "Errors"
    rows = _collect_allocations(event)
    ranked = sorted(rows, key=lambda r: r[metric], reverse=True)
    ranked = [r for r in ranked if r[metric] > 0][:limit]

    if not ranked:
        return f"<p>No {label.lower()} in the selected time range.</p>"

    max_val = max(r[metric] for r in ranked) or 1
    parts = [
        '<div style="font-size:12px;line-height:1.6;">',
    ]
    for r in ranked:
        pct = int((r[metric] / max_val) * 100)
        name = r["name"]
        val = int(r[metric])
        parts.append(
            '<div style="margin-bottom:8px;">'
            f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'font-size:11px;margin-bottom:2px;" title="{name}">{name}</div>'
            '<div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="flex:1;background:#eaeded;border-radius:3px;height:14px;">'
            f'<div style="width:{pct}%;background:#1f77b4;height:14px;border-radius:3px;">'
            f"</div></div>"
            f'<span style="min-width:28px;text-align:right;font-weight:600;">{val}</span>'
            "</div></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _render_pc_bar(event):
    """Horizontal bar chart for PC utilization % or spillover invocations by function."""
    metric = event.get("metric", "utilization")
    if metric not in ("utilization", "spillover"):
        metric = "utilization"

    start_ms, end_ms = _time_range(event)
    utilization, spillover = _pc_metrics_by_function(start_ms, end_ms)
    pc_functions = _functions_with_pc()

    metric_values = utilization if metric == "utilization" else spillover
    all_names = set(pc_functions) | set(metric_values)
    rows = []
    for name in all_names:
        raw = metric_values.get(name, 0.0)
        if metric == "utilization":
            value = _pc_utilization_percent(raw)
            display = f"{value:.0f}%"
            bar_value = value
        else:
            value = raw
            display = f"{int(value)}"
            bar_value = value
        rows.append({"name": name, "value": value, "bar_value": bar_value, "display": display})

    rows = [r for r in rows if r["value"] > 0 or r["name"] in pc_functions]
    rows.sort(key=lambda r: r["value"], reverse=True)

    if metric == "utilization":
        empty_msg = "No provisioned concurrency configured or no utilization in the selected time range."
        bar_color = "#2ca02c"
    else:
        empty_msg = "No provisioned concurrency configured or no spillover in the selected time range."
        bar_color = "#ff7f0e"

    if not rows:
        return f"<p>{empty_msg}</p>"

    max_val = max(r["bar_value"] for r in rows) or 1
    parts = ['<div style="font-size:12px;line-height:1.6;">']
    for r in rows:
        pct = int((r["bar_value"] / max_val) * 100)
        name = r["name"]
        parts.append(
            '<div style="margin-bottom:8px;">'
            f'<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'font-size:11px;margin-bottom:2px;" title="{name}">{name}</div>'
            '<div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="flex:1;background:#eaeded;border-radius:3px;height:14px;">'
            f'<div style="width:{pct}%;background:{bar_color};height:14px;border-radius:3px;">'
            f"</div></div>"
            f'<span style="min-width:36px;text-align:right;font-weight:600;">{r["display"]}</span>'
            "</div></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _render_table(event, self_arn):
    region, _account = _region_account(event)
    sort_by, sort_dir = _read_sort_state(event)
    rows = _apply_sort(_collect_allocations(event), sort_by, sort_dir)

    quota_link = (
        f"https://{region}.console.aws.amazon.com/servicequotas/home/"
        f"services/lambda/quotas/{QUOTA_CODE}"
    )

    if not rows:
        table_html = "<p>No Lambda functions found in this Region.</p>"
        total_rc = total_pc = total_allocated = 0
    else:
        total_rc = sum(r["rc"] or 0 for r in rows)
        total_pc = sum(r["pc"] for r in rows)
        total_allocated = sum(r["allocated"] for r in rows)
        parts = [
            '<table style="width:100%;">',
            "<thead><tr>",
            _render_sort_header(self_arn, "name", "Function", sort_by, sort_dir),
            _render_sort_header(self_arn, "rc", "Reserved", sort_by, sort_dir),
            _render_sort_header(self_arn, "pc", "Provisioned", sort_by, sort_dir),
            _render_sort_header(
                self_arn,
                "peak",
                "Peak concurrency",
                sort_by,
                sort_dir,
                title="MAX(ConcurrentExecutions) over dashboard time range",
            ),
            _render_sort_header(
                self_arn,
                "invocations",
                "Invocations",
                sort_by,
                sort_dir,
                title="SUM(Invocations) over dashboard time range",
                show_indicator=False,
            ),
            _render_sort_header(
                self_arn,
                "errors",
                "Errors",
                sort_by,
                sort_dir,
                title="SUM(Errors) over dashboard time range",
            ),
            _render_sort_header(
                self_arn,
                "throttles",
                "Throttles",
                sort_by,
                sort_dir,
                title="SUM(Throttles) over dashboard time range",
            ),
            "<th>Actions</th>",
            "</tr></thead>",
            "<tbody>",
        ]
        for r in rows:
            overview, rc_link = _function_links(region, r["name"])
            rc_disp = r["rc"] if r["rc"] else "-"
            pc_disp = r["pc"] if r["pc"] else "-"
            peak_disp = f"{r['peak']:.0f}" if r["peak"] else "0"
            inv_disp = f"{r['invocations']:.0f}" if r["invocations"] else "0"
            err_disp = f"{r['errors']:.0f}" if r["errors"] else "0"
            thr_disp = f"{r['throttles']:.0f}" if r["throttles"] else "0"
            parts.append(
                "<tr>"
                f'<td><a href="{overview}"{_NEW_TAB}>{r["name"]}</a></td>'
                f"<td>{rc_disp}</td>"
                f"<td>{pc_disp}</td>"
                f"<td>{peak_disp}</td>"
                f"<td>{inv_disp}</td>"
                f"<td>{err_disp}</td>"
                f"<td>{thr_disp}</td>"
                f'<td><a href="{overview}"{_NEW_TAB}>View</a> | '
                f'<a href="{rc_link}"{_NEW_TAB}>Set RC</a></td>'
                "</tr>"
            )
        parts.append(
            f'<tr><td colspan="8" style="padding-top:8px;">'
            f"<b>Total allocated concurrency: {total_allocated}</b></td></tr>"
        )
        parts.append("</tbody></table>")
        table_html = "".join(parts)

    quota_panel = _render_quota_panel(
        self_arn, quota_link, sort_by, sort_dir, total_rc, total_pc, total_allocated
    )

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


def _render_alarm_state(event):
    """Render a large ON/OFF indicator for a single CloudWatch alarm."""
    alarm_name = event.get("alarmName", "")
    if not alarm_name:
        return "<p>No alarmName provided.</p>"

    try:
        resp = cloudwatch.describe_alarms(AlarmNames=[alarm_name])
        alarms = resp.get("MetricAlarms", [])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("describe_alarms failed for %s: %s", alarm_name, exc)
        return f"<p>Could not read alarm state: {exc}</p>"

    if not alarms:
        return f"<p>Alarm not found: {alarm_name}</p>"

    state = alarms[0].get("StateValue", "INSUFFICIENT_DATA")
    if state == "ALARM":
        label, color, sub = "ON", "#d13212", "Claimed concurrency above 70%"
    elif state == "OK":
        label, color, sub = "OFF", "#1d8102", "Below the 70% threshold"
    else:
        label, color, sub = "—", "#687078", "Insufficient data"

    return (
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;height:100%;text-align:center;">'
        f'<div style="font-size:52px;font-weight:700;line-height:1;color:{color};">'
        f"{label}</div>"
        f'<div style="font-size:12px;color:#545b64;margin-top:8px;">{sub}</div>'
        "</div>"
    )


def lambda_handler(event, context):
    if event.get("describe") is not None:
        return DOCS

    action = event.get("action", "render")
    logger.info("Custom widget action: %s", action)

    if action == "request_quota_increase":
        return _request_quota_increase(event)

    if action == "alarm_state":
        return _render_alarm_state(event)

    if action == "top_n_bar":
        return _render_top_n_bar(event)

    if action == "pc_bar":
        return _render_pc_bar(event)

    self_arn = getattr(context, "invoked_function_arn", "")
    return _render_table(event, self_arn)
