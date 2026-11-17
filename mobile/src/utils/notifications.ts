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

export async function registerDeviceForPushNotificationsAsync(): Promise<{
  registered: boolean;
  message: string;
}> {
  if (Platform.OS === "web") {
    return { registered: false, message: "Use browser Web Push toggle inside the PWA on web." };
  }

  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("orders", {
      name: "Order Status Updates",
      importance: Notifications.AndroidImportance.MAX,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: "#12372a",
    });
  }

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
      message: `${detail} (Requires a real device and EAS projectId / FCM configured). Live WebSocket updates stay active.`,
    };
  }
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
