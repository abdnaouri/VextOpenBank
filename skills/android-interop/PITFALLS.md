# Android Interoperability Engineering Pitfalls

## 1. Assuming Retrofit Annotations Exist

- **The Mistake**: Running `@GET`, `@POST`, `@Path` regex scans against the decompiled Java codebase expecting structured API declarations because the target is an enterprise banking app.
- **The Reality**: Enterprise banking apps heavily utilize cross-platform enterprise frameworks (Kony/Temenos, Cordova, Flutter, Xamarin). In Kony, API calls are defined inside JavaScript controllers invoking Kony SDK service endpoints (`kony.net.HttpRequest` or Kony Fabric SDK), bypassing Retrofit entirely.
- **What to do instead**:
  - Run the framework detection probe (`libkonyjsvm.so`, `libflutter.so`, `libhermes.so`) before writing or executing any Java annotation extractors.
  - If a hybrid framework is detected, redirect static discovery to the framework's native asset/bundle directories rather than decompiled Java source folders.

---

## 2. Relying on Resource String Heuristics for the HTTP Stack

- **The Mistake**: Trusting automated triage scripts that declare `http_stack_hint: OkHttp` based on grep matches for `"okhttp"` inside the APK.
- **The Reality**: Modern SDKs (Google Play Services, Firebase, advertising libraries) bundle OkHttp internally. The core application logic may actually use `HttpURLConnection`, Apache HTTP client, or native C++ sockets (`libcurl`).
- **What to do instead**:
  - Verify runtime class loading using dynamic probes before writing interceptors:
    ```bash
    frida -U -f <pkg> --no-pause -e '
    Java.perform(function() {
      ["okhttp3.OkHttpClient", "com.android.okhttp.OkUrlFactory", "java.net.HttpURLConnection"].forEach(function(c) {
        try { Java.use(c); console.log("FOUND: " + c); } catch(e) {}
      });
    });'
    ```
  - Trace the real network caller via thread call stacks on socket creation or TLS handshakes.

---

## 3. Assuming Root Access Means Frida Will Attach

- **The Mistake**: Assuming that having `uid=0` via `su` on a rooted test phone is sufficient to spawn and instrument the target app.
- **The Reality**: Banking apps implement aggressive anti-root and anti-Frida checks (checking for su paths, test-keys, Magisk sockets, ptrace detection, and namespace inspection). The app crashes or terminates before instrumentation hooks execute.
- **What to do instead**:
  - Configure device-side root isolation **before** attempting dynamic analysis:
    1. In Magisk Manager, enable **Zygisk**.
    2. Add the target `<pkg>` to the **DenyList** (selecting all processes).
    3. Install the **Shamiko** Magisk module to hide root traces at kernel/native levels.
    4. Verify via adb:
       ```bash
       adb shell su -c 'ls /data/adb/modules/shamiko'
       ```

---

## 4. Expecting Plaintext JavaScript Bundles from Apktool

- **The Mistake**: Decompiling with `apktool d` and attempting to inspect `assets/startup.js.mp3` or other JS bundles as plain JavaScript source code.
- **The Reality**: Kony/Temenos encrypts the JavaScript bundle during the build. The decryption key is generated dynamically inside `libkonyjsvm.so` at native runtime. Static disassembly of the `.mp3` file yields high-entropy ciphertext.
- **What to do instead**:
  - Inspect the first 16 bytes:
    ```bash
    unzip -p <apk> 'assets/*.mp3' | head -c 16 | xxd -p
    ```
  - If non-ASCII, stop spending time attempting static decryption. Rely on:
    1. Dynamic JVM transport observation (`work/dynamic/transport_observe.js`), capturing requests after the JS VM dispatches them to the Java network stack.
    2. Memory dumping of plaintext JavaScript strings from the heap of `libkonyjsvm.so` via Frida memory scanning.

---

## 5. Misinterpreting `NetworkTrustConfig: Allow Bundled`

- **The Mistake**: Assuming `NetworkTrustConfig: Allow Bundled` in `application.properties` allows system-installed user CAs or proxy certs (mitmproxy/Burp).
- **The Reality**: `Allow Bundled` means the Kony runtime *exclusively* trusts certificate authorities packaged inside the app's `assets/certs/` folder. System trust stores and user CA stores installed in Android settings are ignored.
- **What to do instead**:
  - Do not waste time debugging Android CA certificate installation.
  - Hook the TrustManager and HostnameVerifier in memory directly using `work/dynamic/transport_shim.js`:
    ```javascript
    var TrustManagerImpl = Java.use('com.android.org.conscrypt.TrustManagerImpl');
    TrustManagerImpl.verifyChain.implementation = function(untrusted, trustAnchorChain, host, clientAuth, ocspData, tlsSctData) {
        return untrusted;
    };
    ```

---

## 6. Equating Decompiled Java File Count to App Logic

- **The Mistake**: Assuming that because `jadx` produced 19,874 Java files, the entire business logic and API integration must be reverse-engineered from Java source code.
- **The Reality**: In hybrid applications (Kony, Cordova, React Native), 98% of the decompiled Java code consists of boilerplate framework runtime, native bridge bindings, UI widget rendering stubs, and third-party support SDKs. The actual business logic resides entirely in the JavaScript VM.
- **What to do instead**:
  - Do not audit 20,000 Java classes looking for business rules.
  - Treat the Java layer purely as a transport pipe: hook where the framework hands off the serialized HTTP request to Java networking libraries (`OkHttpClient`, `HttpURLConnection`).

---

## 7. Writing Monolithic Frida Scripts Before Verifying Target Classes

- **The Mistake**: Writing comprehensive 800-line Frida scripts hooking 40 different crypto, SSL, and network classes before validating which ones exist in the active runtime.
- **The Reality**: When any single unhandled class fails to resolve inside `Java.use()` during initialization, the entire Frida script crashes and aborts execution before any hooks activate.
- **What to do instead**:
  - Keep observation scripts modular and isolated into separate files:
    - `compat_environment.js` (environment only)
    - `transport_shim.js` (unpinning only)
    - `transport_observe.js` (traffic logging only)
  - Always wrap individual class hooks in isolated `try/catch` blocks so that missing classes fail gracefully without preventing other hooks from attaching.
  - Validate candidate classes with a one-line throwaway probe before writing multi-method hooks.
