# Remote production assets

This directory is the fail-closed bridge between the public source tree and
private production bytes. It lets a clean authorized runner reproduce the local
checkout without publishing raw photographs, Vision outputs, or licensed inputs.

Remote availability is not publication clearance. A valid lock proves identity
and availability only; it does not approve a final cut, assert rights, authorize
an upload, satisfy independent physical custody, or file a submission.

## Exact production profile

`scripts/assets.py` recognizes a closed `screendance-production` profile of 487
required inputs:

- 162 source photographs under `pipeline/.work/raw/`;
- 162 Vision masks under `pipeline/.work/vision/mask/`;
- 162 Vision pose records under `pipeline/.work/vision/pose/`; and
- `.work/music/MuseScore_General.sf3`.

The frame set is derived from `corpus/manifest.json`. The profile rejects missing
or extra targets, a non-private photograph or Vision output, a non-restricted
soundfont, a wrong `IMG_1594.JPG` digest, or a soundfont digest that disagrees
with the audio toolchain. Film plates, rendered audio, review media,
deliverables, and packages are outputs and never enter this input lock.

No `lock.v1.json` is committed yet. GitHub does not contain the 162 originals or
their 324 current Vision outputs, so inventing their hashes would create false
parity. The inventory command writes the first lock only after all 487 actual
files are present and re-hashed.

## One-time bootstrap

1. Create a dedicated **private** GitHub asset/control repository. Put the 487
   files in a `payload/` tree using the repository-relative paths above; use Git
   LFS for large bytes. Do not put raw material in this public repository.
2. On the authorized Mac, generate the public lock from that exact private
   payload tree. `EXACT_PUBLIC_SOURCE_COMMIT` is the code commit the production
   run will check out, not a moving branch:

   ```bash
   python3 scripts/assets.py inventory \
     --source-root /path/to/private-asset-repo/payload \
     --output assets/lock.v1.json \
     --lock-id screendance-production-YYYYMMDD \
     --profile screendance-production \
     --repository-commit EXACT_PUBLIC_SOURCE_COMMIT
   ```

3. Commit and push the private payload, then commit the generated lock on a
   public topic branch. Configure a protected `danse-production` environment
   whose workflow can read only the private asset repository.
4. Restore GitHub Actions admission. Use GitHub-hosted macOS for public,
   secret-free Metal checks. Never attach a privileged self-hosted runner to
   this public repository.

This is the only bootstrap that needs the original local material. After it,
normal production can begin from clean GitHub checkouts.

## Normal remote operation

Check out the public repository at the lock's exact `repository_commit`, and the
private asset repository at its reviewed immutable commit. Hydrate from the
private `payload/` tree:

```bash
python3 scripts/assets.py pull \
  --lock /trusted/lock.v1.json \
  --root /path/to/exact/public/checkout \
  --allow-file \
  --file-source-root /path/to/exact/private/checkout/payload \
  --receipt /private/receipts/hydration.json

python3 scripts/assets.py verify \
  --lock /trusted/lock.v1.json \
  --root /path/to/exact/public/checkout \
  --receipt /private/receipts/verification.json
```

Each lock row binds an ID, ignored repository-relative target, byte count,
SHA-256, media type, rights class, required flag, and one or more sources.
Sources may be an explicitly enabled private file checkout, HTTPS object, or
named GitHub Release asset. The digest and byte count remain authoritative even
if a locator is unavailable or replaced.

The production profile forbids direct HTTPS locators for private photographic
and Vision inputs; the public lock therefore cannot contain presigned URLs or
query credentials. A named GitHub Release source for one of those private inputs
must name an explicit token environment, and hydration fails closed when that
credential is unavailable or malformed; unauthenticated public-release fallback
is not accepted. Its restricted soundfont may use only the canonical upstream
URL recorded by `music/audio-toolchain.json`, a file checkout, or a named Release
asset. The checkout must be at the exact Git top level, exact commit, and clean
apart from locked targets and the content cache.

Pulls use a content-addressed `.asset-cache/`, verify bytes before publication,
make targets read-only, and never overwrite a mismatching target. The CLI rejects
duplicate JSON keys, path traversal, symlinks, unsafe URLs, a wrong public Git
commit, missing required assets, and digest or byte-count drift. Receipts contain
only aggregate identities and unresolved opaque IDs; storage URLs, tokens,
filenames, and local paths are redacted.
Receipt paths are immutable: the CLI refuses to overwrite an existing receipt.
Use a fresh run-specific path for each audit, pull, or verification.

Run the portable adversarial suite with:

```bash
python3 scripts/tests/assets.test.py
```

## Runner and output boundary

The private production workflow belongs in the private asset/control repository,
not here. It is manual, exact-commit pinned, protected by an environment, and
split into an unprivileged renderer and a clean publisher. Actions artifacts are
transit only; durable outputs go to an immutable private Release, package, or
object store.

The current 4K ProRes master is larger than GitHub-hosted macOS runner storage.
Render restartable bounded segments to private remote storage and assemble them
in a separate job, or use an ephemeral Mac runner attached only to the private
control repository with adequate disk.
