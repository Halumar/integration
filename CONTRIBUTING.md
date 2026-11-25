# Contributing to the Halumar integration

Thank you for helping to improve the Halumar integration for Home Assistant. The design is
described in [docs/architecture.md](docs/architecture.md).

## License

The integration is licensed under the [Apache License 2.0](LICENSE) (`Apache-2.0`), see
[NOTICE](NOTICE). By contributing, you agree that your contribution is licensed under the same
license (Apache-2.0 section 5).

## Developer Certificate of Origin (DCO)

Every commit must be signed off. With the sign-off you confirm the
[Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change or
otherwise have the right to submit it under the project's license.

Add the sign-off with `git commit -s`. It appends a line with your name and e-mail:

```text
Signed-off-by: Your Name <you@example.com>
```

Use your real name. The [DCO app](https://github.com/apps/dco) checks every pull request; a commit
without a sign-off can be fixed with `git commit --amend -s` or `git rebase --signoff develop`.

There is no separate Contributor License Agreement for this repository. (The
[Halumar Hub](https://github.com/Halumar/hub) uses one.)

## Development setup

You need Python 3.14 (current Home Assistant) and, for the minimum-version tests, Python 3.13. The
commands use `uv`; `python -m venv` and `pip` work the same way.

```sh
git clone https://github.com/Halumar/integration.git
cd integration
uv venv --python 3.14 .venv
. .venv/bin/activate
uv pip install -r requirements_test.txt -r .github/requirements-ci.txt
```

`requirements_test.txt` pins `pytest-homeassistant` (the current Home Assistant release);
`.github/requirements-ci.txt` pins ruff, mypy, pytest-cov and pylint.

To try the integration in a Home Assistant instance, copy or link `custom_components/halumar` into
its `custom_components` folder. A Halumar Hub app is needed to set it up.

## Checks

CI (`.github/workflows/validate.yml`) runs these on every pull request. Run them locally before you
open one.

| Check | Command | Blocking |
|---|---|---|
| Lint | `ruff check custom_components tests` | yes |
| Format | `ruff format --check custom_components tests` | yes |
| Types (strict) | `mypy custom_components` | yes |
| Licenses | `python scripts/ci/check-licenses.py` | yes |
| Tests, current Home Assistant, with coverage gate | `CONTRACTS_DIR=.contracts python -m pytest tests -p pytest_homeassistant -p pytest_cov --cov=custom_components --cov-report=json:coverage.json` then `python scripts/ci/check-coverage.py coverage.json` (more than 95 % per module, 100 % for `config_flow.py`) | yes |
| Tests, minimum Home Assistant (2025.11) | In a separate Python 3.13 environment: `uv pip install -r requirements_test_min.txt`, then `python -m pytest tests` | yes |
| Tests, newest Home Assistant pre-release | `uv pip install --upgrade --prerelease=allow pytest-homeassistant`, then the tests | no |
| hassfest | [home-assistant/actions/hassfest](https://github.com/home-assistant/actions) (Docker) | yes |
| HACS | [hacs/action](https://github.com/hacs/action), category `integration`; runs once the repository is public | yes |
| Contract, brand and public texts | see below | yes |
| Home Assistant pylint plugin | `scripts/ci/pylint-ha.sh` with the plugin from Home Assistant core | no |
| Deprecation report | the tests with warnings logged | no |

### Contract, brand and public-text checks

These need a checkout of [Halumar/contracts](https://github.com/Halumar/contracts) at the tag in
[`.github/contracts-ref`](.github/contracts-ref), in the folder `.contracts` (ignored by git):

```sh
git clone --depth 1 --branch "$(cat .github/contracts-ref)" \
  https://github.com/Halumar/contracts.git .contracts
scripts/ci/contracts-check.sh
scripts/brand-check.sh
python .contracts/tools/publiclint/publiclint.py .
```

With this checkout, run the tests as `CONTRACTS_DIR=.contracts python -m pytest tests -p
pytest_homeassistant`; without `CONTRACTS_DIR` the shared test vectors of the protocol are skipped.

- `contracts-check.sh`: the domain equals the protocol's namespace, and `control_allowlist.json` and
  `event_kinds.json` are byte-identical to the pinned protocol files.
- `brand-check.sh`: `brand.py` and the brand fields of the manifest and `hacs.json` match the
  protocol's brand file, and no brand or namespace literal appears outside the allowed files
  (`.brandlint.toml`). `scripts/brand-check.sh --write` regenerates the brand files.
- `publiclint.py`: no ticket references, local paths, test-network addresses, e-mail addresses or
  secrets in public files.

## Branches

- `main` is the default branch. It holds released versions only; every push to `main` runs the
  release workflow ([docs/releasing.md](docs/releasing.md)).
- `develop` is where changes land. Pull requests go against `develop`; Dependabot opens its pull
  requests there too.

CI (`validate.yml`) runs on every pull request and on every push to `main` and `develop`.

## Rules for changes

1. For larger changes, open an issue first, so we can agree on the approach.
2. Fork the repository and create a branch from `develop`.
3. Add or update tests for every change.
4. Update `strings.json` and both files in `translations/` together; `strings.json` and
   `translations/en.json` are identical. Home Assistant reads only `translations/` for a custom
   integration. `strings.json` is the source file in the layout of Home Assistant core; the tests
   in `tests/test_metadata.py` check actions and entities against it and keep it equal to
   `en.json`.
5. Update the user documentation in `docs/` and `CHANGELOG.md` when users notice the change.
6. Open a pull request against `develop` and describe what changes for users.

### Commit messages

- Subject in English, in the imperative mood ("Add …", "Fix …"), at most 72 characters.
- No internal ticket or review ids, no local paths or addresses.
- Do not describe how to exploit a security problem; say what the change protects.
- Sign off every commit (`git commit -s`).

### Dependencies and assets

- The integration has no runtime requirements. A new requirement in `manifest.json` needs an
  OSI-approved license and an entry in `NOTICE` (`scripts/ci/check-licenses.py` checks this).
- Do not add Home Assistant logos or logos of other companies. The integration uses its own images
  in `custom_components/halumar/brand/`.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Trademarks

The license does not cover the name and logo "Halumar". See [TRADEMARKS.md](TRADEMARKS.md).

## Security

Please do not report security problems in public issues. See [SECURITY.md](SECURITY.md).
