from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
)
from constructs import Construct


class LambdaConcurrencyMonitoringStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        topic = sns.Topic(
            self,
            "ConcurrencyAlarmTopic",
            topic_name="lambda-concurrency-alerts",
        )

        log_group = logs.LogGroup(
            self,
            "QuotaRequesterLogGroup",
            log_group_name="/aws/lambda/limit-increase-request-python",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        quota_requester = lambda_.Function(
            self,
            "QuotaIncreaseRequester",
            function_name="limit-increase-request-python",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "INCREMENT": "500",
            },
            log_group=log_group,
            description=(
                "Requests Lambda concurrency quota increase "
                "when alarm transitions to ALARM"
            ),
        )

        quota_requester.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "servicequotas:GetServiceQuota",
                    "servicequotas:RequestServiceQuotaIncrease",
                    "servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota",
                ],
                resources=["*"],
            )
        )

        quota_requester.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    "arn:aws:iam::*:role/aws-service-role/"
                    "servicequotas.amazonaws.com/*"
                ],
                conditions={
                    "StringEquals": {
                        "iam:AWSServiceName": "servicequotas.amazonaws.com",
                    }
                },
            )
        )

        m1 = cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="ConcurrentExecutions",
            statistic="Maximum",
            period=Duration.minutes(1),
            label="ConcurrentExecutionsMetric",
        )

        e1 = cloudwatch.MathExpression(
            expression="SERVICE_QUOTA(m1)",
            using_metrics={"m1": m1},
            period=Duration.minutes(1),
            label="Current Concurrent Limit",
        )

        m2 = cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="ClaimedAccountConcurrency",
            statistic="Maximum",
            period=Duration.minutes(1),
            label="ClaimedAccountConcurrency",
        )

        e2 = cloudwatch.MathExpression(
            expression="(m2/e1) * 100",
            using_metrics={"m2": m2, "e1": e1},
            period=Duration.minutes(1),
            label="% Claimed",
        )

        alarm = cloudwatch.Alarm(
            self,
            "ClaimedConcurrencyUtilizationAlarm",
            metric=e2,
            threshold=70,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=(
                "Triggers when account-level Lambda claimed concurrency "
                "exceeds 70%"
            ),
        )

        alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))
        # LambdaAction automatically grants invoke permission to the alarm.
        alarm.add_alarm_action(cloudwatch_actions.LambdaAction(quota_requester))
