# Releasing UniversalSubs (developer notes)

## One-time setup
1. Create accounts on https://test.pypi.org and https://pypi.org
2. `python -m pip install build twine`
3. In pyproject.toml, set your real GitHub URL and author name

## Every release
1. Bump the version in ONE place: `src/universalsubs/__init__.py`
   (pyproject reads it automatically; the app title bar shows it)
2. Commit + tag:  `git tag v0.9.2`
3. Build:         `python -m build`        -> dist/*.whl and *.tar.gz
4. Dry run:       `python -m twine upload --repository testpypi dist/*`
   then verify:   `pip install -i https://test.pypi.org/simple/ universalsubs`
5. Real release:  `python -m twine upload dist/*`

Users then install/update with:
    pipx install universalsubs        (first time)
    pipx upgrade universalsubs        (updates)
or plain pip if they prefer.

## What PyPI does NOT carry
- proctap (per-app capture, Win11): not on PyPI — users install the bundled
  wheel from the GitHub release, or the app runs without it (system audio).
- The .bat launchers: pip users launch with the `universalsubs` command.

## Version scheme
MAJOR.MINOR.PATCH — bump PATCH for fixes, MINOR for features,
MAJOR at 1.0.0 when you call it stable.
