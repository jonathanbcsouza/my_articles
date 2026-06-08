# CDK deployment (TypeScript)

TypeScript CDK implementation of the same stack as `../python` (Python). Both produce functionally equivalent CloudFormation templates and deploy the same Python Lambda handler.

Resources created:

- CloudWatch metric math alarm on `% Claimed = (ClaimedAccountConcurrency / SERVICE_QUOTA(ConcurrentExecutions)) * 100`
- SNS topic for notifications
- Lambda function (**Python 3.14**, named `limit-increase-request`) that requests a bounded Lambda concurrency quota increase, then sets its own reserved concurrency to 0 so the next alarm firing requires a human decision
- IAM permissions required by the Lambda (including scoped `lambda:PutFunctionConcurrency`)
- CloudWatch alarm → Lambda + SNS actions

## Prerequisites

- AWS CLI configured for the target account/region
- Node.js 18+ and npm
- CDK bootstrapped in the account/region:

  ```bash
  npx cdk bootstrap
  ```

## Deploy

From this folder (`auto-increase/typescript`):

```bash
npm install
npx cdk synth
npx cdk deploy
```

### Optional: auto-subscribe an email to the alarm topic

Pass an email via CDK context to have the stack create an email subscription on the SNS topic:

```bash
npx cdk deploy -c alertEmail=you@example.com
```

You will receive an AWS SNS confirmation email; click the link to activate the subscription. If you omit `alertEmail`, the topic is created with no subscriptions and you add them yourself (see Notes below).

## Customize before deploy

- Alarm threshold (default 70%): update `ALARM_THRESHOLD_PERCENT` in `lib/lambda-concurrency-monitoring-stack.ts`
- Proportional increase (default 10%): update the `INCREMENT_PERCENT` env var in the same file
- Function/topic names: update `FUNCTION_NAME` / `TOPIC_NAME` constants — note that if you rename the function, the `lambda:PutFunctionConcurrency` policy resource (scoped to this function name) updates automatically because both read from the same constant

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

- The stack creates an **SNS topic** (`lambda-concurrency-alerts`). If you did not pass `-c alertEmail=...`, it has no subscriptions. Open the topic in the SNS console (or use the CLI) and add a subscription — typically an email, or an AWS Chatbot integration for Slack. Until a subscription is added, the alarm will still invoke the Lambda but no one will be notified.

  Example (email subscription after deploy):

  ```bash
  aws sns subscribe \
    --topic-arn arn:aws:sns:<region>:<account-id>:lambda-concurrency-alerts \
    --protocol email \
    --notification-endpoint you@example.com
  ```

- The stack uses `Maximum`, 1-minute period, and `1/1` datapoints to match the article.

## Parity with the Python CDK

The TypeScript stack produces the same resources as `../python/stack.py` — same function name, same IAM statements (including the same Sid `SelfThrottleViaReservedConcurrency`), same alarm configuration, same Lambda handler (shared verbatim). Pick whichever language you prefer.
