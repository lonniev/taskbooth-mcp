# Spike: Cashu 2-of-3 escrow (NUT-11)

Task 22, option 0. Run 2026-11-17 against `testnut.cashu.space` (CDK mint 0.18.1), which mints pretend sats. Not production code; nothing in `src/` imports it.

```
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt
mkdir -p data && .venv/bin/python -I spike.py
```

## The lock

`data` = client, `pubkeys` = [worker, arbiter], `n_sigs` = 2, `locktime` = the refund date, `refund` = [client], `sigflag` = SIG_ALL.

A coordinator stands in for the Operator. It builds the payout outputs, gathers the signatures and submits the swap, and it holds no key in the lock. Each payout output is itself locked to its recipient's key, so the coordinator, who unblinds them, cannot spend them.

## Results (14 of 14 as expected)

| Check | Who signs | Result |
|---|---|---|
| Award split by grade (700 / 299) | client + worker | paid |
| The same signatures with the outputs swapped for the coordinator's | client + worker | refused (20008) |
| Coordinator spends the worker's payout | coordinator | refused |
| Worker and client each spend their share | each alone | paid |
| The same escrow paid twice | client + worker | refused (11001) |
| Dispute for the worker | arbiter + worker | paid |
| Dispute for the client | arbiter + client | paid |
| Worker, client or arbiter alone before the refund date | one | refused (20008) |
| Worker alone after the refund date | worker | refused (20008) |
| Refund after the date | client alone | paid |
| SIG_INPUTS lock: worker reuses the client's signatures and pays itself everything | client + worker | paid, which is why SIG_INPUTS must not be used |

Fee: 1 sat per 1000-sat escrow (the mint charges 100 ppk per input).

## Findings

1. SIG_ALL is required. Under SIG_INPUTS each signature covers only its input, so whoever submits the swap chooses where the money goes. Under SIG_ALL the signatures cover every output, and changing the split is refused.
2. The Operator never holds spendable ecash: the escrow needs two of three keys, and every payout is locked to its recipient.
3. Nutshell 0.20.2 builds the SIG_ALL message in an older form (secrets and `B_` only). The current NUT-11 message is `secret_i || C_i ... || amount_j || B_j`; the spike builds that itself, and the mint accepts it.
4. Nutshell is heavy for an Operator dependency: it pulls in the whole mint (FastAPI, SQLAlchemy, a wallet database), and it broke on marshmallow 4. The parts escrow needs are small: blinding and unblinding (NUT-00), the secret JSON, a Schnorr signature and three HTTP calls. Blinding is secp256k1 point arithmetic, which by DPYC rule belongs in tollbooth-dpyc.
5. Keep escrow proofs apart from ordinary coins: a swap mixing SIG_ALL inputs with plain ones fails (the CDK mint answers with error 50000).
6. A lock has one date. Here it is the refund date; a silent client is covered by arbiter + worker.

## Part 2: Lightning in and out (`spike_lightning.py`)

No one holds a Cashu wallet. The coordinator asks the mint for an invoice bound to the client's key (NUT-20); the client pays it from any Lightning wallet; the coordinator mints straight into the lock, with the client's signature on the outputs. Payouts are melted to each party's own Lightning invoice. A payout wholly to one party is one melt from the escrow. A split by grade is one swap signed by client and worker into two groups, each locked to its recipient, then one melt per recipient, signed by that recipient.

| Check | Who signs | Result |
|---|---|---|
| Client's paid invoice minted into the lock | client | escrow funded |
| The same invoice minted with the worker's signature | worker | refused (20008) |
| Award split 700 / 299 into two groups | client + worker | done |
| Coordinator melts the worker's group to its own invoice | coordinator | refused (20008) |
| Worker's group to the worker's invoice | worker | 686 paid |
| Client's group to the client's invoice | client | 293 paid |
| Dispute: escrow straight to the worker's invoice | arbiter + worker | 980 paid |
| Worker alone melts the escrow | worker | refused (20008) |
| Refund after the date: escrow straight to the client's invoice | client | 980 paid |

The mint accepted the current NUT-20 message (`Cashu_MintQuoteSig_v1`, length-prefixed).

### Findings

7. The funding invoice is bound to the client's key, so a leaked quote id cannot be minted by anyone else.
8. The fee reserve (about 2% here) is held back from every melt, and whatever Lightning routing does not use comes back as ecash change, locked to the recipient. On testnut all of it came back (13, 5, 19 and 19 sats). To keep "no Cashu wallet" true, the Operator needs a plan for that change: add it to the recipient's next payout, or melt it once it is worth a payment. A mint with a smaller reserve shrinks it.
9. A split by grade costs two steps and two sets of fees; a payout to one party costs one.

## Not yet tried

- A real mint, its fees, its limits and its reserve on real Lightning routes.
- The change plan in finding 8.
