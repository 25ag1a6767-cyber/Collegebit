import Constants from "expo-constants";
import * as Notifications from "expo-notifications";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";
import { apiRequest } from "../api/client";

const DEVICE_PUSH_TOKEN_KEY = "ace_canteen_expo_push_token_v1";

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: true,
  }),
});

async function ensureAndroidOrderChannelAsync(): Promise<void> {
  if (Platform.OS !== "android") return;
  await Notifications.setNotificationChannelAsync("orders", {
    name: "Order Status Updates",
    importance: Notifications.AndroidImportance.MAX,
    vibrationPattern: [0, 250, 250, 250],
    lightColor: "#12372a",
  });
}

async function registerPushTokenInternal(): Promise<{
  registered: boolean;
  message: string;
}> {
  const extra = Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined;
  const projectId = extra?.eas?.projectId || Constants.easConfig?.projectId;
  try {
    const tokenResult = projectId
      ? await Notifications.getExpoPushTokenAsync({ projectId })
      : await Notifications.getExpoPushTokenAsync();
    const expoPushToken = tokenResult.data;
    const appVersion = Constants.expoConfig?.version || "2.0.0";

    await apiRequest<{ registered: boolean }>("/api/v1/push/device/register", {
      method: "POST",
      body: JSON.stringify({
        expoPushToken,
        platform: Platform.OS,
        appVersion,
      }),
    });
    await SecureStore.setItemAsync(DEVICE_PUSH_TOKEN_KEY, expoPushToken);
    return {
      registered: true,
      message: "Device registered for closed-app order accepted & ready alerts!",
    };
  } catch (err) {
    const detail = err instanceof Error ? err.message : "Could not obtain Expo push token";
    return {
      registered: false,
      message: `${detail} (Requires a real device and EAS projectId / FCM, or use free ntfy/Telegram/local alerts below).`,
    };
  }
}

export async function registerDeviceForPushNotificationsAsync(): Promise<{
  registered: boolean;
  message: string;
}> {
  if (Platform.OS === "web") {
    return { registered: false, message: "Use browser Web Push toggle inside the PWA on web." };
  }
  await ensureAndroidOrderChannelAsync();

  const existingPerm = await Notifications.getPermissionsAsync();
  let finalStatus = existingPerm.status;
  if (finalStatus !== "granted") {
    const requested = await Notifications.requestPermissionsAsync();
    finalStatus = requested.status;
  }
  if (finalStatus !== "granted") {
    return {
      registered: false,
      message: "Push permission was not granted. Enable Notifications in device Settings anytime.",
    };
  }
  return registerPushTokenInternal();
}

export async function syncPushTokenIfAlreadyGrantedAsync(): Promise<void> {
  if (Platform.OS === "web") return;
  try {
    await ensureAndroidOrderChannelAsync();
    const perm = await Notifications.getPermissionsAsync();
    if (perm.status !== "granted") return;
    await registerPushTokenInternal();
  } catch {
    // Silent best-effort sync; local WebSocket notifications remain active
  }
}

export async function scheduleLocalOrderNotificationAsync(
  title: string,
  body: string,
  data?: Record<string, unknown>
): Promise<boolean> {
  if (Platform.OS === "web") return false;
  try {
    await ensureAndroidOrderChannelAsync();
    const perm = await Notifications.getPermissionsAsync();
    if (perm.status !== "granted") return false;
    await Notifications.scheduleNotificationAsync({
      content: {
        title,
        body,
        sound: true,
        data: data || {},
      },
      trigger: null,
    });
    return true;
  } catch {
    return false;
  }
}

export async function sendLocalTestNotificationAsync(): Promise<{
  shown: boolean;
  message: string;
}> {
  if (Platform.OS === "web") {
    return { shown: false, message: "Triggered server alert check." };
  }
  await ensureAndroidOrderChannelAsync();
  const existing = await Notifications.getPermissionsAsync();
  let finalStatus = existing.status;
  if (finalStatus !== "granted") {
    const requested = await Notifications.requestPermissionsAsync();
    finalStatus = requested.status;
  }
  if (finalStatus !== "granted") {
    return {
      shown: false,
      message: "Enable notification permission in device Settings to pop local phone alerts.",
    };
  }
  await Notifications.scheduleNotificationAsync({
    content: {
      title: "CollegeBits Mobile Alert",
      body: "Free order accepted & ready-for-pickup notifications are working!",
      sound: true,
      data: { type: "test" },
    },
    trigger: null,
  });
  return { shown: true, message: "Local mobile notification triggered!" };
}

export async function unregisterDevicePushNotificationsAsync(): Promise<void> {
  try {
    const token = await SecureStore.getItemAsync(DEVICE_PUSH_TOKEN_KEY);
    if (!token) return;
    await apiRequest(`/api/v1/push/device/register?expoPushToken=${encodeURIComponent(token)}`, {
      method: "DELETE",
    });
    await SecureStore.deleteItemAsync(DEVICE_PUSH_TOKEN_KEY);
  } catch {
    // Best-effort cleanup on sign out
  }
}
