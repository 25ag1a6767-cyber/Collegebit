import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 5,
  iterations: 10,
  thresholds: STANDARD_THRESHOLDS,
};

function buildCsvRows(count) {
  const lines = ["recipient_identifier,full_name,email,department,quantity"];
  for (let i = 1; i <= count; i++) {
    lines.push(`25AG1A${String(i).padStart(4, "0")},Student ${i},stu${i}@college.edu,CSE,1`);
  }
  return lines.join("\n");
}

export default function () {
  const token = __ENV.COORD_TOKEN || "";
  const bulkOrderId = __ENV.BULK_ORDER_ID || "1";
  const csvContent = buildCsvRows(3000);
  const res = http.post(
    `${BASE_URL}/api/v1/bulk-orders/${bulkOrderId}/recipients/import`,
    JSON.stringify({ csvContent, runInBackground: true }),
    authHeaders(token)
  );
  check(res, {
    "3000 CSV import succeeded": (r) => r.status === 200 && r.json("data.totalCsvRows") === 3000,
  });
}
