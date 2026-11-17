import React, { useCallback, useEffect, useState } from "react";
import {
  Linking,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { API_BASE_URL, apiRequest, saveTokens } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { NotificationItem } from "../types/api";
import { registerDeviceForPushNotificationsAsync } from "../utils/notifications";

export const ProfileScreen: React.FC = () => {
  const { user, wsConnected, refreshProfile, signOut } = useAuth();
  const [fullName, setFullName] = useState(user?.fullName || "");
  const [phone, setPhone] = useState(user?.phone || "");
  const [diet, setDiet] = useState(user?.dietaryPreference || "");
  const [allergens, setAllergens] = useState((user?.allergenPreferences || []).join(", "));
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [prefs, setPrefs] = useState({ inAppEnabled: true, webPushEnabled: true, emailEnabled: false, smsEnabled: false });
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const loadNotifications = useCallback(async () => {
    try {
      const [res, prefRes] = await Promise.all([
        apiRequest<NotificationItem[] | { items: NotificationItem[] }>("/api/v1/users/me/notifications"),
        apiRequest<{ inAppEnabled: boolean; webPushEnabled: boolean; emailEnabled: boolean; smsEnabled: boolean }>(
          "/api/v1/users/me/notification-preferences"
        ).catch(() => null)
      ]);
      setNotifications(Array.isArray(res) ? res : res.items || []);
      if (prefRes) setPrefs(prefRes);
    } catch {
      // Ignore
    }
  }, []);

  const togglePref = async (key: "inAppEnabled" | "webPushEnabled" | "emailEnabled" | "smsEnabled") => {
    const next = { ...prefs, [key]: !prefs[key] };
    setPrefs(next);
    try {
      await apiRequest("/api/v1/users/me/notification-preferences", {
        method: "PATCH",
        body: JSON.stringify({ [key]: next[key] })
      });
      setStatusMsg("Notification delivery preference saved.");
    } catch {
      setPrefs(prefs);
    }
  };

  useEffect(() => {
    loadNotifications();
  }, [loadNotifications]);

  const saveProfile = async () => {
    setStatusMsg(null);
    try {
      await apiRequest("/api/v1/users/me", {
        method: "PATCH",
        body: JSON.stringify({
          fullName: fullName.trim() || undefined,
          phone: phone.trim() || null,
          dietaryPreference: diet.trim() || null,
          allergenPreferences: allergens
            .split(",")
            .map(s => s.trim().toLowerCase())
            .filter(Boolean)
        })
      });
      await refreshProfile();
      setStatusMsg("Student profile and dietary preferences updated.");
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : "Could not update profile");
    }
  };

  const changeAccountPassword = async () => {
    setStatusMsg(null);
    try {
      const res = await apiRequest<{ accessToken: string; refreshToken: string }>(
        "/api/v1/users/me/change-password",
        {
          method: "POST",
          body: JSON.stringify({ currentPassword, newPassword })
        }
      );
      if (res.accessToken && res.refreshToken) {
        await saveTokens(res.accessToken, res.refreshToken);
      }
      setCurrentPassword("");
      setNewPassword("");
      setStatusMsg("Account password changed and active session rotated.");
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : "Password update failed");
    }
  };

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <View style={styles.card}>
        <Text style={styles.name}>{user?.fullName}</Text>
        <Text style={styles.meta}>
          {user?.email} · Customer Account · {wsConnected ? "● Live Push Socket" : "○ Reconnecting"}
        </Text>

        <Text style={styles.label}>Full Name</Text>
        <TextInput style={styles.input} value={fullName} onChangeText={setFullName} />

        <Text style={styles.label}>Mobile Number</Text>
        <TextInput style={styles.input} value={phone} onChangeText={setPhone} />

        <Text style={styles.label}>Dietary Preference (vegetarian, vegan, jain…)</Text>
        <TextInput style={styles.input} value={diet} onChangeText={setDiet} />

        <Text style={styles.label}>Avoid Allergens (comma-separated)</Text>
        <TextInput style={styles.input} value={allergens} onChangeText={setAllergens} />

        <TouchableOpacity style={styles.primaryBtn} onPress={saveProfile}>
          <Text style={styles.primaryBtnText}>Save Profile & Preferences</Text>
        </TouchableOpacity>
      </View>

      <View style={styles.card}>
        <Text style={styles.sectionTitle}>Account Security — Change Password</Text>
        <Text style={styles.label}>Current Password</Text>
        <TextInput
          style={styles.input}
          secureTextEntry
          value={currentPassword}
          onChangeText={setCurrentPassword}
          placeholder="Current password"
        />
        <Text style={styles.label}>New Password (12+ characters)</Text>
        <TextInput
          style={styles.input}
          secureTextEntry
          value={newPassword}
          onChangeText={setNewPassword}
          placeholder="At least 12 characters"
        />
        <TouchableOpacity style={styles.primaryBtn} onPress={changeAccountPassword}>
          <Text style={styles.primaryBtnText}>Update Password</Text>
        </TouchableOpacity>
      </View>

      {statusMsg && <Text style={styles.status}>{statusMsg}</Text>}

      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.sectionTitle}>Push & Order Notifications</Text>
          <TouchableOpacity
            onPress={async () => {
              await apiRequest("/api/v1/users/me/notifications/read-all", { method: "POST" });
              loadNotifications();
            }}
          >
            <Text style={styles.link}>Mark all read</Text>
          </TouchableOpacity>
        </View>
        {notifications.slice(0, 12).map(n => (
          <View key={n.id} style={styles.notifItem}>
            <Text style={styles.notifTitle}>{n.title}</Text>
            <Text style={styles.notifBody}>{n.message}</Text>
          </View>
        ))}
      </View>

      <View style={styles.card}>
        <Text style={styles.sectionTitle}>Notification Channel Preferences</Text>
        <TouchableOpacity style={styles.primaryBtn} onPress={() => togglePref("inAppEnabled")}>
          <Text style={styles.primaryBtnText}>In-App Alerts: {prefs.inAppEnabled ? "ON" : "OFF"}</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.primaryBtn} onPress={() => togglePref("webPushEnabled")}>
          <Text style={styles.primaryBtnText}>Push Alerts: {prefs.webPushEnabled ? "ON" : "OFF"}</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.primaryBtn} onPress={() => togglePref("emailEnabled")}>
          <Text style={styles.primaryBtnText}>Email Receipts: {prefs.emailEnabled ? "ON" : "OFF"}</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.primaryBtn} onPress={() => togglePref("smsEnabled")}>
          <Text style={styles.primaryBtnText}>SMS Order Alerts: {prefs.smsEnabled ? "ON" : "OFF"}</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.primaryBtn}
          onPress={async () => {
            const res = await registerDeviceForPushNotificationsAsync();
            setStatusMsg(res.message);
          }}
        >
          <Text style={styles.primaryBtnText}>Register Device for Closed-App Push</Text>
        </TouchableOpacity>
      </View>

      <View style={styles.card}>
        <Text style={styles.sectionTitle}>Legal &amp; Strict No-Refund Policy</Text>
        <Text style={styles.notifBody}>
          All completed digital payments and prepaid wallet checkouts are final (non-refundable). Unfulfilled paid orders are logged for counter administration resolution.
        </Text>
        <TouchableOpacity
          style={styles.primaryBtn}
          onPress={() => API_BASE_URL && Linking.openURL(`${API_BASE_URL}/legal#privacy`)}
        >
          <Text style={styles.primaryBtnText}>Privacy Policy</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.primaryBtn}
          onPress={() => API_BASE_URL && Linking.openURL(`${API_BASE_URL}/legal#terms`)}
        >
          <Text style={styles.primaryBtnText}>Terms &amp; No-Refund Policy</Text>
        </TouchableOpacity>
      </View>

      <TouchableOpacity style={styles.logoutBtn} onPress={signOut}>
        <Text style={styles.logoutText}>Sign Out</Text>
      </TouchableOpacity>
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  content: { padding: 16, paddingBottom: 32 },
  card: {
    backgroundColor: "#fff",
    borderRadius: 16,
    padding: 16,
    borderWidth: 1,
    borderColor: "#dce7de",
    marginBottom: 14
  },
  name: { fontSize: 18, fontWeight: "800", color: "#12372a" },
  meta: { fontSize: 12, color: "#55685d", marginTop: 4, marginBottom: 8 },
  sectionTitle: { fontSize: 16, fontWeight: "800", color: "#12372a", marginBottom: 8 },
  label: { fontSize: 12, fontWeight: "700", color: "#234638", marginTop: 10, marginBottom: 5 },
  input: {
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 9
  },
  primaryBtn: {
    marginTop: 12,
    backgroundColor: "#12372a",
    paddingVertical: 12,
    borderRadius: 10,
    alignItems: "center"
  },
  primaryBtnText: { color: "#fff", fontWeight: "700" },
  status: {
    backgroundColor: "#eaf6ef",
    color: "#155d3d",
    padding: 10,
    borderRadius: 10,
    marginBottom: 12,
    fontWeight: "600"
  },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  link: { color: "#26734e", fontWeight: "700", fontSize: 12 },
  notifItem: { borderTopWidth: 1, borderColor: "#edf2ee", paddingVertical: 8 },
  notifTitle: { fontWeight: "700", fontSize: 13, color: "#17251d" },
  notifBody: { fontSize: 12, color: "#55685d", marginTop: 2 },
  logoutBtn: {
    paddingVertical: 13,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#fecdca",
    backgroundColor: "#fff",
    alignItems: "center"
  },
  logoutText: { color: "#b42318", fontWeight: "800" }
});
