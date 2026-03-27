import logging
import os

import boto3


logger = logging.getLogger()
logger.setLevel(logging.INFO)

SERVICE_CODE = "lambda"
QUOTA_CODE = "L-B99A9384"  # Concurrent executions
INCREMENT = float(os.environ.get("INCREMENT", "500"))

client = boto3.client("service-quotas")


def has_pending_request():
    history = client.list_requested_service_quota_change_history_by_quota(
        ServiceCode=SERVICE_CODE,
        QuotaCode=QUOTA_CODE,
    )
    return any(
        quota["Status"] in ("PENDING", "CASE_OPENED")
        for quota in history.get("RequestedQuotas", [])
    )


def lambda_handler(event, context):
    alarm_name = event.get("alarmData", {}).get("alarmName", "unknown")
    logger.info(f"Alarm triggered: {alarm_name}")

    if has_pending_request():
        logger.info("Skipping - a quota increase request is already pending")
        return {"status": "SKIPPED", "reason": "pending request exists"}

    current = client.get_service_quota(
        ServiceCode=SERVICE_CODE,
        QuotaCode=QUOTA_CODE,
    )
    current_value = current["Quota"]["Value"]
    desired_value = current_value + INCREMENT

    response = client.request_service_quota_increase(
        ServiceCode=SERVICE_CODE,
        QuotaCode=QUOTA_CODE,
        DesiredValue=desired_value,
    )

    status = response["RequestedQuota"]["Status"]
    logger.info(
        f"Requested increase: {current_value} -> {desired_value} | Status: {status}"
    )

    return {
        "current": current_value,
        "desired": desired_value,
        "status": status,
    }
