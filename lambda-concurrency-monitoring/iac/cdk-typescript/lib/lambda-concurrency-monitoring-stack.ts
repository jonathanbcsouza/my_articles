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

const FUNCTION_NAME = 'limit-increase-request-python-314';
const TOPIC_NAME = 'lambda-concurrency-alerts';
const ALARM_THRESHOLD_PERCENT = 70;

export interface LambdaConcurrencyMonitoringStackProps extends StackProps {
  /**
   * Optional email to auto-subscribe to the SNS alarm topic. If omitted, the
   * topic is created without subscriptions and you add them manually.
   */
  readonly alertEmail?: string;
}

export class LambdaConcurrencyMonitoringStack extends Stack {
  constructor(scope: Construct, id: string, props: LambdaConcurrencyMonitoringStackProps = {}) {
    super(scope, id, props);

    const topic = new sns.Topic(this, 'ConcurrencyAlarmTopic', {
      topicName: TOPIC_NAME,
    });

    if (props.alertEmail) {
      topic.addSubscription(new snsSubscriptions.EmailSubscription(props.alertEmail));
    }

    const logGroup = new logs.LogGroup(this, 'QuotaRequesterLogGroup', {
      logGroupName: `/aws/lambda/${FUNCTION_NAME}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const quotaRequester = new lambda.Function(this, 'QuotaIncreaseRequester', {
      functionName: FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_14,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      timeout: Duration.seconds(30),
      memorySize: 128,
      environment: {
        INCREMENT_PERCENT: '0.10',
      },
      logGroup,
      description:
        'Requests Lambda concurrency quota increase when alarm transitions to ALARM, ' +
        'then sets its own reserved concurrency to 0 so the next alarm requires a human decision',
    });

    quotaRequester.addToRolePolicy(
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

    quotaRequester.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'SelfThrottleViaReservedConcurrency',
        effect: iam.Effect.ALLOW,
        actions: ['lambda:PutFunctionConcurrency'],
        resources: [`arn:aws:lambda:*:*:function:${FUNCTION_NAME}`],
      }),
    );

    quotaRequester.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:CreateServiceLinkedRole'],
        resources: ['arn:aws:iam::*:role/aws-service-role/servicequotas.amazonaws.com/*'],
        conditions: {
          StringEquals: {
            'iam:AWSServiceName': 'servicequotas.amazonaws.com',
          },
        },
      }),
    );

    const m1 = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'ConcurrentExecutions',
      statistic: 'Maximum',
      period: Duration.minutes(1),
      label: 'ConcurrentExecutionsMetric',
    });

    const e1 = new cloudwatch.MathExpression({
      expression: 'SERVICE_QUOTA(m1)',
      usingMetrics: { m1 },
      period: Duration.minutes(1),
      label: 'Current Concurrent Limit',
    });

    const m2 = new cloudwatch.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'ClaimedAccountConcurrency',
      statistic: 'Maximum',
      period: Duration.minutes(1),
      label: 'ClaimedAccountConcurrency',
    });

    const e2 = new cloudwatch.MathExpression({
      expression: '(m2/e1) * 100',
      usingMetrics: { m2, e1 },
      period: Duration.minutes(1),
      label: '% Claimed',
    });

    const alarm = new cloudwatch.Alarm(this, 'ClaimedConcurrencyUtilizationAlarm', {
      metric: e2,
      threshold: ALARM_THRESHOLD_PERCENT,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: `Triggers when account-level Lambda claimed concurrency exceeds ${ALARM_THRESHOLD_PERCENT}%`,
    });

    alarm.addAlarmAction(new cloudwatchActions.SnsAction(topic));
    // LambdaAction automatically grants invoke permission to the alarm.
    alarm.addAlarmAction(new cloudwatchActions.LambdaAction(quotaRequester));
  }
}
