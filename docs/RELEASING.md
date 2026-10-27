# Releasing

The add-on and the integration share one version. Two things ship it, and
they don't wait for each other:

- **The add-on ships when the version bump merges.** The Supervisor reads
  `relay/config.yaml` from `main` and offers the update as soon as the
  version there changes, with or without a GitHub release. The add-on
  update dialog shows `relay/CHANGELOG.md` from `main` too.
- **The integration ships when the tag is pushed.** HACS offers GitHub
  releases, which `release.yml` creates from the tag.

So the bump PR is the release as far as add-on users are concerned. It must
be releasable before it merges, and the tag follows straight after.

## Order

1. **Open the bump PR.** One PR changes, together:
   - `version:` in `relay/config.yaml`;
   - `"version"` in `custom_components/icomm_local/manifest.json`;
   - `CHANGELOG.md`: the `## [Unreleased]` notes become
     `## [X.Y.Z] - YYYY-MM-DD`, with at least one bullet;
   - `relay/CHANGELOG.md`: a copy (`cp CHANGELOG.md relay/`).
2. **Every check on the PR is green.** Not just the required ones: tests
   (relay on every Python, the integration in the latest and the oldest
   supported Home Assistant), the add-on image build with its in-image tests
   (both architectures), hassfest, HACS, security. The `version-bump` job in
   `tests.yml` runs `scripts/release_check.py versions vX.Y.Z` whenever the
   PR changes the add-on version: tag, add-on, integration and changelog
   must agree.
3. **Merge.** From here add-on users can update.
4. **Tag the merge commit right away:**

   ```bash
   git switch main && git pull
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

5. **`release.yml` publishes.** It checks that the tagged commit is on
   `main`, that the versions and changelog agree, reruns `tests.yml` and
   `build.yml` at the tag, and only then creates the GitHub release (the only
   step with write access) with that version's changelog section as notes.
   If it fails, fix forward with a new patch version: the add-on is already
   out.

Check locally before opening the PR:

```bash
python scripts/release_check.py versions vX.Y.Z
python scripts/release_check.py mirror
```
