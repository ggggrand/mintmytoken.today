# MintMyToken — Cardano Token Minting & Vesting Platform
### Live at: [mintmytoken.today](https://mintmytoken.today)

> Mint custom Cardano tokens and distribute them with on-chain trustless vesting — no coding required.

---

## What is MintMyToken?

MintMyToken is an open-access SaaS platform built on Cardano that allows anyone to:

1. **Mint custom tokens** — set name, ticker, supply, decimals, image and metadata in minutes
2. **Vest token distributions** — lock tokens for teams, investors and communities using native script timelocks
3. **Claim vested tokens** — recipients claim directly from their CIP-30 wallet when tranches unlock

Everything is **on-chain and trustless**. Once tokens are locked in a vesting schedule, not even the platform owner can touch them — enforced by Cardano's consensus rules.

---

## Problem We Solve

Launching a token on Cardano currently requires:
- Technical knowledge of PyCardano or cardano-cli
- Understanding of CIP-25 metadata standards
- Manual TX construction for vesting/distribution
- Separate tools for minting, distribution and claiming

**MintMyToken unifies all of this into one platform accessible to non-technical users.**

---

## Features

### Token Minting
- Custom name, ticker, supply, decimals
- Image upload embedded in CIP-25 metadata (64-byte safe splitting)
- Preprod testnet + Mainnet support
- CIP-30 wallet connect (Eternl, Typhon, NuFi)
- Flat fee model — 50 ADA per mint

### Token Vesting Launchpad
- **Cliff vesting** — all tokens unlock on one date
- **Linear vesting** — split into equal tranches over time
- **Bulk CSV upload** — distribute to 1000+ addresses at once
- **Named groups** — Team / Investors / Community each with own schedule
- **Batch signing** — large distributions auto-split into 20-output batches
- Native script timelocks: `ScriptAll([ScriptPubkey(recipient), InvalidBefore(unlock_slot)])`
- Scripts embedded in TX metadata (label 674) — claiming works even if website is down
- Service fee: 10 ADA per vesting project

### Claim Portal
- Recipients connect wallet at `/claim`
- See all locked and unlocked tranches
- One-click claim when tranche is ready
- Manual address lookup (no wallet required to check status)

---

## Technical Architecture

```
Browser (user)
    │  HTTPS :443
    ▼
Apache2  — SSL termination, static file serving
    │  HTTP :5003 (internal)
    ▼
Gunicorn — WSGI server (2 workers)
    │
    ▼
Flask (app.py) — REST API + page routing
    │
    ├── minting.py  — PyCardano TX builder, CIP-30 witness merge
    ├── vesting.py  — Bulk vesting engine, SQLite, batch TX builder
    └── Blockfrost  — UTXO fetching + TX submission
```

### Stack
| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12 + Flask |
| Blockchain | PyCardano 0.10+ |
| Chain API | Blockfrost |
| Database | SQLite (vesting schedules) |
| Server | Gunicorn + Apache2 |
| Frontend | Vanilla JS + CIP-30 wallet API |
| SSL | Let's Encrypt (certbot) |

### Anti-Manipulation Design
Vesting uses native script timelocks:
```python
ScriptAll([
    ScriptPubkey(recipient_vkey_hash),  # only recipient can sign
    InvalidBefore(unlock_slot),          # only after unlock date
])
```
- Owner has **zero keys** to vesting script addresses
- Scripts are stored in TX metadata (label 674) — fully recoverable on-chain
- No admin functions, no upgrade keys, no backdoors
- Verifiable on Cardano explorer by anyone

---

## Repository Structure

```
mintmytoken/
├── app.py                 # Flask routes and API endpoints
├── minting.py             # Token minting engine (PyCardano)
├── vesting.py             # Vesting launchpad engine (PyCardano + SQLite)
├── .env.example           # Environment variable template (no secrets)
├── templates/
│   ├── index.html         # Mainnet minting page
│   ├── preprod.html       # Preprod testnet minting page
│   ├── vest.html          # Vesting launchpad
│   ├── claim.html         # Token claim portal
│   ├── vest_dashboard.html # Distribution dashboard
├── static/
│   └── uploads/           # User-uploaded token images
├── sessions/              # Minting session records
├── keys/                  # Ephemeral policy signing keys
└── docs/
```

---

## API Reference

