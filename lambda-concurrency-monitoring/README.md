# AWS Lambda: Monitoring Concurrency with ClaimedAccountConcurrency

![AWS Lambda concurrency monitoring overview with ClaimedAccountConcurrency](./images/Diagram.drawio.png)

## What is AWS Lambda?

AWS Lambda is a compute service that runs your code in response to events (API requests, queue messages, file uploads) without requiring you to manage servers. It scales automatically as traffic increases.

## The problem

When you scale serverless architectures, observability can be a challenge. Many times, you might be using a Lambda function, and do not want to get a surprise by having a cascading production issue because you found your functions were throttling because a regional limit was hit.

In this article I will explain what the regional concurrency limit means, how to monitor it, and how it translates to the current capacity you have for your AWS Region.

> **Recommended:** See [concurrency dashboard CDK project](./dashboard-project) that is ready for dpeloyment. It visualizes regional capacity, per-function usage, and a reclaim -> cap -> increase decision guide, so you can decide before changing anything. The sections below explain the concepts behind it. Otherwise, if your growth is healthy and organic, you can then also look at the [automated limit increase solution](./iac).

**Topics:**

1. How Lambda concurrency works (just enough to understand the metric choice)
2. Why `ClaimedAccountConcurrency` is the right metric to monitor
3. Setting up a CloudWatch alarm step by step
4. Automated solution for organic healthy traffic

**Considerations:**

For critical functions, setting a [Reserved Concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html) (RC) is recommended. Reserved concurrency guarantees dedicated capacity that no on-demand functions can consume.

When the alarm fires, it is recommended to investigate before requesting more capacity. Examples where increasing the limit will not help:

- **Reserved Concurrency over limit increases.** If you have multiple functions in the same region and you predict on-demand traffic growth across multiple functions, use RC to protect the critical ones. It will ensure no other function in your region can use the reserved portion you allocated for them. Note: RC also caps the function, always plan RC according to expected traffic.

- **Runaway error loops.** An erroring function. Raising the concurrency limit just gives it more room to fail for functions that are not using reserved concurrency. Always check whether the consumer is healthy before requesting more capacity. If it is broken, cap it with reserved concurrency instead.

- **Async invocations.** Synchronous invocations get throttled visibly. However, asynchronous invocations (CloudFormation custom resources, S3 triggers, EventBridge) are queued for up to 6 hours. For these, check `AsyncEventAge` and review the `MaximumEventAgeInSeconds` for critical async functions.

In every case, throttling can benefit you and act as a circuit breaker. Only increase the limit when the consumer is healthy and the traffic is legitimate.

**Note:** This guide uses the **AWS Console** intentionally. While Infrastructure as Code (CloudFormation, CDK, Terraform) is more efficient, console-first instructions make the concepts easier to learn. Once you understand the mechanics, translating to IaC is straightforward. A CDK example ready for deployment is available [`here`](./iac).

---

## How Lambda concurrency works

Concurrency is the number of in-flight requests that your AWS Lambda function is handling at the same time. There are two types of concurrency controls available: Reserved concurrency (RC) and Provisioned concurrency (PC).

For each concurrent request, Lambda provisions a separate instance of your execution environment. Execution environments are secure, isolated environments that run on hardware-virtualized virtual machines (MicroVMs). They manage the resources required to run your function and provide lifecycle support for the function's runtime and any external extensions associated with your function.

As your functions receive more requests, Lambda automatically handles scaling the number of execution environments until you reach your account's concurrency limit.

By default, Lambda provides your account with a total concurrency limit of 1,000 concurrent executions across all functions in an AWS Region.

> When your regional concurrency limit is hit, throttling can have a cascading effect. If your application relies on Lambda as a middleware between your API, SQS, Kinesis or DynamoDB, throttles will affect how these services behave.

To better understand it, let's see this diagram.

![Lambda concurrency diagram showing multiple concurrent requests over time](./images/concurrency-5-animation-summary.png)

See the green lines, at time `t1`, there are three active environments serving three concurrent requests.

At `t2` there are **5 active environments** , so the concurrency at that moment is **5**. Requests 1 through 5 each had their "INIT" phase. Hence, they were all cold starts, and each consumed 1 concurrent execution.

Then, between `t3` and `t4`, requests 6, 7, 8 did not have "INIT". This means they reused environments that started earlier, so these requests were `warm starts`. 

Around `t4` request 9 required a new environment, hence, it was a cold start. After this, request 10 came in (warm start), and reused a pre-existing environment.

