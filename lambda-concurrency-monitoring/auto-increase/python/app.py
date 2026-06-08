#!/usr/bin/env python3
import aws_cdk as cdk

from stack import LambdaConcurrencyMonitoringStack


app = cdk.App()

LambdaConcurrencyMonitoringStack(
    app,
    "LambdaConcurrencyMonitoringStack",
)

app.synth()
