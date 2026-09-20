# Snapshot Schema V2 Contract

```json
{"schema_version":2,"server_id":7,"clients":{"uuid:a":{"id":"a"}},"inbounds":[{"id":101,"server_id":7,"client_refs":["uuid:a"],"client_overrides":{}}]}
```

- Legacy list blocks remain semantically unchanged.
- Schema v2 stays normalized in memory.
- Unknown versions are rejected per block and last good state remains.
- External full/delta payloads keep the current expanded inbound shape.
- Entity changes identify affected inbounds through memberships without a fleet scan.
- Existing JSON-compatible compression is used; no pickle.
