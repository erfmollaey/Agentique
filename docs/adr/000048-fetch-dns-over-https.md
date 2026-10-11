# AD-048 — Fetch DNS resolution moves to DNS-over-HTTPS, opt-in

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-10 |
| **Supersedes** | nothing (adds a mode; does not change AD-031) |
| **Touches** | `app/infrastructure/fetch/resolver.py`, `app/core/config.py` |
| **Does not touch** | `app/infrastructure/fetch/ssrf.py` |

## Context

The research pipeline could not fetch anything. Every fetch — real, valid,
public domains — failed with `FetchBlockedDestinationError`, and runs therefore
died at synthesis with `SynthesisInputError` and no verified evidence.

The SSRF boundary was not broken. It was working, and it was reporting a true
fact about the world it could see.

Measured inside the worker container:

```
$ docker compose exec worker python -c \
    "import socket; print(socket.getaddrinfo('github.com', 443))"
[((AddressFamily.AF_INET, ...), ('198.18.0.22', 443), ...)]

$ # a name that does not exist, also inside the container
198.18.0.34
```

`198.18.0.0/15` is RFC 2544 benchmarking space: not globally routable, not
assignable to a host, and correctly refused by `classify_address`
(`app/infrastructure/fetch/ssrf.py:221`) via the explicit entry in
`BLOCKED_IPV4_NETWORKS` at `ssrf.py:135`.

The cause is a fake-IP DNS layer on the host — Tailscale MagicDNS is the
common case, and comparable DNS-filtering products behave the same way. It
rewrites *every* outbound UDP/53 answer into `198.18.0.0/15`, which is why a
nonexistent domain resolves too.

Three observations pinned it to the network path rather than to resolver
configuration:

1. **A raw query to a public resolver still came back fake.**
   `docker run --dns=1.1.1.1` put `nameserver 1.1.1.1` in the container's
   `/etc/resolv.conf`, and a hand-built UDP query to `1.1.1.1:53` returned
   `0xc6 0x12 0x00 0x16` — `198.18.0.22`.
2. **A nonexistent domain resolved**, to `198.18.0.34`. A rewrite, not an answer.
3. **HTTPS was not rewritten.** `https://example.com` from the same container
   returned 200. The interception is specific to port 53.

The consequence is the important part: the symptom is indistinguishable from
"the internet is down", for a reason that is entirely local to one developer's
network configuration.

## Decision

`app/infrastructure/fetch/resolver.py` owns **where an address answer comes
from**, and offers two implementations of the `AddressResolver` port that
`SsrfPolicy` already accepted:

| `FETCH_DNS_MODE` | Resolver | Answers from |
|---|---|---|
| `system` (default) | `SystemAddressResolver` | the platform's `getaddrinfo` |
| `doh` | `DoHAddressResolver` | DNS-over-HTTPS endpoints, RFC 8484 |

`system` is the default. DoH is opt-in, and an unconfigured deployment behaves
exactly as it did before this decision.

`ssrf.py` is unchanged. The single choice configuration makes is which resolver
runs, never which addresses are permitted: the scheme allowlist, the blocklists,
the "every answer must pass" rule, and the connection pinning that closes the
DNS-rebinding window all execute identically in both modes.

## Why DoH, and why not the alternatives

**Not: allowlist `198.18.0.0/15` in the address policy.** It is blocklisted
because it is not a routable destination, which is true regardless of why a
particular container resolves there. Removing the entry to make a symptom
disappear would also stop defending against a real attack. And it is not even a
local fix: those addresses are reachable *only* because a transparent proxy on
this host tunnels them, so any other environment fails differently.

**Not: set the container's DNS servers.** Tested and disproved above — the
rewrite is on the network path, so every resolver the container is pointed at
is intercepted identically. This is the option most likely to be reached for
first and the one least likely to work.

**Not: a second resolver library with hardcoded nameservers.** Same
interception, since it also speaks UDP/53, plus an added dependency and a
hardcoded list inside application code.

**DoH works** because HTTPS (TCP/443) is not rewritten on those networks, so the
answers it returns are real and pass the address policy on their own merits —
`cloudflare-dns.com` → `140.82.121.3`, `dns.google` → `140.82.121.4`, both
matching the host's own resolution. The policy is not relaxed; it is simply
given true answers to judge.

## Consequences

**The security boundary is unchanged.** That is the whole point, and it is
asserted in the suite: a DoH answer for a private, loopback, or fake-IP host is
refused exactly as a system answer would be. Three implementation mutants were
verified caught by the tests, including one that made DoH skip address
validation entirely. **A resolver that cannot answer fails closed** — it is a
typed rejection, never an implicit "no address found, so allow".

**`FETCH_DNS_SERVERS` is an ordered list**, tried in order, with at least two
independent operators by default, because one endpoint is a single point of
failure for the entire fetch stage. Fallback happens only when an endpoint could
not be reached or understood — a transport failure, a non-200, or a malformed
answer. **An endpoint that answers is used as it answered, including "no
records"**: a legitimate NXDOMAIN must not be masked by asking a different
provider, because then which provider answered is not something the deployment
can reason about. Verified live, with a dead primary and a working secondary.

**A deployment with a broken local resolver now works**, including one whose
symptom was previously invisible.

## Limits — stated plainly

**This is not a network-layer fix and does not claim to be.** It works around
one specific failure mode that a local device can introduce. If a future
environment also rewrites HTTPS — an intercepting TLS proxy, a corporate
MITM middlebox with a pinned root — DoH will not help, because the rewrite moves
to the same channel DoH relies on. Do not "fix" that by loosening the SSRF
policy: the correct fix is on the host, in the DNS interception itself. This ADR
records that escalation deliberately, because the tempting unblock at that point
is exactly the one that must not be taken.

Additional limits:

- **The public endpoints are a default, not a recommendation.** They are
  unauthenticated, rate-limited, and see the hostnames looked up. A deployment
  that cares should point `FETCH_DNS_SERVERS` at a resolver it trusts or runs.
- **No live `/research` run has completed end-to-end.** Search, fetch,
  extraction, chunking, and evidence all executed successfully against the real
  endpoints with no `FetchBlockedDestinationError`, and synthesis was verified
  separately against real fetched sources. The run that reached it was stopped
  by an unrelated Groq `429` and by Celery's 120 s hard limit, both outside this
  decision's scope. Terminal delivery to a real chat is therefore unverified.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `FETCH_DNS_MODE` | `system` | `system` = platform `getaddrinfo`; `doh` = DNS-over-HTTPS. |
| `FETCH_DNS_SERVERS` | `https://cloudflare-dns.com/dns-query,https://dns.google/dns-query` | Ordered, comma-separated RFC 8484 endpoints. Used only in `doh` mode. |
| `FETCH_DOH_TIMEOUT_SECONDS` | `5.0` | Bound on one attempt. Separate from the fetch timeout on purpose: a resolver that hangs must not consume a fetch's budget. |

An unrecognised mode raises at construction rather than silently falling back,
so a deployment that asked for one resolution path and got another is a loud
failure rather than a silent one.
