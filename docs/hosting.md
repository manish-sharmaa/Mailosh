# Hosting guide — sizing, cost, port 25 reality

Facts verified 2026-08-31 from official pages; items marked *unverified* could not be confirmed from a primary source that day. This doc backs spec §12 and the wizard's relay-mode recommendation.

## Sizing (what Mailosh actually needs)

- **Stalwart:** ~100 MB RAM idle; official docs: "a system with 1 GB of RAM is generally sufficient" for 5–10 users; one core fine at that scale (stalw.art/docs/install/requirements). Native ACME TLS with HTTP-01, DNS-01, DNS-PERSIST-01, TLS-ALPN-01 challenges (stalw.art/docs/server/tls/acme/challenges) — feeds SPK-4.
- **PostgreSQL:** on a shared 4 GB box set `shared_buffers` ≈ 512 MB–1 GB (official guidance: 25 % of RAM on a dedicated ≥1 GB server; >40 % rarely helps).
- **Reference box:** 2 vCPU / 4 GB / ≥40 GB NVMe runs stalwart + postgres + mailosh + worker for a family/small team with headroom. Dev = local Docker, free.

## VPS shortlist (~4 GB tier)

| Provider | Price/mo | Port 25 (outbound) | PTR/rDNS | India | Notes |
|---|---|---|---|---|---|
| Hetzner Cloud CX23 (EU) | ~€5 range (*price JS-rendered, unverified*), 20 TB traffic | Blocked on new accounts; **documented unblock** via console request after 1 month + first paid invoice | Self-serve console | No | Best documented direct-send path in the budget class |
| OVH VPS-1 | **$4.54** (annual) / ₹420 excl. GST | *Unverified* (no documented default block found) | Self-serve | Billing yes, no India DC | Cheapest credible box overall |
| Contabo Cloud VPS 4 | **€5.50** (4 c/8 GB!) | *Unverified* | Self-serve panel | Region exists (*surcharge unverified*) | Big specs, noisy-neighbour reputation |
| netcup VPS 500 G12 | **€5.91** incl. VAT (2 c/4 GB/128 GB) | *Unverified* | *Unverified* | No | 12-month term |
| Vultr vc2-2c-4gb | **$20** | Blocked; **unblock via support ticket** | Portal (*page 404, unverified*) | **Mumbai, Delhi, Bangalore** | Best India direct-send option |
| Linode/Akamai 4 GB | **$24** | Restricted (post-2019 accounts); lift via support with use case | Yes (expected for mail) | **Mumbai ×2, Chennai** | Solid India alternative |
| DigitalOcean 4 GB | **$24** | **Blocked, no unblock process** — DO says use a third-party sender | *Unverified* | BLR1 | Relay mode only |
| AWS Lightsail 4 GB | **$24** (Mumbai: 2 TB transfer) | Limited; removal form still documented (root-user request) | Via AWS Support (same request) | ap-south-1 | Heavier process |
| Oracle Always Free | $0 — **now 2 OCPU / 12 GB** (1,500 OCPU-hrs + 9,000 GB-hrs/mo), idle instances reclaimed | Direct send effectively unavailable (docs only offer their Email Delivery relay) | *Unverified* | Home region can be Mumbai/Hyderabad (*list unverified*) | Relay-mode toy/dev box only |
| Scaleway | *pricing page blocked, unverified* | Blocked by default; enable-SMTP toggle **requires KYC** | *Unverified* | No | |

## Outbound relay (for `SMTP_MODE=relay`)

| Relay | Price | Free tier | Gotcha |
|---|---|---|---|
| Amazon SES | **$0.10/1,000** (à la carte; "Essentials" $0.16/1k) | $200 credits/6 mo | +$0.12/GB attachments; production-access review |
| SMTP2GO | $10/mo per 10k (or $100/yr) | **1,000/mo (200/day)** | Best pure-SMTP fit for personal servers |
| Resend | $20/mo per 50k | 3,000/mo (100/day) | API-first |
| Postmark | $15/mo per 10k | 100/mo dev | Streams split transactional/broadcast |
| Mailgun | $15/mo per 10k | 100/day | Overage $1.80/1k |
| Brevo | *unverified* (commonly cited 300/day free) | — | Marketing-suite oriented |

Benchmark for honesty in our docs: **Migadu Micro $19/yr** hosts mailboxes with zero ops (20 outbound/day cap) — self-hosting is about ownership, not saving money at tiny scale.

## Recommended paths (wizard defaults)

1. **EU, direct send:** Hetzner CX23 → request port-25 unblock after month 1 (run relay mode until granted) → set PTR to `mail.example.com`. ~€5–6/mo all-in.
2. **India, direct send:** Vultr Mumbai/Delhi/Bangalore $20/mo → support ticket for port 25 → PTR.
3. **Cheapest overall (recommended for most):** any €4–6 VPS (OVH/Contabo/netcup) + **relay mode** on port 587 via SES ($0.10/1k) or SMTP2GO free tier. No port-25 dependency, inbound MX works everywhere. ~€5/mo + pennies.
4. Add object storage only when local disk pressures: Backblaze B2/Hetzner/IDrive e2 (~$5–7/TB, free API calls); OVH Mumbai/DO Bangalore for India residency. Mail storage ≈ $0.15–0.25/user/yr.

**Wizard implication (spec §11):** always probe outbound 25; when blocked, preselect relay mode and link this doc. PTR/rDNS check belongs in the health panel with provider-specific fix links.
