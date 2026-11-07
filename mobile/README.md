# CampusBite AI v2.0 — Mobile Application for Android & iPhone (iOS)
*(Expo + React Native + TypeScript)*

This mobile client is dedicated for students and customers, connecting to the shared CampusBite backend (`/api/v1` REST API and `/api/v1/ws` WebSocket hub). Menus, real-time stock reservations, UPI payments, formatted pickup tokens, and live order tracking stay 100% synchronized with the kitchen displays.

## 1. Running on Android & iPhone

### Option A: Direct Local Run on iPhone or Android (via Expo Go)
1. Install **Expo Go** from the **Apple App Store** (for iPhone) or **Google Play Store** (for Android).
2. From the repository root, start the mobile dev server:
   ```powershell
   Set-Location mobile
   npm install
   npx expo start
   ```
3. Scan the terminal QR code:
   - **iPhone:** Open the native **Camera** app, tap the QR code banner to open in Expo Go.
   - **Android:** Scan inside the **Expo Go** app.

### Option B: Build Standalone Binaries (EAS Build)
- **Android Standalone APK:**
  ```powershell
  npx eas build --platform android --profile preview
  ```
- **iPhone (iOS) Build / IPA:**
  ```powershell
  npx eas build --platform ios --profile preview
  ```

### Option C: Instant Mobile PWA Installation (Zero App Store Required)
Students can also install the full native-like application on any mobile phone in seconds:
- **On Android (Chrome / Brave / Edge):**
  Open `http://<HOST_IP>:8000/` and tap **"Install App"** (or browser menu ➔ *Install app*).
- **On iPhone (Safari):**
  Open `http://<HOST_IP>:8000/` in Safari, tap the **Share** button (box with upward arrow), and select **"Add to Home Screen"**. It installs with the native icon, full-screen viewport, and zero browser address bar.

---

## 2. Security Architecture
- **Zero embedded secrets:** Razorpay keys and AI tokens reside strictly on the backend.
- **Hardware-backed keystore:** Short-lived JWTs and refresh tokens are stored in Android Keystore / iOS Keychain via `expo-secure-store`.
- **Single-use pickup pass:** QR codes and backup codes are signed server-side and invalidated atomically upon counter handover.
