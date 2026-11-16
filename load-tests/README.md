# CollegeBits 3.0 — k6 Load & High-Concurrency Test Suite

This directory contains the k6 load-testing scripts for validating the **3,000-student 5-minute lunch ordering window**, **3,000-recipient bulk orders**, **dual-mode payments**, **cryptographic QR token generation**, **two-counter physical pickup**, and **500-vs-600 atomic inventory contention**.

## Directory Structure
- `config/thresholds.js` — Shared stages (`50 -> 100 -> 250 -> 500 -> 1000 -> 3000` VUs) and SLA thresholds (`p95 < 300ms` menu, `p95 < 500ms` orders, `< 1%` error rate).
- `individual-checkout.js` — Individual menu browsing & idempotent order creation.
- `bulk-order-creation.js` — Concurrent master bulk order creation (centralized & individual modes).
- `bulk-csv-import.js` — 3,000-recipient CSV batch import (`POST /api/v1/bulk-orders/{id}/recipients/import`).
- `centralized-payment.js` — Coordinator centralized bulk payment intents & idempotency.
- `individual-payment.js` — Concurrent individual recipient payment intents & isolation.
- `payment-webhooks.js` — Signed Razorpay webhook burst & duplicate delivery idempotency.
- `token-generation.js` — Batch generation of 3,000 unique SHA-256 hashed `bmt_...` QR tokens.
- `token-distribution.js` — Representative CSV and PDF token exports.
- `qr-pickup.js` — Two-counter (`Counter 1` & `Counter 2`) QR token redemption.
- `duplicate-pickup.js` — Simultaneous duplicate QR scan race condition (`409 TOKEN_ALREADY_REDEEMED`).
- `inventory-contention.js` — 600 concurrent buyers competing for 500 available meals (zero overselling).
- `full-lunch-spike.js` — Complete 5-minute lunch rush across 50, 100, 250, 500, 1,000, and 3,000 virtual users.

## Running with k6 CLI
```bash
k6 run -e BASE_URL=http://127.0.0.1:8000 load-tests/full-lunch-spike.js
k6 run -e BASE_URL=http://127.0.0.1:8000 -e COORD_TOKEN=$TOKEN load-tests/bulk-csv-import.js
k6 run -e BASE_URL=http://127.0.0.1:8000 -e STUDENT_TOKEN=$TOKEN load-tests/inventory-contention.js
```

## Running the Integrated Python High-Concurrency & 3,000-Recipient Benchmark
When the `k6` binary is not installed on the local machine, run the full local benchmark harness:
```powershell
$env:PYTHONPATH = "backend"
py -3 scripts/run_v3_bulk_and_lunch_spike_benchmark.py
```
