# AWS Lambda: Proactively Monitoring Concurrency with ClaimedAccountConcurrency

You use AWS Lambda and want to get notified when concurrency utilization reaches 70% — before throttling happens, not after.

This article walks through how Lambda concurrency actually works, why `ClaimedAccountConcurrency` is the metric you should be watching, and how to set up a CloudWatch alarm that gives you time to react.

---

## What is concurrency in AWS Lambda?

From [AWS documentation](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html):

> **Concurrency is the number of in-flight requests that your AWS Lambda function is handling at the same time.**

Each concurrent request needs its own **execution environment** — a dedicated, isolated container that can only process **one request at a time**. If your function is handling 5 requests simultaneously, Lambda is running 5 separate execution environments.

A practical way to calculate concurrency:

```
Concurrency = (average requests per second) × (average request duration in seconds)
```

For example, if your function receives **100 requests/second** and each takes **500ms**:

```
Concurrency = 100 × 0.5 = 50
```

That means Lambda needs **50 execution environments** running in parallel to handle that load without throttling.

---

## Execution environment lifecycle

Every Lambda execution environment goes through two phases:

| Phase      | What happens                                  | Also known as |
|-----------|-----------------------------------------------|--------------|
| **Init**   | Runtime starts, dependencies load, your code outside the handler runs | Cold start    |
| **Invoke** | Your handler function executes                | Warm execution |

During both phases, the execution environment is **busy** — it cannot accept another request.

### Environment reuse (warm starts)

Lambda doesn't throw away environments after each request. When an environment finishes processing, it stays alive and can handle the **next request** without going through Init again:

- **First request** → Init + Invoke (cold start)
- **Subsequent requests** → Invoke only (warm start)

This reuse is what makes Lambda efficient — warm starts skip the initialization overhead entirely.

---

## How Lambda scales to handle concurrent requests

When multiple requests arrive at the same time, Lambda creates as many execution environments as needed:

- If an idle environment **is available** → reuse it (warm start)
- If **no idle environment exists** → create a new one (cold start)

Here's what that looks like with 10 requests arriving over time:

![Lambda concurrency diagram — multiple concurrent requests over time with Init and Invoke phases](./images/lambda-concurrency-diagram.png)

Walking through the diagram:

| Request | What Lambda does         | Why                                                          |
|---------|-------------------------|--------------------------------------------------------------|
| 1–5     | Creates new environments | No idle environments available — each is a cold start        |
| 6       | Reuses environment from request 1 | Environment 1 finished, now idle — warm start      |
| 7–8     | Reuses environments from requests 2–3 | Those environments finished — warm starts        |
| 9       | Creates a new environment | All existing environments are still busy — cold start       |
| 10      | Reuses environment from request 4 | Environment 4 finished — warm start               |

### Visualizing concurrency at a point in time

Draw a **vertical line** at any moment and count the active environments it crosses. That's your concurrency at that instant.

In the diagram above, the dashed green line at time `t` crosses **5 active environments** — so the concurrency at that moment is **5**.

> **Key concept:** Concurrency = number of execution environments active at the same time.

---

## Concurrency is regional and account-level

This is a critical point that catches people off guard: **concurrency is not per function**. It is:

- **Shared** across all Lambda functions in the account
- **Scoped** to a single AWS Region

| Region       | Concurrency pool |
|-------------|-----------------|
| `us-east-1` | One shared pool  |
| `eu-west-1` | Separate pool    |

If you have 20 functions in `us-east-1`, they all draw from the **same concurrency pool**.

### Default limit

By default, every AWS account gets **1,000 concurrent executions per Region**.

