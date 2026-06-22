// WorkReport — Service worker registration + Web Push subscription helper.
// Included on every logged-in page. Registers the service worker (for PWA
// installability/offline) and, if the browser supports Push, quietly tries
// to keep the current device subscribed so the user receives notifications
// for job assignments, report/TA approvals, and edit-request reviews.

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

async function wrSubscribeToPush(registration) {
  try {
    if (!("PushManager" in window)) return;

    // Only proceed if the user has already granted notification permission.
    // We never call Notification.requestPermission() automatically here —
    // that must happen from an explicit user tap (see wrEnableNotifications),
    // since browsers ignore/penalize permission prompts triggered on page load.
    if (Notification.permission !== "granted") return;

    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const keyRes = await fetch("/push/vapid-public-key");
      if (!keyRes.ok) return;
      const { publicKey } = await keyRes.json();
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    }

    await fetch("/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription),
    });
  } catch (e) {
    // Non-fatal: notifications are a nice-to-have, never block the app.
  }
}

// Call this from a button's onclick to ask the user for permission and
// subscribe this device. Must be triggered by a real user gesture (tap).
async function wrEnableNotifications() {
  try {
    if (!("Notification" in window) || !("serviceWorker" in navigator)) {
      alert("Push notifications aren't supported on this browser.");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return;
    const registration = await navigator.serviceWorker.ready;
    await wrSubscribeToPush(registration);
    const btn = document.getElementById("wr-enable-push-btn");
    if (btn) {
      btn.textContent = "🔔 Notifications on";
      btn.disabled = true;
    }
  } catch (e) {
    // ignore
  }
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/static/sw.js")
      .then((registration) => {
        // If permission was already granted in a past visit, keep the
        // subscription alive silently (e.g. after the SW updates).
        wrSubscribeToPush(registration);
      })
      .catch(() => {});
  });
}
