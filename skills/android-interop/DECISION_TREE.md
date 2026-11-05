# Android App Interoperability Decision Tree

- **Step 1: Split APK Assessment**
  - **Probe command**:
    ```bash
    unzip -Z1 <apk_or_apks> 2>/dev/null | grep -E '\.apk$' || find $(dirname <apk_or_apks>) -name "*.apk" | wc -l
    ```
  - **Branches**:
    - **Multiple APKs detected (>1)**:
      - **Action**: Merge split APKs into a single monolithic APK using `apktool` or install as split via `adb install-multiple`.
      - **Tool path**:
        ```bash
        adb install-multiple -r base.apk split_config.*.apk
        # Or merge with an APK editor/merger tool
        ```
      - **Artifact produced**: Merged standalone APK (`merged.apk`).
    - **Single monolithic APK**:
      - **Action**: Proceed directly to DEX deobfuscation and shelling triage.
      - **Artifact produced**: Target APK (`target.apk`).

- **Step 2: DEX Shelling and Packer Triage**
  - **Probe command**:
    ```bash
    jadx -d /tmp/jadx_probe --no-res <apk> >/dev/null 2>&1 && find /tmp/jadx_probe -name "*.java" | wc -l
    ```
  - **Branches**:
    - **Java file count < 100**:
      - **Verdict**: APK is shelled or packed (e.g., Qihoo 360, SecShell, Bangcle, DexGuard enterprise stub).
      - **Action**: Branch to Phase 3B memory dumping.
      - **Tool path**:
        ```bash
        frida -U -f <pkg> -l work/dynamic/artifact_recovery.js --no-pause
        adb pull /sdcard/recovered/ work/static/recovered/
        for d in work/static/recovered/*.dex; do jadx -d "work/static/jadx_recovered/$(basename $d .dex)" "$d"; done
        ```
      - **Artifact produced**: Dumped raw DEX files in `work/static/recovered/*.dex` and decompiled sources in `work/static/jadx_recovered/`.
    - **Java file count >= 100**:
      - **Verdict**: Unpacked standard DEX structure. Proceed to framework fingerprinting.
      - **Artifact produced**: Decompiled source tree in `work/static/jadx/`.

- **Step 3: Framework Fingerprinting**
  - **Probe command**:
    ```bash
    unzip -Z1 <apk> | grep -E 'lib(flutter|hermes|konyjsvm|reactnativejni)\.so'
    ```
  - **Branches**:
    - **Output contains `libkonyjsvm.so`**:
      - **Verdict**: Kony / Temenos Quantum application. Business logic lives in compiled JavaScript bundles (`startup.js` or `startup.js.mp3`), wrapped by a thin native VM and standard Android Java harness.
      - **Tool path**: Check bundle encryption (Step 4), then use JVM transport observation hooks.
      - **Artifact produced**: `kony_framework_verdict.json`.
    - **Output contains `libflutter.so`**:
      - **Verdict**: Flutter application. Business logic compiled to native AOT machine code (`libapp.so`). Dart runtime does not use Java TLS stack or system CA trust store.
      - **Tool path**: `blutter` + System Proxy / Mitmproxy with patched SSL verification in Dart engine.
      ```bash
      unzip -p <apk> 'lib/arm64-v8a/libapp.so' > /tmp/libapp.so
      blutter /tmp/libapp.so /tmp/blutter_out
      frida -U -f <pkg> -l /tmp/blutter_out/frida/*.js --no-pause
      ```
      - **Artifact produced**: Dumped Dart object pool (`pp.txt`) and native function offset table.
    - **Output contains `libhermes.so` or `libreactnativejni.so`**:
      - **Verdict**: React Native application (Hermes or JavaScriptCore).
      - **Tool path**: Extract bundle from `assets/index.android.bundle` and disassemble via `hermes-dec`.
      ```bash
      unzip -p <apk> assets/index.android.bundle > /tmp/index.bundle
      npx hermes-dec /tmp/index.bundle /tmp/bundle_decompiled.js
      ```
      - **Artifact produced**: Decompiled React Native JavaScript (`bundle_decompiled.js`).
    - **No framework shared library detected**:
      - **Verdict**: Standard JVM / Android native application (Java/Kotlin).
      - **Tool path**: Static Retrofit / OkHttp annotation scanning followed by standard OkHttp/HttpURLConnection Frida hooks.
      - **Artifact produced**: Extracted static routes (`interface_contract.json`).

