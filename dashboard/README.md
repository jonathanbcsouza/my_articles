# Lambda Concurrency Dashboard

This project enables you to monitor Lambda regional concurrency in your account and Region through a deployable CloudWatch dashboard. 

From the dashboard you will be able to see alarm state and regional utilization, a sortable per function table that shows allocated Reserved and Provisioned Concurrency, peak concurrency, invocations, errors, and throttles per function. 

Also displays top 10 throttling and erroring functions. Everything visible at a glance so you can make quick and more effective decisions. 

When scaling is justified, there is also a button for logging a limit increase request. No need to go to service quotas! 

This solution also builds an alarm notifies an SNS topic when utilization crosses the  **% ClaimedAccountConcurrency > 70%** (utilization) threshold . 

## Dashboard preview

### Regional capacity, alarm, and top consumers

![Lambda concurrency dashboard, alarm state, % Claimed, claimed vs available, trend, top consumers, throttles, errors, and unreserved pool](../docs/images/dashboard/lambda-concurrency-dashboard-1.png)

### Per-function allocation and activity

![Lambda concurrency dashboard, all functions table with RC, PC, peak concurrency, invocations, errors, throttles, and quota increase panel](../docs/images/dashboard/lambda-concurrency-dashboard-2.png)

### Provisioned concurrency and decision guide

![Lambda concurrency dashboard, PC utilization and spillover by function, plus reclaim, cap, increase guide](../docs/images/dashboard/lambda-concurrency-dashboard-3.png)

## Deploy

```bash
cd dashboard
npm install
npx cdk bootstrap   # once per account/Region
npx cdk deploy
```

Uses `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION` from your environment.

To auto-subscribe an email to the alarm topic:

```bash
npx cdk deploy -c alertEmail=you@example.com
```

Open the dashboard: **CloudWatch -> Dashboards -> `lambda-concurrency`**

## Sample alarm email

When `% Claimed` crosses **70%**, CloudWatch publishes JSON to SNS. The email subject is set by AWS, for example:

```
ALARM: "lambda-concurrency-claimed-pct-70" in US East (N. Virginia)
```

The body is JSON. The human-readable part to read first is **`AlarmDescription`** (configured in CDK with a direct dashboard link and investigate-first guidance):

```
Lambda regional concurrency claimed has exceeded 70% of the account limit.

ClaimedAccountConcurrency = allocated RC/PC + unreserved executions in use.
New on-demand invocations may throttle soon if usage keeps climbing.

What to do:
1. Open the concurrency dashboard and identify top consumers / wasted RC.
2. Make your decisions. These can be either setting RC, removing it or many others.

Dashboard: https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/lambda-concurrency
Alarm: https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#alarmsV2:alarm/lambda-concurrency-claimed-pct-70

---
State change: 2026-06-08T01:15:00.000+0000
Reason: Threshold Crossed: 1 datapoint [72.5 (08/06/26 01:14:00)] was greater than the threshold (70.0).
Region: us-east-1 | Account: 123456789012
```

## Resources this CDK deploys

| Resource | Name |
|---|---|
| CloudFormation stack | `LambdaConcurrencyDashboardStack` |
| CloudWatch dashboard | `lambda-concurrency` |
| CloudWatch alarm | `lambda-concurrency-claimed-pct-70` (% Claimed > 70%, SNS only) |
| SNS topic | `lambda-concurrency-alerts` |
| Lambda function | `concurrency-dashboard-widget` |
| CloudWatch log group | `/aws/lambda/concurrency-dashboard-widget` (7-day retention) |
| IAM role + policies | Lambda execution role, read access to Lambda and CloudWatch, Service Quotas on the quota button |
| Lambda permission | Allows CloudWatch to invoke the custom widget |

The alarm here is **notification only**. The optional auto-increase Lambda (`limit-increase-request`) lives in [`../auto-increase`](../auto-increase).

## Customize

| What | Where |
|---|---|
| Dashboard name, 70% threshold | [`lib/concurrency-dashboard-stack.ts`](lib/concurrency-dashboard-stack.ts) |
| Default quota increment (+10) | `DEFAULT_INCREMENT` in [`lambda/handler.py`](lambda/handler.py) |

## After deploy

Once deployed, ensure you can receive alarm notifications.

If you passed `alertEmail` during deploy, AWS sends a confirmation message to that address. Confirm the subscription before alarms can reach you. See [Confirm your Amazon SNS subscription](https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.confirm.html).

If you did not pass `alertEmail`, open the SNS console, select the `lambda-concurrency-alerts` topic, and create a subscription for email, HTTPS, or another supported endpoint. Confirm each subscription the same way.

To forward alerts to Slack or other destinations, subscribe those endpoints to the `lambda-concurrency-alerts` topic. See [Configure Amazon SNS to send messages for alerts to other destinations (Slack)](https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-alertmanager-SNS-otherdestinations.html#AMP-alertmanager-SNS-otherdestinations-Slack).

## Destroy

```bash
npx cdk destroy
```
