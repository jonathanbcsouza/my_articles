import os
import boto3
import logging
import math

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SERVICE_CODE = "lambda"
QUOTA_CODE = "L-B99A9384"  # Concurrent executions
INCREMENT_PERCENT = float(os.environ.get("INCREMENT_PERCENT", "0.10"))

quotas = boto3.client("service-quotas")
lambda_client = boto3.client("lambda")


def has_pending_request():
    paginator = quotas.get_paginator(
        "list_requested_service_quota_change_history_by_quota"
    )
    for page in paginator.paginate(ServiceCode=SERVICE_CODE, QuotaCode=QUOTA_CODE):
        for r in page.get("RequestedQuotas", []):
            if r["Status"] in ("PENDING", "CASE_OPENED"):
                return True
    return False


def throttle_self(function_name):
    """Set this function's reserved concurrency to 0 so future alarm
    invocations are throttled by Lambda itself. Re-enable with:
        aws lambda delete-function-concurrency --function-name <name>
    """
    lambda_client.put_function_concurrency(
        FunctionName=function_name,
        ReservedConcurrentExecutions=0,
    )
    logger.warning(
        f"Auto-increase DISABLED: set {function_name} RC=0. "
        f"A human must re-enable the function to allow future auto-increases."
    )


def lambda_handler(event, context):
    alarm_name = event.get("alarmData", {}).get("alarmName", "unknown")
    logger.info(f"Alarm triggered: {alarm_name}")

    if has_pending_request():
        logger.info("Skipping: a quota increase request is already pending")
        throttle_self(context.function_name)
        return {"status": "SKIPPED", "reason": "pending request exists"}

    current = quotas.get_service_quota(
        ServiceCode=SERVICE_CODE, QuotaCode=QUOTA_CODE
    )
    current_value = current["Quota"]["Value"]

    # Calculate proportional increase, rounded up
    increment = math.ceil(current_value * INCREMENT_PERCENT)
    desired_value = current_value + increment

    response = quotas.request_service_quota_increase(
        ServiceCode=SERVICE_CODE,
        QuotaCode=QUOTA_CODE,
        DesiredValue=desired_value,
    )

    status = response["RequestedQuota"]["Status"]
    logger.info(
        f"Requested increase: {current_value} -> {desired_value} "
        f"(+{increment}, {INCREMENT_PERCENT * 100:.0f}%) | Status: {status}"
    )

    # One-shot safety net: throttle so the next alarm requires a human decision
    throttle_self(context.function_name)

    return {
        "current": current_value,
        "desired": desired_value,
        "increment": increment,
        "increment_percent": INCREMENT_PERCENT * 100,
        "status": status,
    }
