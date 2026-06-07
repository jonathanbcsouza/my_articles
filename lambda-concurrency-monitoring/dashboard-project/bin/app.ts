#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { ConcurrencyDashboardStack } from '../lib/concurrency-dashboard-stack';

const app = new cdk.App();

new ConcurrencyDashboardStack(app, 'LambdaConcurrencyDashboardStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