### Minting
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/build-mint-tx` | POST | Build minting TX for wallet to sign |
| `/api/submit-tx` | POST | Merge wallet witness and submit |
| `/api/upload-image` | POST | Upload token image, returns URL |

### Vesting
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/create-vest-project` | POST | Create vesting project from CSV groups |
| `/api/build-batch-tx` | POST | Build one batch TX (20 outputs) |
| `/api/submit-batch-tx` | POST | Submit signed batch |
| `/api/claim-tranches` | GET | Get claimable tranches for address |
| `/api/build-claim-tx` | POST | Build claim TX for recipient |
| `/api/submit-claim-tx` | POST | Submit signed claim TX |
| `/api/vest-project/<id>` | GET | Project dashboard data |

---

## Minting Flow

```
1. User fills token details (name, supply, image, receive address)
2. User uploads image → stored at /static/uploads/
3. User connects CIP-30 wallet
4. Frontend calls /api/build-mint-tx
5. Backend:
   a. Decodes wallet address (CBOR hex → bech32)
   b. Fetches UTXOs from Blockfrost
   c. Generates ephemeral PaymentSigningKey (policy key)
   d. Creates ScriptPubkey minting policy
   e. Builds TX: inputs → minted tokens to receive_address + 50 ADA to service wallet
   f. Signs with policy key (policy witness)
   g. Returns TX CBOR hex
6. Wallet signs TX inputs (wallet witness)
7. Backend merges witnesses + submits
8. Success: TX hash + Policy ID + Explorer link
```

---

## Vesting Flow

```
1. Token owner visits /vest
2. Creates groups (Team 20%, Investors 30%, Community 50%)
3. Uploads CSV per group: address, amount_or_percentage
4. Sets schedule: cliff date or linear tranches
5. Review: sees cost estimate and unlock timeline
6. Connects wallet holding the tokens
7. Backend creates script addresses for every recipient×tranche
8. Auto-batch signing: wallet signs batches of 20 outputs
9. Tokens locked → recipients visit /claim to collect
```

---

## Roadmap

### Completed ✅
- [x] Token minting (Preprod + Mainnet)
- [x] CIP-25 metadata with image support
- [x] CIP-30 wallet connect (Eternl, Typhon, NuFi)
- [x] Cliff and linear vesting
- [x] Bulk CSV upload (1000+ addresses)
- [x] Batch TX signing
- [x] On-chain script registry (website-independent claiming)
- [x] Terms and Conditions
- [x] SSL + production VPS deployment

### In Progress 🔨
- [ ] Admin dashboard (all mints, fees collected, analytics)
- [ ] Lace wallet support (CBOR address encoding fix)
- [ ] Email notifications for claim events
- [ ] Token explorer integration (show token on DexHunter/TapTools)

### Planned 📋
- [ ] Multi-policy minting (mint multiple tokens in one session)
- [ ] Airdrop tool (send existing tokens to CSV list, no vesting)
- [ ] Token burn function
- [ ] Metadata update service (CIP-27)
- [ ] NFT collection minting (batch, sequential IDs)
- [ ] API access for developers (pay-per-call)
- [ ] DAO governance token templates

---

## Team

### Daniel Gusterov — Lead Developer
Blockchain builder specializing in Cardano.

- **[MintMyToken](https://mintmytoken.today)** — Token minting and vesting platform (this project)
- **[CryptoIntel](https://cryptointel.live)** — Crypto intelligence SaaS dashboard

**Skills:** Python · Flask · PyCardano · Cardano native scripts · CIP-30 · Blockfrost · Apache · Linux VPS · SQLite · JavaScript

**Company:** Kripto Revolucija (registered, Republic of Macedonia)

**Cardano community:** Administrator of large Macedonian Cardano community group

---

## Environment Variables


```env
PREPROD_BLOCKFROST_ID=preprod1...
MAINNET_BLOCKFROST_ID=mainnet1...
SERVICE_ADDRESS_PREPROD=addr_test1...
SERVICE_ADDRESS_MAINNET=addr1...
FLASK_SECRET=<random 32-byte hex>
BASE_URL=https://mintmytoken.today
```

---

## Deployment

See [docs/DEPLOY.md](docs/DEPLOY.md) for full VPS setup guide including:
- Python virtual environment setup
- Apache configuration
- systemd service
- SSL certificate (Let's Encrypt)
- Port assignments

---

## License

---

## Links

- **Live platform:** https://mintmytoken.today
- **Preprod testnet:** https://mintmytoken.today/preprod
- **Vesting launchpad:** https://mintmytoken.today/vest
- **Claim portal:** https://mintmytoken.today/claim
- **Cardano Explorer:** https://cardanoscan.io
- **Blockfrost:** https://blockfrost.io

---

*Built on Cardano · Made in Macedonia · By Daniel Gusterov*
