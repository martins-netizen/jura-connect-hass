# SonarQube analysis notes

## Analysis scope

The fork uses SonarQube Cloud automatic analysis. `.sonarcloud.properties`
declares Python 3.13 and separates the integration and browser card from
`tests/`. Tests remain in the analysis; no rules or findings are suppressed.

## Browser-card test evaluation

`tests/test_brew_card.mjs` reads `www/jura-brew-card.js` using a fixed URL
relative to the test file and evaluates that repository-owned source with
`vm.runInNewContext`. The context provides minimal browser stubs for testing
the unbundled browser script. Neither the source path nor the source text
comes from Home Assistant states, configuration, request data, or a network
response.

This use does not establish a security boundary. Node explicitly documents
that `node:vm` is not a security mechanism and must not run untrusted code.
Changes to the card and tests need the same code review and CI trust controls
as any other executable repository code.

Suggested finding disposition: reviewed as safe for this test-only use. This
document records the code review rationale; it does not change the finding's
status in SonarQube Cloud.

## Python dependency lockfile

Builds and tests run through Nix (`nix develop` and `nix build`) in
`.github/workflows/ci.yml`. The committed `flake.lock` pins both `nixpkgs` and
the `jura-connect` source by revision and content hash. `flake.nix` constructs
the Python environments from those inputs.

The missing `uv.lock` / `poetry.lock` / `pdm.lock` / `pylock.toml` finding
therefore does not describe the repository's Nix build and test workflow.
Suggested disposition: accepted for that workflow, with `flake.lock` as the
dependency lock mechanism. Adding an unused second package-manager lockfile
would not improve the build.

This assessment is limited to Nix. A separate `pip install` resolves the
version ranges in `pyproject.toml`; Home Assistant installs the requirements
from `manifest.json`. Neither path consumes `flake.lock`, and neither is
claimed to reproduce the Nix environment. A future supported pip/uv workflow
should define and test its own dependency locking policy.

## Browser-card ordering

Auto-discovery compares entity identifiers explicitly using their existing
JavaScript code-unit order. These are machine identifiers rather than
translated display labels. Locale-sensitive sorting could silently select a
different default machine when a browser's language changes. A regression
test covers identifiers containing digits and underscores and opposite entity
registry insertion orders.

## References

- [Node VM documentation](https://nodejs.org/api/vm.html)
- [Nix lockfile documentation](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-flake-lock)
- [SonarQube Cloud automatic analysis](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/automatic-analysis)