For these requests, 6 required new execution environments (cold starts) and 4 reused existing warm environments. Important to note that when observing the green line, the number of concurrent requests varied over time.

### What is my current limit?

By default, every account gets **1,000 concurrent executions per Region**. However, this is a soft limit you can increase via [Service Quotas](https://docs.aws.amazon.com/servicequotas/latest/userguide/request-quota-increase.html).

> New AWS accounts have reduced concurrency and memory quotas. AWS raises these quotas automatically based on your usage.

For more information, please refer to [Understanding and visualizing concurrency](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#understanding-concurrency).

### Regional Concurrency x RPS

Lambda also enforces a **requests per second** limit equal to 10x your concurrency limit. You can be throttled by this request rate even if concurrency is not fully utilized.

---

## 1. Understanding the ClaimedAccountConcurrency

For monitoring concurrency, Lambda exposes different metrics in CloudWatch:

| Metric | What it measures |
| :------------------------------- | :-------------------------------------------------------------- |
| `ConcurrentExecutions` | The number of active concurrent invocations at a given point in time |
| `UnreservedConcurrentExecutions` | Invocations using the remaining pool (does not consider reserved or provisioned concurrency) |
| `ClaimedAccountConcurrency` | Total concurrency **unavailable** for new on-demand invocations |

"_Ahhh, okay, if I want to monitor the regional limit, I must track the ConcurrentExecutions metric!_"

Well... partially correct. Monitoring these can help with planning distribution of your current limit across your region and how a specific function is consuming the shared pool; however, for understanding the actual utilization in your region, we must focus on ClaimedAccountConcurrency instead.

### Okay, so, what does ClaimedAccountConcurrency capture?

```
ClaimedAccountConcurrency = UnreservedConcurrentExecutions + Allocated Concurrency
```

We understand `UnreservedConcurrentExecutions`, but what about **Allocated concurrency**?

Allocated Concurrency represents the sum of both:
1. **Reserved concurrency (RC)**: ensures the function gets a guaranteed slice of the available pool for your region. The function also cannot exceed that amount or use unreserved capacity. No other function can use it, even if the function is idle. This can be configured at the function level. It consumes your pool even when not in use.
2. **Provisioned concurrency (PC)**: This allows you to have pre-initialized environments for individual functions. It counts against the pool even when the function is not processing requests.

**Notes:**

> Lambda always [keeps 100 units]((https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#:~:text=At%20the%20function,the%20function%20level.)) available for functions without RC.

> If a function has both RC and PC configured, Lambda counts only the RC (since RC should always be ≥ PC). PC is only counted separately for functions that don't have RC.

If you want to run a quick test in `us-east-1`, set reserved concurrency to a high number on a function (right below your limit), invoke another function, then check the metrics below (allow a few seconds to propagate):

```
https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#metricsV2?graph=~(metrics~(~(~(expression~'SERVICE_QUOTA*28m1*29~label~'Current*20Concurrent*20Limit~id~'e1~period~60~yAxis~'left~color~'*239467bd))~(~'AWS*2fLambda~'ConcurrentExecutions~(id~'m1~yAxis~'left~label~'ConcurrentExecutionsMetric~visible~false))~(~'.~'UnreservedConcurrentExecutions~(id~'m3))~(~'.~'ClaimedAccountConcurrency~(id~'m2~yAxis~'left~color~'*23ff7f0e))~(~(expression~'*28m2*2fe1*29*20*2a*20100~label~'*25*20Claimed~id~'e2~period~60~yAxis~'left))~(~(expression~'e1*20-*20m2~label~'Available~id~'e5~period~60~yAxis~'left~color~'*232ca02c))~(~'AWS*2fLambda~'Invocations~(id~'m4~stat~'Sum)))~sparkline~false~view~'timeSeries~stacked~false~region~'us-east-1~period~60~stat~'Maximum~liveData~false~labels~(visible~true)~legend~(position~'bottom)~start~'-PT5M~end~'P0D)&query=~'*7bAWS*2fLambda*7d
```

## 2. Calculating The Regional Limit

#### Scenario 1 - Account Concurrency Limit: 10

Let's think about this example:

| Configuration | Value |
| :------------------------------- | :-------------------------------------------------------------- |
| Account concurrency limit for your region | 10 |
| Reserved concurrency (function A) | 3 |
| Reserved concurrency (function B) | 3 |
| Provisioned concurrency (function C, PC only — no RC) | 2 |
| Active executions (unreserved concurrent executions for function D) | 1 |

For the example above, `ClaimedAccountConcurrency` is equal to 9, and we only have 1 as our current capacity for this region.

![Scenario 1 breakdown: account limit 10, ClaimedAccountConcurrency = 9, only 1 unit of headroom left](./images/breakdown-scenario-1.svg)



#### Scenario 2 - Account Concurrency Limit: 1,000

| Configuration | Value |
| :------------------------------------------------ | :----- |
| Account concurrency limit | 1,000 |
| Reserved concurrency (function A) | 400 |
| Reserved concurrency (function B) | 400 |
| Provisioned concurrency (function C, PC only — no RC) | 100 |
| Active executions (unreserved concurrent executions across functions D, E, F) | 60 |

In this example, since 60 active executions are being consumed across functions that do not have reserved or provisioned concurrency, the utilization should be 960. See calculation below:

```
ClaimedAccountConcurrency = UnreservedConcurrentExecutions + Allocated Concurrency
ClaimedAccountConcurrency = 60 + allocated concurrency (400 + 400 + 100 = 900)
```

As per the above, only 60 on-demand invocations are running, but 900 additional units are allocated (claimed by RC/PC), giving a total `ClaimedAccountConcurrency` of **960**. Actual concurrency available for new on-demand invocations is **40**.

![Scenario 2 breakdown (steady state): account limit 1,000, 60 unreserved executions + 900 allocated = 960 claimed, 40 available](./images/breakdown-scenario-2.svg)


#### Scenario 3 - 150 Unreserved Concurrency Spike

If any executions are running on _unreserved_ functions and `ClaimedAccountConcurrency` goes beyond the regional limit, you should expect throttling.

In this example, you have the Reserved concurrency for functions A, B, and C, and between functions D, E, and F you consume more than 60 concurrent environments (unreserved concurrency). Your total utilization is 960, hence available capacity is 40.

However, in this case you have a new spike and other functions are being invoked concurrently. Let's call them functions G, H, and I. Between them, **150 new concurrent executions** happen (in addition to the 60 we had before). At that point in time, only **40** concurrent executions were available, so only **40** can run immediately. For the remaining **110** concurrent executions you should expect throttling, as the number of concurrent requests will now be above the regional limit.

Calculation:
-  available concurrency = your regional limit − ClaimedAccountConcurrency
-  1,000 − 960 = 40

Now let's simulate an additional 150 unreserved concurrency:
-  150 (new spike of unreserved concurrency) − 40 (available concurrency)
Result: 110 Throttles.

![Scenario 3 breakdown (spike scenario): 150 concurrent requests arrive on unreserved functions, 40 run immediately and 110 are throttled](./images/breakdown-scenario-3.svg)


You can see more examples from [Reserved concurrency diagram](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#understanding-concurrency:~:text=To%20better%20understand%20reserved%20concurrency%2C%20consider%20the%20following%20diagram%3A) and [Provisioned Concurrency + Reserved concurrency diagram](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#:~:text=The%20previous%20example,the%20following%20diagram%3A)

---

## 3. What about creating an alarm?

> **Note:** While using Infrastructure as Code (CloudFormation, CDK, Terraform) is more efficient, I am adding instructions via console first for learning purposes. The idea is to review the concepts first; translating to IaC will be straightforward.

### Viewing the relevant metrics

a. Go to **CloudWatch** → **All metrics**

b. Click the **Source** tab

c. Paste the following JSON:

```json
{
  "metrics": [
    [
      "AWS/Lambda",
      "ConcurrentExecutions",
      {
        "id": "m1",
        "yAxis": "left",
        "label": "ConcurrentExecutionsMetric",
        "visible": false
      }
    ],
    [
      {
        "expression": "SERVICE_QUOTA(m1)",
        "label": "Current Concurrent Limit",
        "id": "e1",
        "period": 60,
        "yAxis": "left",
        "color": "#9467bd"
      }
    ],
    [
      "AWS/Lambda",
      "ClaimedAccountConcurrency",
      {
        "id": "m2",
        "yAxis": "left",
        "color": "#ff7f0e"
      }
    ],
    [
      {
        "expression": "(m2/e1) * 100",
        "label": "% Claimed",
        "id": "e2",
        "period": 60,
        "yAxis": "left"
      }
    ],
    [
      {
        "expression": "e1 - m2",
        "label": "Available",
        "id": "e5",
        "period": 60,
        "yAxis": "left",
        "color": "#2ca02c"
      }
    ]
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

d. Click **Update**

After pasting the JSON and clicking **Update**, you should see the metrics table populated with all five entries. The table shows each metric's ID, label, details (source metric or expression), statistic, and period:


![CloudWatch metrics console - line chart view with all five entries (m1, e1, m2, e2, e5) and tooltip showing current values](./images/metrics-line-chart-full.png)

| ID | Type | Purpose |
| :---- | :---------- | :------------------------------------------------------------------------------------ |
| `m1` | Metric | `ConcurrentExecutions` - used as input for `SERVICE_QUOTA()`. Hidden from the graph. |
| `e1` | Expression | `SERVICE_QUOTA(m1)` - dynamically fetches your actual regional concurrency limit |
| `m2` | Metric | `ClaimedAccountConcurrency` - the metric we want to monitor |
| `e2` | Expression | `(m2/e1) * 100` - utilization as a percentage |
| `e5` | Expression | `e1 - m2` - remaining available concurrency |

> **Why `SERVICE_QUOTA(m1)` instead of hardcoding 1,000?** The concurrency limit is a soft limit. If you've requested an increase, `SERVICE_QUOTA()` dynamically reflects your actual current limit for `ConcurrentExecutions`, so no need to update the alarm every time your quota changes.

If you would like to explore a **Pie** view, select only `ClaimedAccountConcurrency` and `Available` (checkboxes on the left). Ensure to select the specific time period where you intend to reflect on the chart visualization.


![CloudWatch metrics console - pie chart view showing ClaimedAccountConcurrency vs Available with metrics table (m1, e1, m2, e2, e5)](./images/metrics-pie-chart-full.jpeg)

Once you have the above, this confirms you have the metrics and expressions that are relevant for monitoring this regional limit.

## 4. Creating the alarm

a. From CloudWatch metrics, click the **bell icon** next to the `% Claimed` expression (`e2`)

b. Configure the alarm condition:

| Setting | Value | Why |
| :----------------------- | :------------------- | :---------------------------------------------- |
| **Metric** | `% Claimed` (e2) | The utilization percentage we calculated |
| **Threshold type** | Static | Fixed threshold value |
| **Condition** | Greater than **70** | 70% gives headroom before hitting the limit |
| **Period** | 1 minute | Matches Lambda's metric emission granularity |
| **Statistic** | Maximum | Catches spikes - average would smooth them out |
| **Datapoints to alarm** | 1 out of 1 | Triggers on the first breach |

### Configure actions

Configure an **SNS topic** as the notification target. This can deliver alerts via:

- Email
- Slack (via AWS Chatbot or a Lambda-backed integration)
- Others

### Name the alarm

Give the alarm a descriptive name and optionally add a Markdown description (rendered in the CloudWatch console):


![CloudWatch alarm details - name and Markdown description setup](./images/alarm-details-setup.png)

### Review and create

Review the configuration and click **Create alarm**.

### Alarm in action

Once active, the alarm graph shows your utilization over time:

- **Blue line** → `% Claimed` utilization
- **Threshold** → 70%
- The alarm bar at the bottom transitions from **OK** (green) to **In alarm** (red) when the threshold is breached

![CloudWatch alarm graph - % Claimed metric crossing the 70% threshold, alarm bar transitioning from OK to In alarm](./images/alarm-graph-threshold-70.png)

---

## Deploy as code (CDK)

Prefer code over the console? Everything in this article is available as standalone CDK apps.

### The main solution: the concurrency dashboard

Start here. The [`dashboard-project/`](./dashboard-project) deploys an interactive CloudWatch dashboard: regional capacity, per-function consumption, a live reserved/provisioned concurrency table, and a reclaim -> cap -> increase decision guide. It gives you the data to decide before changing anything, which is exactly the "investigate first" approach this article recommends.

![Lambda concurrency dashboard](./dashboard-project/images/lambda-concurrency-dashboard.png)

### Add an alarm to get notified

The [`iac/`](./iac) folder (TypeScript and Python) deploys the `% Claimed` alarm from this article, wired to SNS so your team is notified at 70%.

### If traffic is healthy: automate the limit increase

Once you have used the dashboard to confirm the growth is healthy and organic (not a retry storm or bad actor), you can also use the automated limit increase solution. `iac/` includes an optional Lambda that requests a quota increase when the alarm fires, then disables itself (sets its own reserved concurrency to 0) so the next breach needs a human. Use it only for traffic you have already confirmed is legitimate, since it can otherwise give a failing function more room to fail. See [`iac/README.md`](./iac/README.md) for trade-offs.

### References:

- [Monitoring concurrency](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-concurrency.html)
- [Understanding and visualizing concurrency](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#understanding-concurrency)
