import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 5,
  iterations: 10,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const token = __ENV.COORD_TOKEN || "";
  const bulkOrderId = __ENV.BULK_ORDER_ID || "1";
  const res = http.post(
    `${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/tokens/generate`,
    JSON.stringify({ regenerate: false, expiryHours: 24 }),
    authHeaders(token)
  );
  check(res, { "tokens generated 200": (r) => r.status === 200 });
}
