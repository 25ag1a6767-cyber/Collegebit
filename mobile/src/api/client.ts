import * as SecureStore from "expo-secure-store";
import { ApiEnvelope, AuthTokenResponse } from "../types/api";

declare const process: { env: Record<string, string | undefined> };

const ACCESS_TOKEN_KEY = "ace_canteen_access_token_v1";
const REFRESH_TOKEN_KEY = "ace_canteen_refresh_token_v1";

export const API_BASE_URL = (
  process.env.EXPO_PUBLIC_API_URL || "http://10.0.2.2:8000"
).replace(/\/+$/, "");

let memoryAccessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export async function getAccessToken(): Promise<string | null> {
  if (memoryAccessToken) return memoryAccessToken;
  try {
    memoryAccessToken = await SecureStore.getItemAsync(ACCESS_TOKEN_KEY);
    return memoryAccessToken;
  } catch {
    return null;
  }
}

export async function saveTokens(accessToken: string, refreshToken?: string | null): Promise<void> {
  memoryAccessToken = accessToken;
  try {
    await SecureStore.setItemAsync(ACCESS_TOKEN_KEY, accessToken);
    if (refreshToken) {
      await SecureStore.setItemAsync(REFRESH_TOKEN_KEY, refreshToken);
    }
  } catch {
    // Fallback to in-memory session if secure hardware enclave is unavailable
  }
}

export async function clearTokens(): Promise<void> {
  memoryAccessToken = null;
  try {
    await SecureStore.deleteItemAsync(ACCESS_TOKEN_KEY);
    await SecureStore.deleteItemAsync(REFRESH_TOKEN_KEY);
  } catch {
    // Ignore storage cleanup errors
  }
}

export async function getRefreshToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
  } catch {
    return null;
  }
}

async function rotateAccessToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const rt = await getRefreshToken();
    if (!rt) return null;
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ refreshToken: rt })
      });
      const json = (await res.json()) as ApiEnvelope<AuthTokenResponse>;
      if (!res.ok || !json.success || !json.data?.accessToken) {
        await clearTokens();
        return null;
      }
      await saveTokens(json.data.accessToken, json.data.refreshToken);
      return json.data.accessToken;
    } catch {
      return null;
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

export class ApiClientError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  allowRefresh = true
): Promise<T> {
  const token = await getAccessToken();
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...((options.headers as Record<string, string>) || {})
  };

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers
  });

  if (response.status === 401 && allowRefresh && !path.startsWith("/api/v1/auth/")) {
    const nextToken = await rotateAccessToken();
    if (nextToken) {
      return apiRequest<T>(path, options, false);
    }
  }

  const payload = (await response.json().catch(() => null)) as ApiEnvelope<T> | null;
  if (!response.ok || (payload && payload.success === false)) {
    throw new ApiClientError(
      payload?.error?.code || `HTTP_${response.status}`,
      payload?.error?.message || `Request failed (${response.status})`,
      response.status
    );
  }
  return (payload && "data" in payload ? payload.data : payload) as T;
}

export function createRealtimeSocket(
  token: string | null,
  onEvent: (event: Record<string, unknown>) => void,
  onStateChange?: (connected: boolean) => void
): () => void {
  let closed = false;
  let ws: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let backoffMs = 1500;

  const wsBase = API_BASE_URL.replace(/^http/, "ws");
  const connect = () => {
    if (closed) return;
    const qs = token ? `?token=${encodeURIComponent(token)}` : "";
    ws = new WebSocket(`${wsBase}/api/v1/ws${qs}`);
    ws.onopen = () => {
      backoffMs = 1500;
      onStateChange?.(true);
    };
    ws.onmessage = msg => {
      try {
        const parsed = JSON.parse(String(msg.data)) as Record<string, unknown>;
        onEvent(parsed);
      } catch {
        // Ignore malformed frame
      }
    };
    ws.onclose = () => {
      onStateChange?.(false);
      if (!closed) {
        retryTimer = setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 1.8, 20000);
      }
    };
    ws.onerror = () => onStateChange?.(false);
  };

  connect();
  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
  };
}

export function formatRupeesFromPaise(paise: number | undefined | null): string {
  return `₹${(Number(paise || 0) / 100).toFixed(2)}`;
}
