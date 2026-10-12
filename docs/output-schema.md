# Output schema

Fixed column order (stable for downstream imports; missing Clay columns stay empty):

```
client, list_name, audience_id, tier, priority,
first_name_clean, first_name, last_name, full_name, job_title,
linkedin_url, person_location,
company_name, company_name_normalized, normalized_by, company_domain,
company_linkedin_url, company_size, company_industry,
also_in_lists, shard, clay_table_id, query_hash, built_at
```

Notes:

- `first_name_clean` strips titles/credentials/emojis/brackets; `first_name` keeps Clay's raw
  value.
- `linkedin_url` is canonical `https://www.linkedin.com/in/<slug>`.
- `company_domain` is lowercased, protocol/`www.`/paths/ports/trailing dots removed.
- `company_name_normalized` uses Clay's value when present, otherwise local suffix stripping
  (Inc/LLC/Ltd/GmbH/…); `normalized_by` is `clay` or `local`.
- `also_in_lists` records the other lists a deduped person appeared in.
- `shard` is `<n>/<total>`; `clay_table_id` and `query_hash` trace rows back to their table.
- Files: `outputs/<audience_id>__<slug>.csv` per audience and `<client>__merged.csv`.