- This is a **soft limit** — you can request an increase through [Service Quotas](https://docs.aws.amazon.com/servicequotas/latest/userguide/request-quota-increase.html)
- New accounts may start with **lower limits** that AWS increases gradually

### Concurrency limit vs scaling rate

These are two different things:

| Concept              | What it means                                              |
|---------------------|-----------------------------------------------------------|
| **Concurrency limit** | Maximum total concurrent executions allowed (e.g. 1,000) |
| **Scaling rate**      | How fast Lambda can spin up new environments              |

From AWS: Lambda can provision up to **1,000 new environments every 10 seconds** per function.

This means even with a limit of 1,000, Lambda **cannot reach it instantly**. If you get a sudden spike from 0 to 1,000 concurrent requests, some will be throttled while Lambda catches up.

---

## Understanding concurrency metrics

Lambda emits several concurrency-related CloudWatch metrics (1-minute granularity):

| Metric                            | What it measures                                           |
|----------------------------------|-----------------------------------------------------------|
| `ConcurrentExecutions`            | Actively running function invocations right now            |
| `UnreservedConcurrentExecutions`  | Invocations using the shared (unreserved) concurrency pool |
| `ClaimedAccountConcurrency`       | Total concurrency **unavailable** for new on-demand invocations |

### Why ConcurrentExecutions is not enough

You might think monitoring `ConcurrentExecutions` gives you the full picture. It doesn't.

`ConcurrentExecutions` only shows what's **actively running**. It doesn't account for concurrency that's been **allocated but isn't in use** — which still counts against your limit.

---

## ClaimedAccountConcurrency — the metric that matters

`ClaimedAccountConcurrency` represents the total concurrency that is **unavailable** for new on-demand invocations. It's calculated as:

```
ClaimedAccountConcurrency = UnreservedConcurrentExecutions + Allocated Concurrency
```

Where **Allocated Concurrency** includes:

- **Reserved concurrency (RC):** Guarantees a function gets a dedicated slice of the pool. That capacity is **blocked** from other functions, even when idle.
- **Provisioned concurrency (PC):** Pre-initializes environments to eliminate cold starts. That capacity is **reserved** and counts against the pool, even when no requests are being processed.

### Why this matters — a real example

| Item                             | Value  |
|---------------------------------|--------|
| Account concurrency limit        | 1,000  |
| Reserved concurrency (function A)| 400    |
| Reserved concurrency (function B)| 400    |
| Provisioned concurrency (function C)| 100 |
| Active executions right now       | 50     |

**What ConcurrentExecutions shows:** 50

**What ClaimedAccountConcurrency shows:** 900 (400 + 400 + 100)

**What's actually available for new on-demand invocations:** 100

Even though only 50 invocations are running, **900 units are claimed**. If other functions spike, they only have 100 units to work with before throttling kicks in.

This is exactly why Lambda uses `ClaimedAccountConcurrency` — not `ConcurrentExecutions` — to determine whether capacity is available.

---

## Concurrency vs Requests Per Second (RPS)

Lambda enforces a **separate** rate limit:

```
Max RPS = 10 × concurrency limit
```

With the default limit of 1,000 concurrency:

```
Max RPS = 10 × 1,000 = 10,000 requests/second
```

You can be **throttled due to request rate** even if your concurrency isn't maxed out. For example, a function with 20ms average duration processing 30,000 requests/second only needs 600 concurrent environments — but it exceeds the 10,000 RPS cap and will be throttled.

---

## Setting up the CloudWatch metrics

Go to **CloudWatch → All metrics → Source** and paste this configuration:

```json
{
  "metrics": [
    [ "AWS/Lambda", "ConcurrentExecutions", {
      "id": "m1", "yAxis": "left",
      "label": "ConcurrentExecutionsMetric", "visible": false
    }],
    [ { "expression": "SERVICE_QUOTA(m1)",
        "label": "Current Concurrent Limit",
        "id": "e1", "period": 60, "yAxis": "left",
        "color": "#9467bd"
    }],
    [ "AWS/Lambda", "ClaimedAccountConcurrency", {
      "id": "m2", "yAxis": "left", "color": "#ff7f0e"
    }],
    [ { "expression": "(m2/e1) * 100",
        "label": "% Claimed",
        "id": "e2", "period": 60, "yAxis": "left"
    }],
    [ { "expression": "e1 - m2",
        "label": "Available",
        "id": "e5", "period": 60, "yAxis": "left",
        "color": "#2ca02c"
    }]
  ],
  "sparkline": false,
  "view": "pie",
  "stacked": false,
  "region": "us-east-1",
  "period": 60,
  "stat": "Maximum",
  "liveData": false,
  "labels": { "visible": true },
  "legend": { "position": "bottom" }
}
```

### Breaking down the metrics

| ID   | Type       | What it does                                                                 |
|------|-----------|-----------------------------------------------------------------------------|
| `m1` | Metric     | `ConcurrentExecutions` — needed as the input for `SERVICE_QUOTA()`          |
| `e1` | Expression | `SERVICE_QUOTA(m1)` — dynamically fetches your actual regional limit        |
| `m2` | Metric     | `ClaimedAccountConcurrency` — the metric we're monitoring                   |
| `e2` | Expression | `(m2/e1) * 100` — utilization as a percentage                               |
| `e5` | Expression | `e1 - m2` — remaining available concurrency                                |

> **Why `SERVICE_QUOTA(m1)` instead of hardcoding 1,000?** Because the concurrency limit is a soft limit. If you've requested an increase, `SERVICE_QUOTA()` dynamically reflects your actual current limit.

### Visualizing as a Pie chart

Switch the view to **Pie** and select `ClaimedAccountConcurrency` and `Available`:

![Pie chart — ClaimedAccountConcurrency at 33.3%, Available at 66.7%](./images/pie-chart-concurrency.png)

This gives you an immediate visual of how much capacity is claimed vs. available.

---

## Creating the CloudWatch alarm

### Step 1: Create alarm from the metric

Click the **bell icon** on the `% Claimed` metric (`e2`) to create an alarm directly from it.

### Step 2: Configure the alarm condition

| Setting          | Value                  | Why                                                    |
|-----------------|------------------------|-------------------------------------------------------|
| **Threshold**    | Greater than 70        | Alert before reaching the limit — 70% gives headroom  |
| **Period**       | 1 minute               | Match Lambda's metric granularity                      |
| **Statistic**    | Maximum                | Catch spikes, not averages                             |
| **Datapoints**   | 1 out of 1             | Alert on the first breach, don't wait for sustained    |

### Step 3: Add alarm details

Configure the alarm name and description. The description field supports Markdown when viewed in the CloudWatch console:

![CloudWatch alarm details — name and description setup](./images/alarm-details-setup.png)

### Step 4: Configure notifications

Set up an SNS topic to receive alarm notifications via email, Slack, or any integration you prefer.

### Alarm in action

Once active, the alarm graph shows your utilization over time:

- **Blue line** → `% Claimed` utilization
- **Threshold** → 70%
- Alarm transitions to **In alarm** state when the line crosses the threshold

![CloudWatch alarm graph — % Claimed with threshold > 70](./images/alarm-graph-threshold-70.png)

---

## Taking it further: event-driven limit increases

Instead of just alerting, you can make this **event-driven**. When the alarm transitions to the `ALARM` state, it can trigger:

1. **An SNS notification** → sends an alert to your team
2. **A Lambda function** → automatically submits a Service Quotas increase request via the AWS SDK

This turns your monitoring from reactive ("we got throttled, now what?") into proactive ("concurrency is at 70%, let's scale the limit before it becomes a problem").

---

## Conclusion

Monitoring Lambda concurrency correctly requires understanding what's actually being measured:

| What you might monitor       | What you should monitor           |
|-----------------------------|----------------------------------|
| `ConcurrentExecutions`       | `ClaimedAccountConcurrency`      |
| Active invocations only      | Active + reserved + provisioned  |
| Partial view of capacity     | True remaining capacity          |

### Key takeaways

- **Concurrency** = number of active execution environments at the same time
- **One request** = one environment — they cannot be shared
- Concurrency is **regional** and **shared** across all functions in the account
- Scaling is **gradual** (1,000 new environments per 10 seconds), not instant
- `ClaimedAccountConcurrency` reflects **real capacity usage**, including reserved and provisioned
- Set alarms at **70%** to give yourself time to react or auto-scale
- `SERVICE_QUOTA()` dynamically fetches your actual limit — don't hardcode it

---

*References: [AWS Lambda — Understanding function scaling](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html)*
