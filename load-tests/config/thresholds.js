// Shared k6 configuration and strict SLA thresholds for CollegeBits 3.0
export const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000";

export const STANDARD_THRESHOLDS = {
  http_req_failed: ["rate<0.01"], // Internal API error rate < 1%
  "http_req_duration{endpoint:menu}": ["p(95)<300"], // Menu API p95 < 300 ms
  "http_req_duration{endpoint:orders}": ["p(95)<500"], // Internal Order API p95 < 500 ms
  "http_req_duration{endpoint:pickup}": ["p(95)<350"], // Pickup verification p95 < 350 ms
};

export const RAMP_STAGES_3000 = [
  { duration: "30s", target: 50 },
  { duration: "30s", target: 100 },
  { duration: "45s", target: 250 },
  { duration: "45s", target: 500 },
  { duration: "60s", target: 1000 },
  { duration: "90s", target: 3000 },
  { duration: "30s", target: 0 },
];

export function authHeaders(token, idempotencyKey = null) {
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return { headers };
}