- **Step 4: Kony / Asset Bundle Encryption Assessment**
  - **Probe command**:
    ```bash
    unzip -p <apk> 'assets/*.mp3' 2>/dev/null | head -c 16 | xxd -p
    ```
  - **Branches**:
    - **Header matches ASCII text or standard ZIP/PK magic (`504b0304`)**:
      - **Verdict**: Plaintext bundle or standard compressed zip renamed to `.mp3` for asset compression bypass.
      - **Action**: Extract directly and run static string and regex analysis.
      - **Tool path**:
        ```bash
        unzip -q <apk> 'assets/*.js*' -d /tmp/kony_assets/
        rg -oN 'https?://[a-zA-Z0-9./_?=&-]+' /tmp/kony_assets/
        ```
      - **Artifact produced**: Extracted plaintext JavaScript routes in `work/contract/urls_raw.txt`.
    - **Header contains high-entropy binary bytes (non-ASCII, non-ZIP magic)**:
      - **Verdict**: Encrypted Kony JavaScript bundle (`startup.js.mp3`). Static recovery is unviable because decryption keys are computed inside `libkonyjsvm.so` at runtime.
      - **Action**: Abort static bundle extraction. Rely on dynamic JVM transport observation and native JS VM string memory extraction.
      - **Tool path**: Run Frida JVM transport interceptors (`transport_observe.js`) during application execution.
      - **Artifact produced**: Runtime exchange captures in `work/logs/exchanges.jsonl`.

- **Step 5: Cryptography & Pinning Delegation Probe**
  - **Probe command**:
    ```bash
    unzip -p <apk> 'assets/application.properties' 2>/dev/null | grep -E '(UseCryptoLibrary|NetworkTrustConfig)'
    ```
  - **Branches**:
    - **Output contains `NetworkTrustConfig=Allow Bundled` (or custom pinning flag)**:
      - **Verdict**: Hardcoded certificate pinning enforced against assets certificates (e.g., `assets/certs/`). Adding a CA to Android System / User trust store has zero effect.
      - **Action**: Do not rely on external proxy CA. Hook the JVM networking stack or native trust manager directly before certificate validation takes place.
      - **Tool path**: Load `work/dynamic/transport_shim.js` with `TrustManagerImpl` and `CertificatePinner` neutralizers.
      - **Artifact produced**: Active SSL unpinning log output confirming bypassed handshakes.
    - **Output indicates standard OS trust / no custom pinning**:
      - **Verdict**: Standard TLS validation.
      - **Action**: Use Magisk trust cert module or mitmproxy CA in system store.
      - **Tool path**: Standard proxy configuration via `adb shell settings put global http_proxy <ip>:<port>`.
      - **Artifact produced**: Proxy flow captures.

- **Step 6: Runtime HTTP Client Stack Identification**
  - **Probe command**:
    ```bash
    frida -U -f <pkg> --no-pause -e '
    Java.perform(function() {
      var candidates = [
        "okhttp3.OkHttpClient",
        "com.android.okhttp.OkUrlFactory",
        "org.apache.http.impl.client.DefaultHttpClient",
        "org.chromium.net.CronetEngine"
      ];
      candidates.forEach(function(c) {
        try { Java.use(c); console.log("LOADED: " + c); } catch(e) {}
      });
    });'
    ```
  - **Branches**:
    - **`LOADED: okhttp3.OkHttpClient`**:
      - **Verdict**: OkHttp 3.x/4.x stack.
      - **Tool path**: Hook `okhttp3.OkHttpClient$Builder.build()` to inject `NetworkInterceptor`.
      - **Artifact produced**: `work/logs/exchanges.jsonl` with full headers, verb, URL, and body.
    - **`LOADED: com.android.okhttp.OkUrlFactory` or bare `java.net.HttpURLConnection`**:
      - **Verdict**: Classic Android HTTP URL Connection wrapper (common in older Kony runtime wrappers).
      - **Tool path**: Hook `java.net.URL.openConnection()` and wrap `InputStream`/`OutputStream`.
      - **Artifact produced**: `work/logs/exchanges.jsonl`.
    - **`LOADED: org.chromium.net.CronetEngine`**:
      - **Verdict**: Google Cronet networking stack.
      - **Tool path**: Hook `org.chromium.net.UrlRequest$Builder` or intercept native BoringSSL calls.
      - **Artifact produced**: `work/logs/exchanges.jsonl`.
    - **No Java HTTP class loaded but network traffic occurs**:
      - **Verdict**: Native networking (`libcurl.so`, native sockets, or custom C++ HTTP engine).
      - **Tool path**: Native Frida hook on `libc.so` (`connect`, `send`, `recv`, `SSL_write`, `SSL_read`).
      - **Artifact produced**: Native packet/payload logs.

- **Step 7: Surface Reconciliation & External Replay**
  - **Probe command**:
    ```bash
    python3 scripts/reconcile_surfaces.py \
      --static work/contract/interface_contract.json \
      --dynamic work/logs/exchanges.jsonl \
      --out work/contract/surface_report.json
    ```
  - **Branches**:
    - **`runtime_only` entries detected**:
      - **Verdict**: Target endpoints are dynamically formed (reflection, encrypted JS bundle, or runtime concatenation).
      - **Action**: Feed `runtime_only` entries into `gen_harness.py`. Replay outside device with Python `requests` or `curl`.
      - **Tool path**:
        ```bash
        python3 scripts/gen_harness.py
        python3 work/repro/replay.py --url "<target_substring>"
        ```
      - **Artifact produced**: HTTP 200 verification recorded in `work/repro/replay_success.log`.
