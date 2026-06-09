import * as path from 'path';
import { Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';

const DASHBOARD_NAME = 'lambda-concurrency';
const WIDGET_FUNCTION_NAME = 'concurrency-dashboard-widget';
const TOPIC_NAME = 'lambda-concurrency-alerts';
const ALARM_NAME = 'lambda-concurrency-claimed-pct-70';
const ALARM_THRESHOLD_PERCENT = 70;
const FULL_WIDTH = 24;

export interface ConcurrencyDashboardStackProps extends StackProps {
  /**
   * Optional email to auto-subscribe to the SNS alarm topic. If omitted, the
   * topic is created without subscriptions and you add them manually.
   */
  readonly alertEmail?: string;
}

/** Pie chart with segment labels always visible (CDK GraphWidget lacks labels prop). */
class LabeledPieWidget extends cloudwatch.GraphWidget {
  public override toJson(): any[] {
    const json = super.toJson();
    json[0].properties.labels = { visible: true };
    return json;
  }
}

/**
 * SEARCH expression per function — legend shows clean FunctionName values
 * (Metrics Insights prefixes labels with "1 - " or the raw SQL string).
 */
class SearchByFunction extends cloudwatch.MathExpression {
  constructor(metricName: string, stat: string, period: Duration) {
    super({
      expression:
        `SEARCH('{AWS/Lambda,FunctionName} MetricName="${metricName}"', ` +
        `'${stat}', ${period.toSeconds()})`,
      usingMetrics: {},
      period,
    });
  }
}

/**
 * Graph widget for SEARCH expressions. CDK nests metrics as entry.value[] and
 * defaults label to the full expression string; set label to '' so the legend
 * shows only the FunctionName from each series.
 */
class SearchGraphWidget extends cloudwatch.GraphWidget {
  public override toJson(): any[] {
    const json = super.toJson();
    const metrics = json[0]?.properties?.metrics;
    if (Array.isArray(metrics)) {
      for (const entry of metrics) {
        const value = entry?.value;
        if (!Array.isArray(value)) continue;
        for (const part of value) {
          if (part && typeof part === 'object' && 'expression' in part) {
            part.label = '';
          }
        }
      }
    }
    return json;
  }
}

export class ConcurrencyDashboardStack extends Stack {
  constructor(scope: Construct, id: string, props: ConcurrencyDashboardStackProps = {}) {
    super(scope, id, props);

    // --- Custom-widget Lambda (renders the live RC + PC table) ---
    const widgetLogGroup = new logs.LogGroup(this, 'WidgetLogGroup', {
      logGroupName: `/aws/lambda/${WIDGET_FUNCTION_NAME}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const widgetFn = new lambda.Function(this, 'ConcurrencyDashboardWidget', {
      functionName: WIDGET_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      timeout: Duration.seconds(60),
      memorySize: 128,
      logGroup: widgetLogGroup,
      description:
        'CloudWatch custom widget: renders reserved + provisioned concurrency ' +
        'per function and offers a confirmed quota-increase action',
    });

    // Read path: assemble the RC/PC snapshot from the Lambda control plane,
    // plus average concurrency per function over the dashboard time range.
    widgetFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'lambda:ListFunctions',
          'lambda:GetFunctionConcurrency',
          'lambda:ListProvisionedConcurrencyConfigs',
          'lambda:GetAccountSettings',
          'cloudwatch:GetMetricData',
          'cloudwatch:DescribeAlarms',
        ],
        resources: ['*'],
      }),
    );

    // Action path: the confirmed "request limit increase" button.
    widgetFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'servicequotas:GetServiceQuota',
          'servicequotas:RequestServiceQuotaIncrease',
          'servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota',
        ],
        resources: ['*'],
      }),
    );

    widgetFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:CreateServiceLinkedRole'],
        resources: ['arn:aws:iam::*:role/aws-service-role/servicequotas.amazonaws.com/*'],
        conditions: {
          StringEquals: { 'iam:AWSServiceName': 'servicequotas.amazonaws.com' },
        },
      }),
    );

    // Allow CloudWatch custom widgets to invoke the function.
    widgetFn.addPermission('AllowCloudWatchCustomWidgetInvoke', {
      principal: new iam.ServicePrincipal('cloudwatch.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceAccount: this.account,
    });

    // --- Shared metric building blocks ---
    const period = Duration.minutes(1);

    const concurrentExecutions = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'ConcurrentExecutions',
      statistic: 'Maximum',
      period,
      label: 'ConcurrentExecutions',
    });

    const limit = new cloudwatch.MathExpression({
      expression: 'SERVICE_QUOTA(m1)',
      usingMetrics: { m1: concurrentExecutions },
      period,
      label: 'Current Concurrent Limit',
    });

    const claimed = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'ClaimedAccountConcurrency',
      statistic: 'Maximum',
      period,
      label: 'ClaimedAccountConcurrency',
    });

    const percentClaimed = new cloudwatch.MathExpression({
      expression: '(m2/e1) * 100',
      usingMetrics: { m2: claimed, e1: limit },
      period,
      label: '% Claimed',
    });

    const available = new cloudwatch.MathExpression({
      expression: 'e1 - m2',
      usingMetrics: { m2: claimed, e1: limit },
      period,
      label: 'Available',
    });

    // --- Alarm: % Claimed > 70%, notify via SNS only (no Lambda action) ---
    const alarmTopic = new sns.Topic(this, 'ConcurrencyAlarmTopic', {
      topicName: TOPIC_NAME,
    });

    if (props.alertEmail) {
      alarmTopic.addSubscription(new snsSubscriptions.EmailSubscription(props.alertEmail));
    }

    const dashboardUrl =
      `https://${this.region}.console.aws.amazon.com/cloudwatch/home` +
      `?region=${this.region}#dashboards/dashboard/${DASHBOARD_NAME}`;

    const alarmUrl =
      `https://${this.region}.console.aws.amazon.com/cloudwatch/home` +
      `?region=${this.region}#alarmsV2:alarm/${encodeURIComponent(ALARM_NAME)}`;

    const claimedAlarm = new cloudwatch.Alarm(this, 'ClaimedConcurrencyUtilizationAlarm', {
      alarmName: ALARM_NAME,
      metric: percentClaimed,
      threshold: ALARM_THRESHOLD_PERCENT,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: [
        'Lambda regional concurrency claimed has exceeded 70% of the account limit.',
        '',
        'ClaimedAccountConcurrency = allocated RC/PC + unreserved executions in use.',
        'New on-demand invocations may throttle soon if usage keeps climbing.',
        '',
        'What to do:',
        '1. Open the concurrency dashboard and identify top consumers / wasted RC.',
        '2. Reclaim → cap → increase (do not raise the limit during a retry storm).',
        '',
        `Dashboard: ${dashboardUrl}`,
        `Alarm: ${alarmUrl}`,
      ].join('\n'),
    });

    // Notification only - this dashboard intentionally takes no automated action.
    claimedAlarm.addAlarmAction(new cloudwatchActions.SnsAction(alarmTopic));

    const unreserved = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'UnreservedConcurrentExecutions',
      statistic: 'Maximum',
      period,
      label: 'UnreservedConcurrentExecutions',
    });

    const searchByFunction = (stat: string, metricName: string): SearchByFunction =>
      new SearchByFunction(metricName, stat, period);

    // --- Dashboard ---
    const dashboard = new cloudwatch.Dashboard(this, 'ConcurrencyDashboard', {
      dashboardName: DASHBOARD_NAME,
      defaultInterval: Duration.hours(3),
    });

    // Row 1 - Regional capacity headline: alarm ON/OFF, utilization, split, trend
    dashboard.addWidgets(
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'Alarm state',
        params: { action: 'alarm_state', alarmName: ALARM_NAME },
        width: 4,
        height: 6,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: false,
      }),
      new cloudwatch.SingleValueWidget({
        title: '% Claimed (utilization)',
        metrics: [percentClaimed],
        width: 4,
        height: 6,
      }),
      new LabeledPieWidget({
        title: 'Claimed vs Available',
        left: [claimed, available],
        view: cloudwatch.GraphWidgetView.PIE,
        width: 6,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Claimed concurrency vs limit',
        left: [claimed, limit],
        right: [percentClaimed],
        rightYAxis: { min: 0, max: 100, label: '% Claimed', showUnits: false },
        width: 10,
        height: 6,
        rightAnnotations: [
          { value: ALARM_THRESHOLD_PERCENT, label: '70% threshold', color: cloudwatch.Color.ORANGE },
        ],
      }),
    );

    // Row 2 - Alarm graph + top consumers
    dashboard.addWidgets(
      new cloudwatch.AlarmWidget({
        title: 'ClaimedAccountConcurrency % alarm',
        alarm: claimedAlarm,
        leftYAxis: { min: 0, max: 100, label: '% Claimed', showUnits: false },
        width: 12,
        height: 6,
      }),
      new SearchGraphWidget({
        title: 'Top consumers by function (over time)',
        left: [searchByFunction('Maximum', 'ConcurrentExecutions')],
        width: 12,
        height: 6,
        legendPosition: cloudwatch.LegendPosition.BOTTOM,
      }),
    );

    // Row 3 - Health signals + shared pool
    dashboard.addWidgets(
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'Top 10 throttled functions',
        params: { action: 'top_n_bar', metric: 'throttles', limit: 10 },
        width: 8,
        height: 6,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'Top 10 functions by errors',
        params: { action: 'top_n_bar', metric: 'errors', limit: 10 },
        width: 8,
        height: 6,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
      new cloudwatch.GraphWidget({
        title: 'Unreserved (shared pool) in use',
        left: [unreserved],
        width: 8,
        height: 6,
      }),
    );

    // Row 4 - All functions: allocation + activity table (with request-limit panel)
    dashboard.addWidgets(
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'All functions: allocation + activity',
        width: FULL_WIDTH,
        height: 12,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
    );

    // Row 5 - Provisioned concurrency explainer (only relevant when PC is used)
    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          '### Provisioned concurrency (PC) charts',
          '',
          'These apply **only to functions with provisioned concurrency configured**. If you do not use PC, these charts will be empty.',
          '',
          '- **Utilization** — how much of the provisioned capacity was in use (0–100%). Low utilization means you are paying for idle warm environments.',
          '- **Spillover** — invocations that exceeded provisioned capacity and ran on on-demand (or were rejected, depending on configuration). High spillover means PC is set too low for actual traffic.',
        ].join('\n'),
        width: FULL_WIDTH,
        height: 3,
      }),
    );

    // Row 6 - Provisioned concurrency metrics (custom bars — SEARCH labels PC
    // metrics with the metric name, not FunctionName)
    dashboard.addWidgets(
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'PC utilization % by function',
        params: { action: 'pc_bar', metric: 'utilization' },
        width: 12,
        height: 6,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'PC spillover invocations by function',
        params: { action: 'pc_bar', metric: 'spillover' },
        width: 12,
        height: 6,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
    );

    // Row 7 - Decision aid
    const region = this.region;
    const quotaConsole = `https://${region}.console.aws.amazon.com/servicequotas/home/services/lambda/quotas/L-B99A9384`;
    const requestsConsole = `https://${region}.console.aws.amazon.com/servicequotas/home/requests?region=${region}`;
    const docLink = (href: string, label: string) => `[${label}](${href})`;
    const DOC_RC = 'https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html';
    const DOC_PC = 'https://docs.aws.amazon.com/lambda/latest/dg/provisioned-concurrency.html';
    const DOC_ALLOC =
      'https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html#understanding-concurrency';
    const DOC_MONITOR = 'https://docs.aws.amazon.com/lambda/latest/dg/monitoring-concurrency.html';
    const DOC_QUOTA =
      'https://docs.aws.amazon.com/servicequotas/latest/userguide/request-quota-increase.html';
    const DOC_LIMITS = 'https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html';

    const docs = (links: string[]) => `    Docs: ${links.join(' · ')}`;

    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          '## Decision guide: reclaim -> cap -> increase',
          '',
          '1. **Reclaim** - is allocated concurrency (RC/PC table above) much larger than usage? Trim it; that will help to free capacity in this region.',
          '',
          docs([
            docLink(DOC_RC, 'Reserved concurrency'),
            docLink(DOC_PC, 'Provisioned concurrency'),
            docLink(DOC_ALLOC, 'Allocation basics'),
          ]),
          '',
          '2. **Cap** - specific function showing high error rate + rising concurrency (retry storm)? You can cap it with reserved concurrency.',
          '',
          docs([
            docLink(DOC_RC, 'Reserved concurrency'),
            docLink(DOC_MONITOR, 'Monitoring concurrency'),
          ]),
          '',
          '3. **Increase** - is healthy demand broadly exceeding capacity with no waste and no bad actor? Consider a limit increase.',
          '',
          docs([
            docLink(DOC_QUOTA, 'Quota increase guide'),
            docLink(DOC_LIMITS, 'Lambda limits'),
            docLink(quotaConsole, 'Open quota'),
            docLink(requestsConsole, 'View requests'),
          ]),
          '',
        ].join('\n'),
        width: FULL_WIDTH,
        height: 8,
      }),
    );
  }
}
