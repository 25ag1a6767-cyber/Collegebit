import http from "k6/http";
import { check, sleep } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, RAMP_STAGES_3000, authHeaders } from "./config/thresholds.js";

// Full 5-minute lunch break spike: 50 -> 100 -> 250 -> 500 -> 1,000 -> 3,000 simulated students
export const options = {
  stages: RAMP_STAGES_3000,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const menuRes = http.get(`${BASE_URL}/api/v1/menu`, { tags: { endpoint: "menu" } });
  check(menuRes, { "menu 200": (r) => r.status === 200 });

  const boardRes = http.get(`${BASE_URL}/api/v1/queue/display-board`, { tags: { endpoint: "menu" } });
  check(boardRes, { "display board 200": (r) => r.status === 200 });

  const progressRes = http.get(`${BASE_URL}/api/v1/pickup/progress`, authHeaders(__ENV.STUDENT_TOKEN || ""));
  check(progressRes, { "two-counter progress": (r) => [200, 401].includes(r.status) });

  sleep(0.25);
}
