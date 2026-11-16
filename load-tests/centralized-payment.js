import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 20,
  iterations: 50,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const token = __ENV.COORD_TOKEN || "";
  const bulkOrderId = __ENV.BULK_ORDER_ID || "1";
  const idem = `central-pay-${bulkOrderId}-${__VU}-${__ITER}`;
  const intentRes = http.post(
    `${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/payment-intent`,
    "{}",
    authHeaders(token, idem)
  );
  check(intentRes, {
    "centralized payment intent created or already paid": (r) => r.status === 200 || r.status === 409,
  });
}
