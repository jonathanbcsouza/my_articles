import * as path from 'path';
import { Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';

const DASHBOARD_NAME = 'lambda-concurrency';
const WIDGET_FUNCTION_NAME = 'concurrency-dashboard-widget';
const ALARM_THRESHOLD_PERCENT = 70;
const FULL_WIDTH = 24;

/** Pie chart with segment labels always visible (CDK GraphWidget lacks labels prop). */
class LabeledPieWidget extends cloudwatch.GraphWidget {
  public override toJson(): any[] {
    const json = super.toJson();
    json[0].properties.labels = { visible: true };
    return json;
  }
}

export class ConcurrencyDashboardStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps = {}) {
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

    const unreserved = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'UnreservedConcurrentExecutions',
      statistic: 'Maximum',
      period,
      label: 'UnreservedConcurrentExecutions',
    });

    // Helper: a per-function top-N Metrics Insights expression. Without an
    // explicit GROUP BY these per-function metrics would be aggregated across
    // the whole Region, which is misleading (a utilization ratio especially).
    const topNByFunction = (
      stat: string,
      metricName: string,
      limit = 10,
    ): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression:
          `SELECT ${stat}(${metricName}) FROM SCHEMA("AWS/Lambda", FunctionName) ` +
          `GROUP BY FunctionName ORDER BY ${stat}() DESC LIMIT ${limit}`,
        usingMetrics: {},
        period,
        label: '',
      });

    // --- Dashboard ---
    const dashboard = new cloudwatch.Dashboard(this, 'ConcurrencyDashboard', {
      dashboardName: DASHBOARD_NAME,
      defaultInterval: Duration.hours(3),
    });

    // Row 1 - Regional capacity (the headline)
    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: '% Claimed (utilization)',
        metrics: [percentClaimed],
        width: 6,
        height: 6,
      }),
      new LabeledPieWidget({
        title: 'Claimed vs Available',
        left: [claimed, available],
        view: cloudwatch.GraphWidgetView.PIE,
        width: 8,
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

    // Row 2 - Consumption (who is using the pool)
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Unreserved (shared pool) in use',
        left: [unreserved],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Top 10 consumers (over time)',
        left: [topNByFunction('MAX', 'ConcurrentExecutions')],
        width: 12,
        height: 6,
      }),
    );

    // Row 3 - Allocated concurrency table (RC + PC) via custom widget
    dashboard.addWidgets(
      new cloudwatch.CustomWidget({
        functionArn: widgetFn.functionArn,
        title: 'All functions: allocated concurrency + peak usage',
        width: FULL_WIDTH,
        height: 10,
        updateOnRefresh: true,
        updateOnResize: true,
        updateOnTimeRangeChange: true,
      }),
    );

    // Row 4 - Provisioned concurrency health (per-function top-N)
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'PC utilization by function (top 10)',
        left: [topNByFunction('MAX', 'ProvisionedConcurrencyUtilization')],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'PC spillover by function (top 10)',
        left: [topNByFunction('SUM', 'ProvisionedConcurrencySpilloverInvocations')],
        width: 12,
        height: 6,
      }),
    );

    // Row 5 - Early warning / health
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Top 10 throttled functions',
        left: [
          new cloudwatch.MathExpression({
            expression:
              'SELECT SUM(Throttles) FROM SCHEMA("AWS/Lambda", FunctionName) ' +
              'GROUP BY FunctionName ORDER BY SUM() DESC LIMIT 10',
            usingMetrics: {},
            period,
            label: '',
          }),
        ],
        view: cloudwatch.GraphWidgetView.BAR,
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Error rate by function (%)',
        left: [
          new cloudwatch.MathExpression({
            expression:
              'SELECT AVG(Errors) FROM SCHEMA("AWS/Lambda", FunctionName) ' +
              'GROUP BY FunctionName ORDER BY AVG() DESC LIMIT 10',
            usingMetrics: {},
            period,
            label: '',
          }),
        ],
        view: cloudwatch.GraphWidgetView.BAR,
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Async event age by function (top 10)',
        left: [topNByFunction('MAX', 'AsyncEventAge')],
        width: 8,
        height: 6,
      }),
    );

    // Row 6 - Decision aid
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
