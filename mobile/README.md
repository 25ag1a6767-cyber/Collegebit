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

## 3. Push Notification Status (Mobile vs PWA)
- **Customer PWA (`/customer/`):** Supports full background Web Push via VAPID keys (`VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`) + `pywebpush` + Service Worker (`frontend/sw.js`), with WebSocket & in-app toast fallback.
- **Native Expo App (`mobile/`):** Declares `POST_NOTIFICATIONS` on Android and maintains real-time order updates while open via authenticated WebSocket (`/api/v1/ws`) + `/api/v1/users/me/notifications` inbox. Closed-app push notifications in standalone Android APK / iOS IPA require adding `expo-notifications` + FCM (`google-services.json`) / APNs credentials on top of this build.

## 4. Security Architecture
- **Zero embedded secrets:** Razorpay keys and AI tokens reside strictly on the backend.
- **Hardware-backed keystore:** Short-lived JWTs and refresh tokens are stored in Android Keystore / iOS Keychain via `expo-secure-store`.
- **Single-use pickup pass:** QR codes and backup codes are signed server-side and invalidated atomically upon counter handover.
