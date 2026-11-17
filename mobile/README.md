# CollegeBits — Native Mobile App for Android & iPhone (iOS)
*(Expo + React Native + TypeScript — one codebase, both platforms)*

The student app connects to the shared CollegeBits backend (`/api/v1` REST + `/api/v1/ws`). Menus, stock reservations, UPI payments, pickup tokens, and live order tracking stay synchronized with the kitchen.

## 1. Configure API Base URL (Never Hardcoded)

`mobile/src/api/client.ts` and `mobile/app.config.js` read the server URL strictly from config:
1. `EXPO_PUBLIC_API_URL` (via `mobile/.env`, shell environment, or EAS Environment Variables)
2. `app.json` -> `expo.extra.apiBaseUrl` (injected at bundle time by `mobile/app.config.js`)
3. Local development only (`__DEV__`): `http://10.0.2.2:8000` (Android Emulator) or `http://127.0.0.1:8000` (iOS Simulator)

```powershell
cd mobile
Copy-Item .env.example .env
# Edit mobile/.env and set EXPO_PUBLIC_API_URL=https://canteen.your-college.edu
npm install
npm run typecheck
npx expo start
```

## 2. Build Android APK & Bundle with Expo

### Cloud APK via Expo EAS (no Android Studio needed)
```bash
cd mobile
npm install
npx eas login
EXPO_PUBLIC_API_URL=https://canteen.your-college.edu npx eas build --platform android --profile preview
```
- `--profile preview` produces a direct installable `.apk` (`mobile/eas.json`).
- `--profile production` produces a Play Store `.aab`.

### Local Android SDK Build (Windows / macOS / Linux)
```powershell
$env:EXPO_PUBLIC_API_URL = "https://canteen.your-college.edu"
powershell -ExecutionPolicy Bypass -File .\scripts\build_android_apk.ps1
```

## 3. Free Mobile, WhatsApp & SMS Notifications ($0 Cost Options)
- **Web Push PWA (`/customer/`):** 100% free background push on Android Chrome & iPhone Safari 16.4+ (Home Screen PWA). Generate VAPID keys offline in 1 command: `python scripts/generate_vapid_keys.py`.
- **Native Expo App (`mobile/`):** Pop-up local notifications + floating live banners fire automatically via WebSocket (`Notifications.scheduleNotificationAsync`) at $0 even in Expo Go. For closed-app Expo push, attach Firebase `google-services.json` and `EXPO_PUBLIC_EAS_PROJECT_ID` in Expo EAS.
- **Free Phone Push via `ntfy` ($0 iOS & Android):** Students can tap **Enable Free Phone Push (ntfy)** in Profile (PWA or Mobile App) to subscribe to a private topic in the free iOS/Android `ntfy` app — zero developer fees.
- **Free Telegram Bot Alerts ($0 Unlimited):** Set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_BOT_USERNAME` from `@BotFather`; students link their Telegram account in 2 taps.
- **WhatsApp ($0):**
  1. **Zero-setup 1-tap Staff Notify:** On `/staff`, every order with a student phone number has **WhatsApp ($0)** and **SMS ($0)** buttons opening pre-filled `https://wa.me/...` and `sms:...` messages.
  2. **Official Meta WhatsApp Cloud API:** Set `WHATSAPP_CLOUD_ENABLED=true`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_ACCESS_TOKEN` (first 1,000 customer-initiated service messages/month are free from Meta within the 24h window when students message `CANTEEN_WHATSAPP_NUMBER` first).
- **SMS ($0 API Cost via Android SIM Gateway):** Run an open-source Android SMS Gateway app on a counter phone on Wi-Fi using its SIM plan with `SMS_ENABLED=true`, `SMS_WEBHOOK_PRESET=android_sms_gate`, `SMS_WEBHOOK_URL=...`, and `SMS_WEBHOOK_USERNAME` / `SMS_WEBHOOK_PASSWORD`.

## 4. Security Architecture
- **Zero embedded secrets:** Razorpay keys and AI tokens reside strictly on the backend.
- **Hardware-backed keystore:** Short-lived JWTs and refresh tokens are stored in Android Keystore / iOS Keychain via `expo-secure-store`.
- **Single-use pickup pass:** QR codes and backup codes are signed server-side and invalidated atomically upon counter handover.
