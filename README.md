# wol-relay
Simple service that allows a client to request a wol packet be sent to a pre-defined host

It runs on a host on the same LAN segment as the machines it wakes and sends
a real broadcast magic packet, so callers on other networks (e.g. Home
Assistant in Docker, across a VLAN) don't have to rely on unicast WoL, which
breaks once the target's ARP entry goes stale. Python standard library only.

## API

| Method | Path            | Auth                    | Result |
|--------|-----------------|-------------------------|--------|
| `GET`  | `/health`       | none                    | `200 {"status": "ok"}` |
| `POST` | `/wake/<target>`| `Authorization: Bearer <token>` | `200 {"status": "sent", "target": "<target>"}` |

`POST /wake/<target>` returns `401` for a missing or wrong token, `404` for a
target that doesn't exist **or** that the requester isn't allowed to wake
(the log records which), and `500` if the send fails. Each wake sends the
packet 3 times, 1 second apart, so the request takes about 2 seconds.

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" http://relay-host:8098/wake/desktop
```

## Configuration

Config is a JSON file, `/etc/wol-relay/config.json` by default (override
with `--config`). See [`config.example.json`](config.example.json).

### `targets`

Each key is a target name used in `/wake/<target>`.

| Field | Required | Description |
|-------|----------|-------------|
| `mac` | yes | `aa:bb:cc:dd:ee:ff`, `aa-bb-…`, `aabb.ccdd.eeff`, or bare hex |
| `broadcast_address` | no | Default `255.255.255.255`. Prefer the subnet broadcast, e.g. `10.30.10.255` |
| `source_address` | no | A local IP on the target's VLAN. Pins which interface the packet leaves from |
| `port` | no | UDP port, default `9` |

On a host with several VLAN interfaces, set `source_address` (or a
subnet-directed `broadcast_address`) for every target. Otherwise a send to
`255.255.255.255` leaves via the default route's interface; the relay logs
a warning at startup for any target configured that way.

### `requesters`

Each key is a requester name (shown in logs). Every requester needs
exactly one token source and a `targets` setting:

| Field | Description |
|-------|-------------|
| `token` | The token itself, inline |
| `token_file` | Path to a file containing the token, e.g. `/run/secrets/ha_token`. Surrounding whitespace is stripped |
| `token_env` | Name of an environment variable containing the token |
| `targets` | `"*"` for all targets, or a non-empty list of target names |

Tokens are read once at startup, must be printable ASCII with no spaces,
and must be unique across requesters. Generate one with:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Listener

| Field | Default |
|-------|---------|
| `bind_host` | `0.0.0.0` |
| `bind_port` | `8098` |

The relay refuses to start on any config error and prints which field is
wrong (never a token value).

## Running with Docker Compose

```sh
cp config.example.json config.json        # edit targets and requesters
cp compose.example.yaml compose.yaml      # adjust secrets/environment
cp .env.example .env                      # set PUID/PGID and env tokens
mkdir -p secrets
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/ha_token
chmod 600 secrets/ha_token
docker compose up -d --build
```

Notes:

- **Host networking is required.** On a bridge network the broadcast never
  leaves the Docker bridge, and `source_address` must be one of the host's
  own IPs.
- **`PUID`/`PGID` must be able to read `config.json` and `secrets/*`.**
  Compose file secrets keep their host ownership and permissions.
- With `bind_host` `0.0.0.0` and host networking, the relay listens on every
  interface on the host. Restrict port 8098 to your requesters with a
  firewall rule.
- The image has a `HEALTHCHECK` that calls `/health` via
  `wol_relay.py --healthcheck` (uses 127.0.0.1 when bound to `0.0.0.0`).
- `config.json`, `compose.yaml`, `.env`, and `secrets/` are git-ignored.

## Running directly

```sh
python3 wol_relay.py --config /path/to/config.json
```

No packages to install; needs Python 3.8+.
