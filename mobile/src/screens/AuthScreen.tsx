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
import { useAuth } from "../context/AuthContext";

export const AuthScreen: React.FC = () => {
  const { mfaChallengeToken, signIn, verifyMfaLogin, registerStudent } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [fullName, setFullName] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    setError(null);
    setBusy(true);
    try {
      if (mfaChallengeToken) {
        await verifyMfaLogin(mfaCode);
        return;
      }
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
        <Text style={styles.eyebrow}>ACE CANTEEN · ANDROID</Text>
        <Text style={styles.title}>Order ahead. Skip the break queue.</Text>
        <Text style={styles.subtitle}>
          Shared menu, UPI checkout, and single-use QR pickup synced in real time with campus counters.
        </Text>

        {!mfaChallengeToken ? (
          <>
            <View style={styles.tabRow}>
              <TouchableOpacity
                style={[styles.tabBtn, mode === "login" && styles.tabBtnActive]}
                onPress={() => setMode("login")}
              >
                <Text style={[styles.tabText, mode === "login" && styles.tabTextActive]}>Sign in</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.tabBtn, mode === "register" && styles.tabBtnActive]}
                onPress={() => setMode("register")}
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

            <Text style={styles.label}>Password (12+ characters)</Text>
            <TextInput
              style={styles.input}
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              placeholder="Enter password"
            />
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

        <TouchableOpacity style={styles.primaryBtn} onPress={handleSubmit} disabled={busy}>
          {busy ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text style={styles.primaryBtnText}>
              {mfaChallengeToken
                ? "Verify 2FA Code"
                : mode === "login"
                ? "Sign in"
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
  primaryBtn: {
    marginTop: 18,
    backgroundColor: "#12372a",
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center"
  },
  primaryBtnText: { color: "#fff", fontWeight: "800", fontSize: 15 }
});
