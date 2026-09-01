# Private custody snapshots

`scripts/private_custody.py` creates a portable source bundle plus a byte-exact
archive of every ignored or untracked material file in one clean Git worktree.
It then copies the same immutable snapshot to a second physical device. It does
not remove, move, or rewrite the source.

The snapshot directory is private. Its manifest contains relative filenames and
must stay with the controlled custody media; it must never be committed or used
as a public receipt. The restore command emits a separate redacted receipt with
only hashed snapshot/medium identities, source and remote commits, aggregate
counts and bytes, and artifact digests. Caller labels, snapshot names, branch
names, filenames, and local paths are not serialized into that receipt.

The private GitHub asset plane described in [`assets/README.md`](../assets/README.md)
is an operational availability copy of the same byte identities. It lets a clean
authorized runner hydrate production inputs, but it is not a second independent
physical device and does not satisfy this custody contract's restore rehearsal or
human-acceptance predicates.

## Snapshot and duplicate

Fetch the relevant remote first, then use two existing directories on genuinely
independent physical media:

```bash
python3 scripts/private_custody.py snapshot \
  --source SOURCE_WORKTREE \
  --primary-root PRIMARY_CUSTODY_ROOT \
  --secondary-root SECONDARY_CUSTODY_ROOT \
  --snapshot-id PORTABLE_SNAPSHOT_ID \
  --remote-ref origin/BRANCH \
  --remote-mode equal
```

Use `--remote-mode ancestor` only for a deliberately retained historical commit
that is proven reachable from the named remote branch. `equal` is the default
custody expectation for an archive branch. The tool refuses a dirty tracked
tree, unsafe or unresolved full remote-tracking reference, fetch/push remote mismatch,
escaping symlink on POSIX or Windows, special file, hidden index flags,
destination collision, insufficient space, or two destinations on the same
physical device. Fetch/push parity compares the complete URL sets, so an extra
push destination fails even when the first push URL matches. Every private
inventory pass closes with a second whole-census metadata and digest proof, and
that complete proof runs again after the snapshot artifacts are hashed. A writer
that changes an earlier file while a later file is being read therefore
invalidates the snapshot. The sealed control also records the immutable byte
count of every tracked blob in the admitted commit. Before creating staging, the
space preflight budgets both private material and the uncompressed bytes of every
Git object reachable from that commit, plus a five-percent-or-1-GiB reserve. On
macOS, independence is derived from one APFS physical store and its physical
whole disk; virtual, image-backed, ambiguous, and same-device volumes fail
closed. The tracked tool currently refuses to claim physical independence on
platforms where that proof cannot be derived with macOS `diskutil`.

An interrupted, corrupt, or failed snapshot remains under its hidden
`.incomplete` directory for inspection. The tool never deletes or resumes it and
will not overwrite it on a later invocation. Before publication, every staged
file is flushed through `F_FULLFSYNC` on macOS (`fsync` elsewhere), the staging
directory and its parent are `fsync`ed, and the complete staged snapshot is
verified against its admitted control. Publication uses a kernel-level exclusive
rename (`RENAME_EXCL` on macOS), so even an empty destination created after the
preflight is preserved rather than replaced. The published directory and its
parent are `fsync`ed again after the rename.

## Restore rehearsal

Restore from the second copy into a new directory on a clean target. The receipt
parent must already exist, and neither the restore target nor receipt may exist:

```bash
python3 scripts/private_custody.py restore \
  --source SOURCE_WORKTREE \
  --primary PRIMARY_CUSTODY_ROOT/PORTABLE_SNAPSHOT_ID \
  --secondary SECONDARY_CUSTODY_ROOT/PORTABLE_SNAPSHOT_ID \
  --primary-id OPAQUE_PRIMARY_MEDIUM_ID \
  --secondary-id OPAQUE_SECONDARY_MEDIUM_ID \
  --target NEW_EMPTY_RESTORE_PATH \
  --receipt EXISTING_PRIVATE_RECEIPT_PARENT/redacted-receipt.json
```

Before restoring, the command re-audits the retained source, including hidden
index flags and the full private census, against the sealed snapshot. It then
starts from the bundled Git commit, overlays only the private inventory, rejects
archive traversal and overwrite attempts, hashes every restored file, checks the
ignored and untracked censuses separately against every recorded classification,
and requires a clean tracked diff. A path ignored only by source-local or global
Git configuration therefore blocks the rehearsal if that classification is not
reproduced in the clean target. Both custody copies are re-hashed before
extraction. Source, snapshots, restore target, and receipt must be pairwise
disjoint so a successful rehearsal cannot mutate the evidence it just certified.
Before creating the restore target, the tool requires free space for the sealed
tracked checkout, private inventory, source bundle, and a five-percent-or-1-GiB
reserve; an unreadable capacity boundary fails closed. After the receipt file
itself is flushed, its parent directory is `fsync`ed before success is reported.

The generated receipt intentionally leaves `human_acceptance.ok` false and
`cleanup_authorized` false. A successful machine restore does not authorize
worktree reclamation. Issue #21 remains open until the redacted receipt is
reviewed, durably tracked, and explicitly accepted by the owner; issue #3 must
also finish the archived experiment dispositions before its retained worktree
can be reclaimed.
