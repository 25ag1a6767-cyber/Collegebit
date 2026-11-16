import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 20,
  iterations: 100,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const token = __ENV.COORD_TOKEN || "";
  const bulkOrderId = __ENV.BULK_ORDER_ID || "1";
  const csvRes = http.get(`${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/tokens/export?format=csv`, authHeaders(token));
  const pdfRes = http.get(`${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/tokens/export?format=pdf`, authHeaders(token));
  check(csvRes, { "CSV export valid or mode restricted": (r) => [200, 403].includes(r.status) });
  check(pdfRes, { "PDF export valid or mode restricted": (r) => [200, 403].includes(r.status) });
}
