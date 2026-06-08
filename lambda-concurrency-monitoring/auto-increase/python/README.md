# CDK deployment (Python)

This CDK project creates the full monitoring and automation stack:

- CloudWatch metric math alarm based on `% Claimed = (ClaimedAccountConcurrency / SERVICE_QUOTA(ConcurrentExecutions)) * 100`
- SNS topic for notifications
- Lambda function (**Python 3.14**, named `limit-increase-request`) that requests a bounded Lambda concurrency quota increase, then sets its own reserved concurrency to 0 so the next alarm firing requires a human decision
- IAM permissions required by the Lambda function (including scoped `lambda:PutFunctionConcurrency` so the function can set its own reserved concurrency)
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

From this folder (`auto-increase/python`):

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
- Proportional increase (default 10%): update the `INCREMENT_PERCENT` env var in `stack.py`
- Function/topic names: update `FUNCTION_NAME` and `topic_name` in `stack.py`. If you rename the function, also update the `lambda:PutFunctionConcurrency` policy resource (it is scoped to this function name)
- Optional fixed alarm name: add `alarm_name="your-name"` in the `cloudwatch.Alarm(...)` definition

## Re-enabling after the function sets its own reserved concurrency to 0

After an alarm fires, the Lambda function requests a quota increase and then sets its own reserved concurrency to 0 so the next alarm firing requires a human decision. You can re-enable it via [AWS console](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html#configuring-concurrency-reserved), or via CLI:

```bash
aws lambda delete-function-concurrency \
  --function-name limit-increase-request
```

To check current state:

```bash
aws lambda get-function-concurrency \
  --function-name limit-increase-request
```

`ReservedConcurrentExecutions: 0` means it cannot be invoked.

## Notes

- The stack creates an **SNS topic** (`lambda-concurrency-alerts`) but does not add any subscriptions. After deploying, open the topic in the SNS console (or via CLI) and add a subscription, typically an email, or an AWS Chatbot integration for Slack. Until a subscription is added, the alarm will still invoke the Lambda but no one will be notified.

  Example (email subscription):

  ```bash
  aws sns subscribe \
    --topic-arn arn:aws:sns:<region>:<account-id>:lambda-concurrency-alerts \
    --protocol email \
    --notification-endpoint you@example.com
  ```

  You will receive a confirmation email. Click the link to activate the subscription.

- The stack uses `Maximum`, 1-minute period, and `1/1` datapoints to match the article.
