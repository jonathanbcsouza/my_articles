#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { ConcurrencyDashboardStack } from '../lib/concurrency-dashboard-stack';

const app = new cdk.App();

// Optional email for the SNS alarm topic:
//   npx cdk deploy -c alertEmail=you@example.com
const alertEmail = app.node.tryGetContext('alertEmail');

new ConcurrencyDashboardStack(app, 'LambdaConcurrencyDashboardStack', {
  alertEmail,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
