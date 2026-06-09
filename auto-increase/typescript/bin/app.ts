#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { LambdaConcurrencyMonitoringStack } from '../lib/lambda-concurrency-monitoring-stack';

const app = new cdk.App();

// Optional: pass an email to auto-subscribe to the alarm SNS topic.
//   cdk deploy -c alertEmail=you@example.com
const alertEmail = app.node.tryGetContext('alertEmail') as string | undefined;

new LambdaConcurrencyMonitoringStack(app, 'LambdaConcurrencyMonitoringStack', {
  alertEmail,
});

app.synth();
