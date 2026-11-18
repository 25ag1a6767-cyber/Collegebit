import React, { useState } from "react";
import {
  ActivityIndicator,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest } from "../api/client";
import { useAuth } from "../context/AuthContext";

export const AuthScreen: React.FC = () => {
  const { mfaChallengeToken, signIn, verifyMfaLogin, registerStudent } = useAuth();
  const [mode, setMode] = useState<"login" | "register" | "forgot">("login");
  const [fullName, setFullName] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [infoMsg, setInfoMsg] = useState<string | null>(null);

  const handleSubmit = async () => {
    setError(null);
    setInfoMsg(null);
    if (mfaChallengeToken) {
      if (!mfaCode.trim()) {
        setError("Enter your 6-digit authenticator or recovery code.");
        return;
      }
      setBusy(true);
      try {
        await verifyMfaLogin(mfaCode);
      } catch (err) {
        setError(err instanceof Error ? err.message : "2FA verification failed");
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "forgot") {
      if (!identifier.trim().includes("@")) {
        setError("Enter your registered college email address.");
        return;
      }
      setBusy(true);
      try {
        await apiRequest("/api/v1/auth/password/forgot", {
          method: "POST",
          body: JSON.stringify({ email: identifier.trim() })
        });
        setInfoMsg("If an account matches that email, a password reset link has been sent.");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not request password reset");
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "register" && fullName.trim().length < 2) {
      setError("Enter your full name (at least 2 characters).");
      return;
    }
    if (!identifier.trim()) {
      setError(mode === "login" ? "Enter your email address or phone number." : "Enter your college email address.");
      return;
    }
    if (password.length < 12) {
      setError("Password must be at least 12 characters.");
      return;
    }

    setBusy(true);
    try {
      if (mode === "login") {
        await signIn(identifier, password);
      } else {
        await registerStudent(fullName, identifier, password, phone);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
      <View style={styles.card}>
        <Text style={styles.eyebrow}>ACE CANTEEN · MOBILE</Text>
        <Text style={styles.title}>Order ahead. Skip the break queue.</Text>
        <Text style={styles.subtitle}>
          Shared menu, UPI checkout, and single-use QR pickup synced in real time with campus counters.
        </Text>

        {!mfaChallengeToken ? (
          <>
            <View style={styles.tabRow}>
              <TouchableOpacity
                style={[styles.tabBtn, mode === "login" && styles.tabBtnActive]}
                onPress={() => {
                  setMode("login");
                  setError(null);
                  setInfoMsg(null);
                }}
              >
                <Text style={[styles.tabText, mode === "login" && styles.tabTextActive]}>Sign in</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.tabBtn, mode === "register" && styles.tabBtnActive]}
                onPress={() => {
                  setMode("register");
                  setError(null);
                  setInfoMsg(null);
                }}
              >
                <Text style={[styles.tabText, mode === "register" && styles.tabTextActive]}>
                  Create account
                </Text>
              </TouchableOpacity>
            </View>

            {mode === "register" && (
              <>
                <Text style={styles.label}>Full name</Text>
                <TextInput
                  style={styles.input}
                  value={fullName}
                  onChangeText={setFullName}
                  placeholder="Your full name"
                />
              </>
            )}

            <Text style={styles.label}>{mode === "login" ? "Email or phone" : "College email"}</Text>
            <TextInput
              style={styles.input}
              value={identifier}
              onChangeText={setIdentifier}
              autoCapitalize="none"
              keyboardType="email-address"
              placeholder="you@college.edu"
            />

            {mode === "register" && (
              <>
                <Text style={styles.label}>Mobile number (optional)</Text>
                <TextInput
                  style={styles.input}
                  value={phone}
                  onChangeText={setPhone}
                  keyboardType="phone-pad"
                  placeholder="+91 9876543210"
                />
              </>
            )}

            {mode !== "forgot" && (
              <>
                <Text style={styles.label}>Password (12+ characters)</Text>
                <TextInput
                  style={styles.input}
                  value={password}
                  onChangeText={setPassword}
                  secureTextEntry
                  placeholder="Enter password"
                />
              </>
            )}

            {mode === "login" && (
              <TouchableOpacity
                onPress={() => {
                  setMode("forgot");
                  setError(null);
                  setInfoMsg(null);
                }}
              >
                <Text style={styles.forgotLink}>Forgot your password?</Text>
              </TouchableOpacity>
            )}
            {mode === "forgot" && (
              <TouchableOpacity
                onPress={() => {
                  setMode("login");
                  setError(null);
                  setInfoMsg(null);
                }}
              >
                <Text style={styles.forgotLink}>← Back to Sign in</Text>
              </TouchableOpacity>
            )}
          </>
        ) : (
          <>
            <Text style={styles.label}>Two-Factor Authentication Code</Text>
            <TextInput
              style={styles.input}
              value={mfaCode}
              onChangeText={setMfaCode}
              keyboardType="number-pad"
              placeholder="6-digit TOTP or recovery code"
            />
          </>
        )}

        {error && <Text style={styles.errorText}>{error}</Text>}
        {infoMsg && <Text style={styles.infoText}>{infoMsg}</Text>}

        <TouchableOpacity style={styles.primaryBtn} onPress={handleSubmit} disabled={busy}>
          {busy ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text style={styles.primaryBtnText}>
              {mfaChallengeToken
                ? "Verify 2FA Code"
                : mode === "login"
                ? "Sign in"
                : mode === "forgot"
                ? "Send Reset Link"
                : "Create Student Account"}
            </Text>
          )}
        </TouchableOpacity>
      </View>
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  container: { flexGrow: 1, justifyContent: "center", padding: 20, backgroundColor: "#faf9f5" },
  card: {
    backgroundColor: "#fff",
    borderRadius: 20,
    padding: 22,
    borderWidth: 1,
    borderColor: "#dfe8e1"
  },
  eyebrow: { fontSize: 11, fontWeight: "800", letterSpacing: 1.5, color: "#26734e", marginBottom: 6 },
  title: { fontSize: 24, fontWeight: "800", color: "#12372a", marginBottom: 8 },
  subtitle: { fontSize: 13, color: "#5f6f65", lineHeight: 19, marginBottom: 18 },
  tabRow: {
    flexDirection: "row",
    backgroundColor: "#eef3ef",
    borderRadius: 12,
    padding: 4,
    marginBottom: 16
  },
  tabBtn: { flex: 1, paddingVertical: 10, borderRadius: 9, alignItems: "center" },
  tabBtnActive: { backgroundColor: "#fff" },
  tabText: { fontWeight: "700", color: "#4e6458" },
  tabTextActive: { color: "#12372a" },
  label: { fontSize: 13, fontWeight: "700", color: "#234638", marginBottom: 6, marginTop: 10 },
  input: {
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 13,
    paddingVertical: 11,
    fontSize: 15,
    backgroundColor: "#fff",
    color: "#17251d"
  },
  errorText: { color: "#b42318", marginTop: 12, fontSize: 13, fontWeight: "600" },
  infoText: { color: "#155d3d", marginTop: 12, fontSize: 13, fontWeight: "600" },
  forgotLink: { color: "#26734e", fontSize: 13, fontWeight: "700", marginTop: 12 },
  primaryBtn: {
    marginTop: 18,
    backgroundColor: "#12372a",
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center"
  },
  primaryBtnText: { color: "#fff", fontWeight: "800", fontSize: 15 }
});
