// 注入到每个页面（document 创建前执行）。
// 拦截网页通知 API：网页想弹桌面通知时，把标题/正文转交给 Python（window.__relayNotify）。
(() => {
  if (window.__relayHooked) return;
  window.__relayHooked = true;

  const relay = (title, options) => {
    try {
      const o = options || {};
      if (typeof window.__relayNotify === "function") {
        window.__relayNotify({
          title: String(title || ""),
          body: String(o.body || ""),
          tag: String(o.tag || ""),
        });
      }
    } catch (e) {}
  };

  // 1) new Notification(title, options)
  const Orig = window.Notification;
  function HookedNotification(title, options) {
    relay(title, options);
    // 返回一个“假”通知对象，满足页面对 onclick/close 等的调用
    const fake = new EventTarget();
    fake.title = title;
    fake.body = (options && options.body) || "";
    fake.close = () => {};
    return fake;
  }
  HookedNotification.requestPermission = (cb) => {
    if (typeof cb === "function") cb("granted");
    return Promise.resolve("granted");
  };
  Object.defineProperty(HookedNotification, "permission", { get: () => "granted" });
  HookedNotification.maxActions = (Orig && Orig.maxActions) || 2;
  window.Notification = HookedNotification;

  // 2) serviceWorkerRegistration.showNotification(title, options)（页面上下文发起的）
  if (window.ServiceWorkerRegistration) {
    const origShow = ServiceWorkerRegistration.prototype.showNotification;
    ServiceWorkerRegistration.prototype.showNotification = function (title, options) {
      relay(title, options);
      return Promise.resolve();
    };
    ServiceWorkerRegistration.prototype.__origShowNotification = origShow;
  }

  // 3) 有些站点先查 permissions API
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) =>
      desc && desc.name === "notifications"
        ? Promise.resolve({ state: "granted", onchange: null })
        : origQuery(desc);
  }
})();
