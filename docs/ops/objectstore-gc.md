# ObjectStore GC

`scripts/gc_objectstore.py` removes old generated local ObjectStore objects under:

- `generated-video`
- `generated-audio`
- `subtitles`
- `covers`

`seed-media` is intentionally skipped because seed assets use content-addressed keys.

Dry-run first:

```bash
python scripts/gc_objectstore.py --max-age-hours 24
```

Apply deletion:

```bash
python scripts/gc_objectstore.py --max-age-hours 24 --apply
```

The script reads `CUTAGENT_LOCAL_OBJECTSTORE_PATH` by default. Use `--root` for a specific local
root or S3 cache directory, for example:

```bash
python scripts/gc_objectstore.py --root .data/objectstore-cache --max-age-hours 24 --apply
```

For development machines, run it from cron or systemd timer every few hours with a 24-hour
retention window. Keep DB-referenced orphan cleanup as a separate follow-up because it requires
repository access.
