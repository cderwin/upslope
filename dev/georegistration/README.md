# Georegistration snapshot (local development and tests)

A copy of the georegistration annotations, for tests and the local Docker stack. Production
reads and edits annotations in S3 through the admin API (`/admin/georegistration`); this
directory is never deployed.

- `document.json`: a published annotation document (landmarks, and pixel annotations per
  maptype and image).
- `images/<maptype>/<image_id>.gif`: the images it annotates.

The local stack (`just up`) uploads this snapshot into MinIO on first start. To load it into
any bucket configured in your environment:

```sh
uv run --project python upslope georegistration upload dev/georegistration
```

To refresh it from what is currently published:

```sh
uv run --project python upslope georegistration snapshot dev/georegistration
```
