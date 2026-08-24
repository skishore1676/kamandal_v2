# Tastytrade live handoff

## Current boundary

Kamandal has the broker-order primitives needed for the short-strangle lane:
two-leg open and close, mixed close/open adjustment, broker dry-run, submit,
status, cancel, atomic replacement, partial-fill normalization, and
venue-qualified reconciliation. The strategy remains `shadow`; none of these
capabilities grants authority to authenticate or place an order.

Production readiness requires an explicit account number, OAuth material, and
the pinned Orders API version. The documented production API is
`https://api.tastyworks.com`; the documented sandbox API is
`https://api.cert.tastyworks.com`.

## Information Suman must provide

Before an authenticated check, obtain:

1. A rotated production OAuth client id, client secret, and refresh token.
   Rotate the previously exposed values before they are used again.
2. The production Tastytrade account number approved for uncovered options.
3. A separate sandbox OAuth client and sandbox account number.

Do not paste these values into Codex, chat, Git, the Google Sheet, an Obsidian
note, or a shell command. Enter them only through the hidden interactive prompt
on Old Mac.

For production:

```bash
ssh oldmac
cd /Users/sunny/Documents/kamandal_v2
.venv/bin/python scripts/configure_tastytrade_runtime.py --rotate-oauth
```

This atomically updates `/Users/sunny/Documents/kamandal_v2/.env` with owner-only
permissions. It writes the Tastytrade client id, client secret, refresh token,
account number, documented production host, and pinned Orders API version. It
prints key names only, never values.

For sandbox, use separate credentials and a separate file:

```bash
cd /Users/sunny/Documents/kamandal_v2
.venv/bin/python scripts/configure_tastytrade_runtime.py \
  --env-file .env.tastytrade-sandbox --sandbox --rotate-oauth
```

The sandbox file is ignored by Git.

## Verification ladder

The default check is broker-inert. It builds and displays synthetic open,
close, adjustment, and replacement payloads without authenticating or using
the network:

```bash
.venv/bin/python scripts/tastytrade_contract_check.py \
  --env-file .env.tastytrade-sandbox
```

After explicit approval to authenticate, read account state and chain inventory:

```bash
.venv/bin/python scripts/tastytrade_contract_check.py \
  --env-file .env.tastytrade-sandbox --authenticate --underlying QQQ
```

After choosing a currently listed sandbox expiration and strikes, a separately
approved broker dry-run can be sent. It validates buying-power effect but never
submits the order:

```bash
.venv/bin/python scripts/tastytrade_contract_check.py \
  --env-file .env.tastytrade-sandbox --authenticate --dry-run-open \
  --underlying QQQ --expiration YYYY-MM-DD \
  --put-strike PUT_STRIKE --call-strike CALL_STRIKE --credit CREDIT
```

A full sandbox lifecycle needs a real fake-money sandbox position so that open,
partial-fill handling, cancel/replace, adjustment, and close can be observed.
Those are external broker effects and require a new explicit approval even
though no real money is involved.

The final protected gate is a bounded one-contract production canary followed
by the separate Google Sheet stage change from `shadow` to `live`. Neither is
part of deployment.

## Broker references

- [Tastytrade Orders API](https://developer.tastytrade.com/open-api-spec/orders/)
- [Tastytrade sandbox](https://developer.tastytrade.com/sandbox/)
- [Tastytrade API FAQ](https://developer.tastytrade.com/faq/)
