# Security policy

## Reporting a vulnerability

Please do not report security problems in public issues, discussions or pull requests.

Use GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/Halumar/integration/security) of this repository and select
**Report a vulnerability**, or go directly to
[the new advisory form](https://github.com/Halumar/integration/security/advisories/new). Please
include:

- the affected version of the integration and, if relevant, of the hub and the app;
- what someone could do, and from where (home network, a paired device, the internet);
- the steps to reproduce.

Never send real access tokens, passwords, pairing codes, QR codes or backups. Replace them with
placeholders.

We confirm the report, keep you informed about the assessment and the fix, and agree with you on
when and how the issue is disclosed.

## Supported versions

Security fixes are released for the latest version only.

## Scope

In scope: the code in this repository, in particular

- the Home Assistant users and tokens the integration creates for paired devices;
- the checks of `halumar.control` and `halumar.report_location`;
- the administrator checks on the integration's actions and entities;
- the unauthenticated compliance endpoint `/api/halumar/compliance/<client_id>`;
- the connection to the Halumar Hub (token handling, discovery, hub identity);
- data that leaves the integration (events, diagnostics, logs).

The known limits of what an integration can enforce in Home Assistant are described in
[docs/security-model.md](docs/security-model.md#what-home-assistant-does-not-let-an-integration-prevent).
A report that one of these limits exists is not a vulnerability; a way around a check the
integration does make is.

Out of scope: problems in Home Assistant itself or in other integrations and apps (see the
[Home Assistant security policy](https://www.home-assistant.io/security/)), and problems in the
Halumar Hub, which has its own repository: [Halumar/hub](https://github.com/Halumar/hub/security).
