import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 100,
  iterations: 500,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const token = __ENV.STUDENT_TOKEN || "";
  const bulkOrderId = __ENV.BULK_ORDER_ID || "1";
  const recipientId = (__ITER % 500) + 1;
  const idem = `indiv-pay-${bulkOrderId}-${recipientId}`;
  const res = http.post(
    `${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/recipients/${recipientId}/payment-intent`,
    "{}",
    authHeaders(token, idem)
  );
  check(res, {
    "individual payment intent isolated": (r) => [200, 404, 409].includes(r.status),
  });
}
