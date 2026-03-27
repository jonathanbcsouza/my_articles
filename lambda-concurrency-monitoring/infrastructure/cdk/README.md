# CDK deployment (full stack)

This CDK project creates the full monitoring and automation stack:

- CloudWatch metric math alarm based on `% Claimed = (ClaimedAccountConcurrency / SERVICE_QUOTA(ConcurrentExecutions)) * 100`
- SNS topic for notifications
- Lambda function that requests a Lambda concurrency quota increase
- IAM permissions required by the Lambda function
- Lambda invoke permission for CloudWatch alarm actions

## Prerequisites

- AWS CLI configured for the target account/region
- Node.js 18+ (for CDK CLI)
- Python 3.10+ and `pip`
- CDK bootstrapped in the account/region:

```bash
cdk bootstrap
```

## Deploy

From this folder (`infrastructure/cdk`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install -g aws-cdk
cdk synth
cdk deploy
```

## Customize before deploy

- Alarm threshold (default 70%): update `threshold` in `stack.py`
- Requested increment (default 500): update `INCREMENT` env var in `stack.py`
- Function/topic names: update `function_name` and `topic_name` in `stack.py`
- Optional fixed alarm name: add `alarm_name="your-name"` in the `cloudwatch.Alarm(...)` definition

## Notes

- The stack uses `Maximum`, 1-minute period, and `1/1` datapoints to match the article.
- If you want email notifications, add an SNS subscription after deploy (or extend this stack to add one).
