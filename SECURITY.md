# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository, open **Security**, then **Report a vulnerability**. Do not open a public issue.

Include the `emet-sdk` version, your Python and OpenTelemetry versions, and the steps to reproduce. We acknowledge reports within three business days and keep you updated until a fix is released.

## Scope

In scope: anything in this repository, for example a way to make the SDK send data somewhere other than Emet, send spans started outside `emet_sdk.turn()`, write turn values onto spans that other exporters receive, leak the Emet key, or raise into the host application.

Out of scope: secrets that the redaction patterns do not recognize. Redaction is best effort; suggestions for new patterns are welcome as ordinary issues, without the secret.

## Supported versions

Security fixes go into the latest release.
