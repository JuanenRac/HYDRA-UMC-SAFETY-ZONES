# Tool arms

A tool of the URTC family is **armed** when its power stage may act: a soldering iron that heats, a spindle that may turn. The service that drives the tool reports the arm state of one tool of one robot at a time. `hydra-umc-safety-zones` keeps the reports and lists them, so a panel can show them next to the verdict of `GET /state`.

It is a record and nothing more. The service never arms or disarms a tool and never requests a stop because of a report: the stop stays the job of the zones and of the firmware.

## Switch

Off by default. `--tool-arms` (or `SAFETY_ZONES_TOOL_ARMS=1`) turns it on; until then both routes answer `404`.

Both routes need a credential: the bearer token of the service or the identity of HYDRA-UMC-SERVER. With neither configured, `POST /tool-arms` answers `403` (there is nobody to ask) and `GET /tool-arms` stays open to the machine only if the service itself is.

## `POST /tool-arms`

```json
{"robot": "arm-3", "toolId": 4, "armed": true}
```

| Field | Rule |
| --- | --- |
| `robot` | A robot id of letters, digits and `. _ : -`, at most 64 characters, starting with a letter or digit. |
| `toolId` | A whole number from 0 to 24: the `tool_id` of the tool list of the URTC family (`urtc_tools.json` of the SDK). 25 is the invalid value and is refused. |
| `armed` | `true` or `false`; nothing else. |

Answer `200`: `{"robot": "arm-3", "toolId": 4, "armed": true, "seq": 1}`. `400` for a report that does not follow the rules, `429` when 256 pairs of robot and tool are already kept (a known pair can always be updated).

## `GET /tool-arms`

```json
{
  "schema_version": "1.0",
  "ttlSeconds": 30.0,
  "arms": [{"robot": "arm-3", "toolId": 4, "armed": true, "by": "token", "seq": 1, "ageSeconds": 2.1, "stale": false}],
  "armedCount": 1,
  "staleCount": 0
}
```

* `arms` has the last report of each pair, ordered by robot and tool.
* A report older than `ttlSeconds` is `stale`. A stale report keeps the value it had, but it is **not confirmed**: `armedCount` counts only the armed tools whose report is fresh, and `staleCount` counts the stale ones. A panel must show a stale `armed: true` as a warning, never as safe, and a stale `armed: false` as unknown.
* The reports live in memory: a restart forgets them, and a panel sees that as an empty list until the tools report again.
