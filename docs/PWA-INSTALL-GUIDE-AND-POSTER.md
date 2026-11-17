# CollegeBits — Student Install Guide, Canteen Poster & Real-Device PWA Checklist

---

## 1. Student Guide: How to Install CollegeBits on iPhone and Android

**Zero App Store download needed · Free · Takes 15 seconds**

### On iPhone & iPad (iOS 16.4 or newer)
> Important: Open the link in **Safari** (not inside WhatsApp, Instagram, or Chrome) so iPhone lets you add the app and enable order alerts.

1. Open **Safari** and go to your canteen link: `https://canteen.your-college.edu/customer/`
2. Tap the **Share** button (the square with an arrow pointing up at the bottom of Safari).
3. Scroll down and tap **Add to Home Screen**.
4. Confirm the name **CollegeBits** and tap **Add** (top-right).
5. Open **CollegeBits** from your Home Screen icon.
6. Sign in, tap **Enable push alerts** under your Account section, and tap **Allow** so your iPhone notifies you when food is ready for pickup.

### On Android Phone
1. Open **Google Chrome** and go to: `https://canteen.your-college.edu/customer/`
2. Tap **Install app** on the green banner (or tap Chrome's **⋮ menu** -> **Install app** / **Add to Home screen**).
3. Confirm **Install**.
4. Open **CollegeBits** from your home screen / app drawer, sign in, and tap **Enable push alerts** -> **Allow** for live kitchen & ready-for-pickup alerts.
5. *(Optional)* If your canteen distributes the signed `.apk`, you can also install it directly via the APK link shown in the app menu.

---

## 2. Printable Canteen Poster Copy (Place Near Counter & Entrance)

> Print at A4/A3 size and replace `https://canteen.your-college.edu/customer/` with a QR code pointing to your live domain (you can generate the QR SVG/PNG cleanly from Admin/Staff portal or any offline QR generator pointing to `https://canteen.your-college.edu/customer/`).

---
### **SKIP THE QUEUE — ORDER AHEAD WITH COLLEGEBITS!**
Scan the QR code with your phone camera to open the canteen menu in seconds — **no app store download required.**

```
        [  INSERT PRINTED QR CODE POINTING TO  ]
        [ https://canteen.your-college.edu/customer/ ]
```

**On iPhone (Safari):**
1. Scan QR -> open in **Safari**
2. Tap **Share** -> **Add to Home Screen** -> **Add**
3. Open **CollegeBits** from your Home Screen for full-screen ordering & pickup alerts

**On Android (Chrome):**
1. Scan QR -> open in **Chrome**
2. Tap **Install app** (or **⋮ Menu -> Install app**)

**At the Counter:**
Order online -> Wait for **"Ready for pickup"** alert -> Show your bright **Pickup QR** at your assigned counter -> Collect & go!

*Note: All confirmed digital payments are final (strict no-refunds policy; counter staff assist with kitchen issues).*
---

## 3. Manual Real-Device Verification Checklist (iPhone & Android)

Run every step on one **real physical iPhone (Safari, iOS 16.4+)** and one **real physical Android phone (Chrome)** over HTTPS before opening campus-wide:

| # | Step to Perform | Expected Result on Real Phone | iPhone Pass? | Android Pass? |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Open `/customer/` in mobile browser** | Fast page load; install prompt banner visible (`Share -> Add to Home Screen` on iPhone Safari; `Install app` on Android Chrome). | [ ] | [ ] |
| 2 | **Install to Home Screen** | App icon named **CollegeBits** appears cleanly (no transparent black box on iPhone); splash screen matches theme `#f8f6f0` / `#12372a`. | [ ] | [ ] |
| 3 | **Launch from Home Screen icon** | Opens full-screen (`standalone`) with no browser address bar, no horizontal overflow, bottom navigation bar (`Menu/Cart/Orders/Account`) sits above home indicator, install banner stays hidden. | [ ] | [ ] |
| 4 | **Log in & browse catalogue** | Tapping inputs (email/password/search) does **not** zoom screen (`16px` font), buttons feel easy to tap (`>= 44px`). | [ ] | [ ] |
| 5 | **Enable Push Notifications (by tap)** | No auto-popup on launch. Tapping **Enable push alerts** triggers OS permission prompt; after tapping **Allow**, shows enabled confirmation. | [ ] | [ ] |
| 6 | **Place an order** | Select item -> Cart badge updates -> complete checkout -> Order appears in **Orders** with Token #. | [ ] | [ ] |
| 7 | **Receive order alerts** | Staff marks **Accept** and **Ready**: phone receives push notification (or live toast / WS alert when open) and tapping notification opens `/customer/#my-orders-section`. | [ ] | [ ] |
| 8 | **Show Pickup QR at counter** | Tapping **Show Short-Lived Pickup QR** (or **Show large bright QR**) shows crisp black-on-white QR code + 6-char manual code + countdown; counter scanner reads it easily and pickup marks single-use `COLLECTED`. | [ ] | [ ] |
| 9 | **Go offline & reload** | Enable Airplane Mode -> reopen or refresh app -> friendly `/offline.html` screen (`You are offline`) appears immediately; no stale/faked checkout or pickup QR cached. | [ ] | [ ] |
| 10 | **Reconnect & reopen** | Disable Airplane Mode -> tap **Retry connection** -> live catalogue loads, header pill returns to `Live updates`, orders stay accurate. | [ ] | [ ] |
