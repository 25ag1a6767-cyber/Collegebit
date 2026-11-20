import * as Notifications from "expo-notifications";
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import {
  apiRequest,
  clearTokens,
  createRealtimeSocket,
  getAccessToken,
  getRefreshToken,
  saveTokens
} from "../api/client";
import { AuthTokenResponse, UserProfile } from "../types/api";
import {
  scheduleLocalOrderNotificationAsync,
  syncPushTokenIfAlreadyGrantedAsync,
  unregisterDevicePushNotificationsAsync
} from "../utils/notifications";

interface LiveBanner {
  title: string;
  body: string;
}

interface AuthContextValue {
  user: UserProfile | null;
  loading: boolean;
  wsConnected: boolean;
  lastRealtimeTick: number;
  liveBanner: LiveBanner | null;
  dismissLiveBanner: () => void;
  mfaChallengeToken: string | null;
  signIn: (identifier: string, password: string) => Promise<void>;
  verifyMfaLogin: (code: string) => Promise<void>;
  cancelMfaLogin: () => void;
  registerStudent: (fullName: string, email: string, password: string, phone?: string) => Promise<void>;
  refreshProfile: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function normalizeUser(raw: UserProfile): UserProfile {
  return {
    ...raw,
    role: String(raw.role || "student").toLowerCase() as UserProfile["role"],
    allergenPreferences: raw.allergenPreferences || [],
    counterIds: (raw as unknown as { assignedCounterIds?: number[] }).assignedCounterIds || raw.counterIds || []
  };
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const [lastRealtimeTick, setLastRealtimeTick] = useState<number>(0);
  const [liveBanner, setLiveBanner] = useState<LiveBanner | null>(null);
  const [mfaChallengeToken, setMfaChallengeToken] = useState<string | null>(null);
  const seenEventKeysRef = useRef<Set<string>>(new Set());

  const dismissLiveBanner = useCallback(() => setLiveBanner(null), []);

  useEffect(() => {
    if (!liveBanner) return;
    const t = setTimeout(() => setLiveBanner(null), 6500);
    return () => clearTimeout(t);
  }, [liveBanner]);

  const refreshProfile = useCallback(async () => {
    const profile = await apiRequest<UserProfile>("/api/v1/users/me");
    setUser(normalizeUser(profile));
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const token = await getAccessToken();
        if (token) {
          await refreshProfile();
        }
      } catch {
        await clearTokens();
        setUser(null);
      } finally {
        setLoading(false);
      }
    })();
  }, [refreshProfile]);

  useEffect(() => {
    if (!user?.id) return;
    syncPushTokenIfAlreadyGrantedAsync();
  }, [user?.id]);

  useEffect(() => {
    let cleanup: (() => void) | undefined;
    (async () => {
      const token = await getAccessToken();
      cleanup = createRealtimeSocket(
        token,
        event => {
          const topic = String(event.topic || "");
          if (topic && topic !== "connected" && topic !== "authenticated" && topic !== "pong") {
            setLastRealtimeTick(Date.now());
            const payload = (event.payload || {}) as Record<string, unknown>;
            if (topic === "notification.created") {
              const title = String(payload.title || "CollegeBits Update");
              const body = String(payload.message || "");
              const key = `ntf:${String(payload.entityType || "")}:${String(payload.entityId || "")}:${title}:${body}`;
              if (!seenEventKeysRef.current.has(key)) {
                seenEventKeysRef.current.add(key);
                if (seenEventKeysRef.current.size > 40) seenEventKeysRef.current.clear();
                setLiveBanner({ title, body });
                scheduleLocalOrderNotificationAsync(title, body, payload);
              }
            } else if (topic === "order.ready") {
              const tokNum = payload.tokenNumber ? `Token #${String(payload.tokenNumber)}` : "Your order";
              const title = "Order ready for pickup!";
              const body = `${tokNum} — show your pickup QR at the counter.`;
              const key = `ready:${String(payload.id || tokNum)}`;
              if (!seenEventKeysRef.current.has(key)) {
                seenEventKeysRef.current.add(key);
                setLiveBanner({ title, body });
                scheduleLocalOrderNotificationAsync(title, body, payload);
              }
            }
          }
        },
        connected => setWsConnected(connected)
      );
    })();
    return () => cleanup?.();
  }, [user?.id]);

  useEffect(() => {
    const sub1 = Notifications.addNotificationReceivedListener(() => {
      setLastRealtimeTick(Date.now());
    });
    const sub2 = Notifications.addNotificationResponseReceivedListener(() => {
      setLastRealtimeTick(Date.now());
    });
    return () => {
      sub1.remove();
      sub2.remove();
    };
  }, []);

  const signIn = useCallback(async (identifier: string, password: string) => {
    const trimmed = identifier.trim();
    const data = await apiRequest<AuthTokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ identifier: trimmed, password })
    });
    if (data.mfaRequired && data.mfaChallengeToken) {
      setMfaChallengeToken(data.mfaChallengeToken);
      return;
    }
    if (data.accessToken) {
      await saveTokens(data.accessToken, data.refreshToken);
      setMfaChallengeToken(null);
      if (data.user) setUser(normalizeUser(data.user));
      else await refreshProfile();
    }
  }, [refreshProfile]);

  const verifyMfaLogin = useCallback(async (code: string) => {
    if (!mfaChallengeToken) return;
    const data = await apiRequest<AuthTokenResponse>("/api/v1/auth/mfa/verify-login", {
      method: "POST",
      body: JSON.stringify({ mfaChallengeToken, code: code.trim() })
    });
    if (data.accessToken) {
      await saveTokens(data.accessToken, data.refreshToken);
      setMfaChallengeToken(null);
      if (data.user) setUser(normalizeUser(data.user));
      else await refreshProfile();
    }
  }, [mfaChallengeToken, refreshProfile]);

  const cancelMfaLogin = useCallback(() => {
    setMfaChallengeToken(null);
  }, []);

  const registerStudent = useCallback(
    async (fullName: string, email: string, password: string, phone?: string) => {
      await apiRequest("/api/v1/auth/register", {
        method: "POST",
        body: JSON.stringify({
          fullName: fullName.trim(),
          email: email.trim(),
          password,
          phone: phone?.trim() || null
        })
      });
      await signIn(email, password);
    },
    [signIn]
  );

  const signOut = useCallback(async () => {
    try {
      await unregisterDevicePushNotificationsAsync();
      const rt = await getRefreshToken();
      await apiRequest("/api/v1/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refreshToken: rt })
      }, false);
    } catch {
      // Ignore network errors during sign-out
    } finally {
      await clearTokens();
      setUser(null);
      setMfaChallengeToken(null);
    }
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        wsConnected,
        lastRealtimeTick,
        liveBanner,
        dismissLiveBanner,
        mfaChallengeToken,
        signIn,
        verifyMfaLogin,
        cancelMfaLogin,
        registerStudent,
        refreshProfile,
        signOut
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
