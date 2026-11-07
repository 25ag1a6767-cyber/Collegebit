import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  apiRequest,
  clearTokens,
  createRealtimeSocket,
  getAccessToken,
  getRefreshToken,
  saveTokens
} from "../api/client";
import { AuthTokenResponse, UserProfile } from "../types/api";

interface AuthContextValue {
  user: UserProfile | null;
  loading: boolean;
  wsConnected: boolean;
  lastRealtimeTick: number;
  mfaChallengeToken: string | null;
  signIn: (identifier: string, password: string) => Promise<void>;
  verifyMfaLogin: (code: string) => Promise<void>;
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
  const [mfaChallengeToken, setMfaChallengeToken] = useState<string | null>(null);

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
    let cleanup: (() => void) | undefined;
    (async () => {
      const token = await getAccessToken();
      cleanup = createRealtimeSocket(
        token,
        event => {
          if (event.topic && event.topic !== "connected" && event.topic !== "pong") {
            setLastRealtimeTick(Date.now());
          }
        },
        connected => setWsConnected(connected)
      );
    })();
    return () => cleanup?.();
  }, [user?.id]);

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
      const rt = await getRefreshToken();
      await apiRequest("/api/v1/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refreshToken: rt })
      }, false);
    } catch {

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
        mfaChallengeToken,
        signIn,
        verifyMfaLogin,
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
