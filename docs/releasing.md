# Releasing to PyPI

Goal: `pip install modelspend` works for anyone. The name `modelspend` was free on PyPI when checked (2026-10-17).

## One-time setup (about 10 minutes)

1. Create an account on <https://pypi.org> and turn on two-factor authentication.
2. Add a **pending trusted publisher** for the new project: PyPI > Your account > Publishing > "Add a new pending publisher".
   - PyPI project name: `modelspend`
   - Owner: `rmencke2`, repository: `modelspend`
   - Workflow name: `publish.yml`, environment name: `pypi`
3. On GitHub: Settings > Environments > New environment named `pypi` (you can require your own approval there).
4. On GitHub: Settings > Secrets and variables > Actions > Variables > new variable `PUBLISH_TO_PYPI` = `true`.

No PyPI token is created or stored anywhere: PyPI trusts the workflow itself (OIDC trusted publishing).

## Every release

Publish a GitHub release as usual (tag `vX.Y.Z...`, pre-release ticked for betas). The `publish` workflow builds the wheel and sdist from that tag, runs `twine check`, and uploads them. The version comes from `pyproject.toml`, so it must match the tag.

CI also builds the wheel on every push and installs it into a clean environment (`package` job), so a missing data file is caught before a release, not after.

## Betas

Versions like `0.7.0b1` are pre-releases. If only pre-releases exist, `pip install modelspend` should install the newest one; if pip refuses, `pip install --pre modelspend` always works. Once a stable `1.0.0` exists, plain `pip install` will prefer it.

## Try it first

To rehearse without touching the real index, do the same setup on <https://test.pypi.org> and upload by hand: `python -m build && twine upload --repository testpypi dist/*`, then `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple modelspend`.

## Not verified

The workflow has not run yet (it needs your PyPI setup). The wheel itself was built and installed into a clean environment, and the installed commands were exercised.
