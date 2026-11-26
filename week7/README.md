# Week 7 — API Fundamentals

Student: FAN CHENHAO  
Student ID (last 4 digits): `3161` (`NN = 61`)  
IPv4 Allocation: `10.61.0.0/16`  
IPv6 Allocation: `fd00:3161::/32`

---

## Lab Wrap-up

### 1. What does `docker network inspect apilab` show, and how is it built from Week 6's building blocks?
`docker network inspect apilab` shows a `bridge` network on `172.18.0.0/16` (gateway `172.18.0.1`) with `api` (`172.18.0.2/16`, MAC `aa:df:68:75:ef:16`) and `client` (`172.18.0.3/16`, MAC `72:6c:2d:6e:d0:33`). Each container has its own Linux network namespace, linked via a `veth` pair into the Linux bridge Docker created for `apilab`. Docker also attaches a local DNS resolver to user-defined bridges, so `client` reaches `api` by hostname without knowing `172.18.0.2`.

### 2. For each status code in Step 2, what should a client do next — and which of them should it never retry?
Only `429` should be retried as-is. Wait the `Retry-After` interval (1 second here — in the 10-GET burst, requests 1–5 returned `200` and requests 6–10 hit `429`), then resend. All six other codes in `lab7/status.txt` come from a broken request or missing permission, so retrying the same call will fail forever:

- `404` (`GET /api/v1/vlans/77`): no such VLAN. Check the ID or URL.
- `405` (`POST /api/v1/vlans/10`): wrong method for a single VLAN; the `Allow` header lists `GET, PUT, PATCH, DELETE`.
- `400` (`POST /api/v1/vlans` with `"name": guest`): malformed JSON. Fix the body formatting before sending again.
- `422` (`PUT /api/v1/vlans/5000`): valid JSON, invalid VLAN number (must be 1–4094). Fix the input value.
- `409` (`POST /api/v1/vlans` with id `10`): VLAN 10 already exists. Read it or switch to `PUT /api/v1/vlans/10` if replacing it is what you meant.
- `403` (`PUT /api/v1/vlans/30` with the `viewer` token): the server authenticated `viewer`, saw a read-only role, and refused the write. Switch credentials rather than retrying.

### 3. Why did `retry_naive.py` create duplicates, and how exactly does your fix prevent them? Why must the `Idempotency-Key` be chosen once, before the first attempt?
`retry_naive.py` POSTs to `/api/v1/acl/entries` without an `Idempotency-Key`. When `lose_responses: 0.5` drops the socket right after the server saves ACL entry 5, `requests.post` raises `ConnectionError`, the script assumes the write never landed, and it sends the POST a second time — creating entry 6.

`retry_safe.py` generates one UUID key before the retry loop (`IDEM_KEY = f"acl-guests-no-ssh-{uuid.uuid4()}"`) and keeps it in `HEADERS`. In `lab7/audit-step4.txt`, attempt 1 dropped after writing entry 8, and attempt 2 resent the exact same request with that key. The server found the key and body hash in `state.idem` and returned the cached `201 Created` (`Idempotent-Replayed: true`), leaving `writes: 1`. Generating a new key per attempt would defeat deduplication because each retry would look like a fresh write; reusing a fixed key across separate script executions would instead return an old cached response (or `422` if the body changed).

### 4. How many requests did `inventory.py` send to read your devices, and why more than the number of pages?
There are 261 devices (`200 + 61`), and `inventory.py` requests `limit=10`, so pagination needs at least 27 requests (26 pages of 10 devices plus 1 page with the final device).

With `errors_503: 0.2` in Step 5, `inventory.py` sent 34 requests (7 extra calls from `503` retries and `429` waits when the 5 req/s bucket emptied). Under `--token-ttl 5 --rate 2` in Step 6, `get_page()` made 42 requests — not counting the three `POST /api/v1/auth/token` logins (initial login plus two re-logins after 5-second expiries). At 2 requests/s, reading 27 pages takes well over 10 seconds, so the client repeatedly hit both `429` rate limits and `401` expired-token errors and had to resend those failed page fetches.

### 5. What did you find in the OpenAPI document that you'd never have guessed?
Checking `/openapi.json` showed a few details not covered in the lecture notes: `GET /api/v1/devices` caps `limit` at `50` (minimum `1`, default `20`), `/api/v1/acl/entries/{id}` only supports `GET` and `DELETE` (no `PUT` or `PATCH`), and reusing an `Idempotency-Key` with a modified body fails with `422` instead of overwriting. The devices endpoint also takes a `site` query parameter (`kl-hq`, `pj-dc`, `penang`, `johor`). Calling `GET /api/v1/devices?site=kl-hq&limit=5` with `curl` returned `X-Total-Count: 66` and `sw-kl-hq-001` (`10.61.200.1`).
