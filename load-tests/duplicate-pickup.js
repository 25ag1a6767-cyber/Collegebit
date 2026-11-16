import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 20,
  iterations: 20,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const staffToken = __ENV.STAFF_TOKEN || "";
  const sharedToken = __ENV.SHARED_QR_TOKEN || "bmt_1_1_duplicate_test";
  const res = http.post(
    `${BASE_URL}/api/v1/pickup/verify`,
    JSON.stringify({ qrToken: sharedToken, counterId: (__VU % 2) + 1 }),
    { ...authHeaders(staffToken), tags: { endpoint: "pickup" } }
  );
  check(res, {
    "exactly one succeeds or rejected 409/404": (r) => [200, 404, 409].includes(r.status),
  });
}
