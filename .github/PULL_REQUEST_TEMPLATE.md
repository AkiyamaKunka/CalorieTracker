## What & why

<!-- What does this change, and what problem does it solve? -->

## Parity impact

<!-- This repo keeps a Python server and a Dart app behaviorally identical.
     Delete the lines that don't apply. -->

- [ ] No behavior change (docs, refactor, tooling)
- [ ] Behavior changed on **both** sides; `shared/` updated and bindings regenerated (`python3 scripts/sync_shared.py`)
- [ ] Deliberate one-sided divergence — added a row to `docs/APP_PORT_SPEC.md` §9 and a pinning test

## Gates

- [ ] `python3 -m pytest -q` green
- [ ] `cd app && flutter analyze && flutter test` green
- [ ] `python3 scripts/sync_shared.py --check` clean
- [ ] `bash scripts/check_public_safety.sh` passes (no credentials, no personal data)

## Screenshots

<!-- For UI changes: before/after. Otherwise delete this section. -->
