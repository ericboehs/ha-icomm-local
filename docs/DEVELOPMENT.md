# Development

```bash
pip install --require-hashes -r relay/requirements.txt   # the add-on's exact lock
pip install --require-hashes -r requirements-dev.txt    # test and lint tools (locked)
ruff check . && python -m pytest                         # relay + unit tests
pip install -r requirements-ha.txt                       # pinned HA test harness
python -m pytest tests_ha -p no:cacheprovider -o asyncio_mode=auto   # inside real HA
```

Releasing: see [RELEASING.md](RELEASING.md).

Add-on builds are reproducible. The Dockerfile pins the base image by tag and
digest, and `relay/requirements.txt` pins every package with hashes. To change
a dependency, edit `relay/requirements.in` and regenerate the lock with the
command in that file's header. Dependabot bumps the base image and the lock
weekly, each as one grouped PR.
