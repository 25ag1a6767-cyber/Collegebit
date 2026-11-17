# ACE Canteen (CollegeBits) — 1-Page Staff Quick Guide

**Portal URL:** `https://<your-canteen-domain>/staff` · **TV Display Board:** `/display` *(no student PII shown)*

---

## 1. Shift Start (2 Minutes)
1. Open `/staff` in Chrome or Safari and sign in with your **Staff account** (or change temporary password if prompted).
2. Select your assigned **Counter** (orders are scoped to your counter).
3. Check the **Availability, Stock & Alerts** tab and mark any sold-out breakfast/lunch items unavailable **before** rush hour starts.

---

## 2. Live Kitchen Queue (KDS) Workflow
Only **paid / verified** orders reach your queue (`payment_status = PAID`). Unpaid carts never show up as active tickets.

1. **Accept (`Accept Order`):** Locks the ticket to kitchen prep and alerts the student (push + live status + TV token board).
2. **Preparing (`Start Prep`):** Moves order into active cooking.
   - *Parcel items:* Verify packaging in the **Packing Check** checkbox before marking **Ready** (orders with unverified parcel items cannot advance to Ready).
3. **Ready (`Mark Ready`):** Moves token onto the **TV Token Board (`/display`)** under *Ready for Pickup* and sends the student's **Ready for pickup** notification.

---

## 3. Counter Pickup Verification (QR or 6-Character Code)
**Always verify pickup in the portal before handing over food — never hand over orders from a photo/screenshot without scanning.**

1. Open **QR Scanner & Order Verification** -> tap **Toggle Camera Scanner** (or click into **Scan or Input QR / Token** with a USB scanner).
   - *If Safari/browser blocks camera:* Tap **Allow** in browser Camera Settings, or type the student's **Order ID + 6-character manual code**.
2. Inspect the verified order details (Token #, items, quantity, counter match).
3. Tap **Verify & Complete Pickup Handover**.
   - **Single-use rule:** Once marked **Collected**, the QR expires permanently. Scanning a reused or expired QR will show an error — **do not hand out a duplicate meal**.

---

## 4. Stockouts, Rejections & Kitchen Exceptions (Strict No-Refund Rule)
- **Cannot fulfill a paid order?** Click **Reject / Kitchen Exception** and enter a brief clear reason (e.g., `"Dosa batter finished"`).
- **What happens automatically:**
  - Reserved item stock is **restored** automatically.
  - Because **no automated refunds exist**, the payment record is preserved and flagged as an open **Payment Incident** for Admin review.
  - Explain politely to the student to check with the canteen counter/admin for an authorized substitute item or manual resolution.

---

## 5. End of Shift Checklist
- Confirm zero **Ready / Uncollected** tickets remain from the break (hand over or escalate exceptions).
- Close camera stream / sign out on shared counter tablets.
