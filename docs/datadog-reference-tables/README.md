# Datadog Reference Tables

## sterling_hollis_topology_edges

Import `sterling_hollis_topology_edges.csv` as a Datadog Reference Table named
`sterling_hollis_topology_edges`.

Suggested primary key: `source`.

This table keeps the existing edge-table shape:

```csv
source,depends_on
```

Updated dependency path:

- `sterling-hollis-be` depends on `gmtek5000`.
- `gmtek5000` depends on `store-fulfillment-edge01`.
- `store-fulfillment-edge01` depends on `datacenter-user-sw11a`.
- `datacenter-user-sw11a` is the root node.

Use this table with the demo network log fields `device_hostname`,
`topology_parent_device`, `dependency_path`, and
`correlation_key=sterling-hollis-network-outage`.
