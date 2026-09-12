# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for them. I aim to answer within a week and to publish
a fix and an advisory once the issue is understood.

## Supported versions

Until 1.0, only the latest release gets fixes.

## What pysqlmut does on your machine

pysqlmut runs your project's tests, or the command you give it, many times with your permissions, in
temporary copies of the project. Running it on a project runs that project's code. Treat an untrusted project
the way you would treat running its tests yourself; that is not a vulnerability in pysqlmut.

## How releases are protected

- Releases are built and uploaded by GitHub Actions with PyPI trusted publishing, so no upload token exists
  that could leak, and PyPI publishes an attestation of where each file was built.
- Every workflow action is pinned to a commit, workflows are audited with zizmor, and dependencies are audited
  with pip-audit in CI and kept current by Dependabot.
- Code scanning (CodeQL), secret scanning with push protection and private vulnerability reporting are enabled.
