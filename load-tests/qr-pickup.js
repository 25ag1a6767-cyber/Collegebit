import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 50,
  iterations: 500,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const staffToken = __ENV.STAFF_TOKEN || "";
  const qrToken = __ENV.SAMPLE_QR_TOKEN || `bmt_1_${__ITER + 1}_sample`;
  const counterId = (__ITER % 2) + 1;
  const res = http.post(
    `${BASE_URL}/api/v1/pickup/verify`,
    JSON.stringify({ qrToken, counterId }),
    { ...authHeaders(staffToken), tags: { endpoint: "pickup" } }
  );
  check(res, {
    "pickup verify handled": (r) => [200, 404, 409].includes(r.status),
  });
}
