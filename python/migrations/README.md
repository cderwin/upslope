# Storage migrations

One-off scripts that convert the forecasts already exported to the public bucket
when the export format changes, so the webapp never has to read two formats.
They're run by hand, from a laptop (they aren't in the Docker image), never
by the exporter, so they can't slow down or stall an export check.

```sh
just py-migrate m003_<what> --dry-run   # what would change
just py-migrate m003_<what>             # asks before writing
```

`just py-migrate` loads `.env.local`, so a migration works on whichever bucket
`PUBLIC_BUCKET_NAME` names there. Point it at MinIO (`just up`) to rehearse.

## Sharing the bucket with the exporter

The exporter keeps running while a migration does, and rewrites the manifests
of the runs it follows: each model's latest complete run and newer ones. It
never writes older runs again, only deletes them past retention. So:

- By default a migration changes only runs older than the model's latest
  complete run. The runs it skips are counted in its report; run it again a
  few hours later, once the exporter has moved on, to finish them.
- `--include-followed` migrates the followed runs too. Only use it with the
  exporter stopped (`fly scale count exporter=0`, and `=1` afterwards).
- A run's manifest is rewritten only if it's unchanged since the migration
  read it (`If-Match` on its ETag). A run the exporter rewrote or pruned
  meanwhile is left alone and counted as changed; rerun to finish it.
- New files are uploaded before the manifest that lists them, and old ones
  deleted only after it's written, so a listed file is never missing.

Every migration skips what it has already done, so it's safe to rerun.

## Migrations

None pending. Past ones (in git history):

| Migration | Shipped with | What it did |
| --- | --- | --- |
| `m001_webp_frames` | Lossless WebP frames | Converted every PNG frame to a lossless WebP with the same pixels |
| `m002_int16_values` | int16 values on the model's grid | Replaced the PNG value images with int16 GeoTIFFs, recomputed from dynamical.org |

A new migration is a module here (`m003_<what>.py`) with a click command,
built on `common.py`: `kept_runs` lists the runs, `migrate_runs` applies a step
to each (skipping followed runs unless `--include-followed`), and
`rewrite_manifest` writes a migrated manifest only if it's unchanged. Add its
tests to `tests/`, and delete it once it has run against production.
