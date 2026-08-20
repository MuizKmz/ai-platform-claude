# Demo business API

A stand-in write target for Phase 9's approval flow. **Not part of the
platform** — this is the thing a write connector writes *to*, standing in for
whatever real system a deployment would point at: a ticketing API, an ERP's
REST surface, a WMS endpoint.

## Running it

```powershell
cd backend
uv run --with fastapi --with uvicorn python -m uvicorn main:app --port 9100 --app-dir ..\infra\demo-api
```

Health: http://127.0.0.1:9100/health · Docs: http://127.0.0.1:9100/docs

## What it demonstrates

| Endpoint | Purpose |
|---|---|
| `POST /tickets` | Create. **Requires `Idempotency-Key`** |
| `GET /tickets/{id}` | Read one back |
| `GET /tickets` | List |
| `DELETE /tickets/{id}` | The compensating action — cancels, does not delete |
| `POST /_reset` | Clear state. Tests only |

### Idempotency

```powershell
curl.exe -s -X POST http://127.0.0.1:9100/tickets `
  -H "Content-Type: application/json" -H "Idempotency-Key: K1" `
  -d '{\"title\":\"Shipping delay\",\"priority\":\"high\"}'
```

- No key → **400**. Refused rather than tolerated: a target that accepted keyless
  writes would let the platform omit them, and the omission would surface as a
  duplicate in production.
- First call → **201** with a new ticket.
- Same key again → **200** with the *same* ticket. One ticket exists.

The status code is the observable difference between "this created something"
and "this found what your last call created". A client that treats them alike
cannot tell a duplicate from a success.

## Why a real service rather than a mock

The two properties Phase 9 must demonstrate — idempotency and compensating
actions — are properties of an HTTP conversation. A mock returning whatever the
test wants proves the platform *calls* something; it cannot prove that calling
twice creates one ticket, because the mock is the thing deciding that. Here the
tests observe the behaviour rather than assert it.

State is in memory. Restarting forgets everything, which is correct for a
fixture and would be catastrophic for a real system.
