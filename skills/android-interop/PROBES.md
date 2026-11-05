# Android App Interoperability Diagnostic Probes

## 1. APK Identity and Split Detection

### Probe: Enumerate APK Package Name and Entry Activities
```bash
aapt2 dump badging <apk> | grep -E "^package:|^launchable-activity:"
```
- **Meaning**: Outputs the target application package name, version code, and launchable activity class.
- **Misleading failure mode**: In split APK configurations, querying a split config APK rather than `base.apk` returns incomplete metadata or an error.

### Probe: Detect Split APK Archives in Directory
```bash
find $(dirname <apk>) -maxdepth 1 -name "*.apk" | wc -l
```
- **Meaning**: Counts how many APK files exist alongside the target; count > 1 indicates an Android App Bundle (split install).
- **Misleading failure mode**: If multiple independent versions or unmerged backups are placed in the same folder, this yields a false positive split count.

### Probe: Inspect Compressed ZIP Entries for Nested APKs
```bash
unzip -Z1 <apk> | grep -E '\.apk$'
```
- **Meaning**: Detects APKs bundled or nested inside another container APK.
- **Misleading failure mode**: Shell packers occasionally store encrypted payload files with `.apk` extensions that are not valid ZIP files.

---

## 2. DEX Shelling and Packer Detection

### Probe: Count Decompiled Java Source Files
```bash
jadx -d /tmp/jadx_probe --no-res <apk> >/dev/null 2>&1 && find /tmp/jadx_probe -name "*.java" | wc -l
```
- **Meaning**: Quantifies total decompilable source classes; count < 100 indicates a shell stub where real code is encrypted/hidden.
- **Misleading failure mode**: Apps embedding heavy SDKs (advertising, analytics, crash reporting) can generate > 1,000 Java files even when the main business logic DEX is packed.

### Probe: Scan for Known Packer Native Libraries
```bash
unzip -Z1 <apk> | grep -iE 'lib(jiagu|shella|mobisec|nesec|execmain|DexHelper|secsdk)\.so'
```
- **Meaning**: Identifies signature native libraries belonging to commercial Chinese and enterprise packers (Qihoo, Tencent Legu, SecShell, Bangcle).
- **Misleading failure mode**: In-house or custom enterprise stub loaders name their binaries innocuously (e.g., `libsecurity.so` or `libcore.so`), avoiding known grep patterns.

### Probe: Check for Application Proxy Stub in Manifest
```bash
unzip -p <apk> AndroidManifest.xml | strings | grep -iE '(StubApp|ProxyApplication|StubShell)'
```
- **Meaning**: Detects whether the `<application>` tag points to a known dynamic DEX-loading proxy class.
- **Misleading failure mode**: Decompiled binary XML strings may show remnants from old build configurations or unused library manifests.

---

## 3. Framework Fingerprinting

### Probe: Scan Native Libraries for Hybrid Framework VM Engines
```bash
unzip -Z1 <apk> | grep -E 'lib(flutter|hermes|konyjsvm|reactnativejni|mono|xamarin)\.so'
```
- **Meaning**: Directly identifies runtime architecture (Flutter, React Native Hermes/JSC, Kony/Temenos Quantum, or Xamarin Mono).
- **Misleading failure mode**: Some hybrid apps package third-party SDKs that embed their own mini-engines (e.g., a React Native feedback widget inside a native Java banking app).

### Probe: Enumerate Framework Asset Bundles
```bash
unzip -Z1 <apk> | grep -E 'assets/.*\.(bundle|js|mp3|dart|wasm)'
```
- **Meaning**: Reveals compiled bytecode or script files indicating non-JVM business logic locations.
- **Misleading failure mode**: Kony/Temenos masks executable JavaScript bundles with media extensions (`startup.js.mp3`) to bypass asset compression algorithms during APK packaging.

---

## 4. HTTP Stack Fingerprinting

### Probe: Check Static HTTP Client String References in Decompiled Code
```bash
rg -oN 'Lokhttp3/OkHttpClient|Lorg/apache/http|Lorg/chromium/net/CronetEngine|Ljava/net/HttpURLConnection' work/static/apktool/smali/
```
- **Meaning**: Shows which HTTP client libraries are linked anywhere inside the application's bytecode.
- **Misleading failure mode**: Third-party tracker SDKs often import OkHttp statically, while the bank's core networking uses `HttpURLConnection` or native C++ requests.

### Probe: Test Actual Runtime Class Instantiation via Frida
```bash
frida -U -f <pkg> --no-pause -e '
Java.perform(function() {
  ["okhttp3.OkHttpClient", "com.android.okhttp.OkUrlFactory", "org.chromium.net.CronetEngine", "java.net.HttpURLConnection"].forEach(function(c) {
    try { Java.use(c); console.log("[LOADED] " + c); } catch(e) {}
  });
});'
```
- **Meaning**: Tests whether the Java classloader actually resolves and uses the networking library in memory during process startup.
- **Misleading failure mode**: A class may resolve in `Java.use()` if present in the APK classpath even if the application never creates an instance of it.

### Probe: Hook OkHttp Constructor to Verify In-Use Pipeline
```bash
frida -U -f <pkg> --no-pause -e '
Java.perform(function() {
  try {
    var Builder = Java.use("okhttp3.OkHttpClient$Builder");
    Builder.$init.overload().implementation = function() {
      console.log("[ACTIVE] okhttp3.OkHttpClient$Builder instantiated from: " + Java.use("android.util.Log").getStackTraceString(Java.use("java.lang.Exception").$new()));
      return this.$init();
    };
  } catch(e) { console.log("[INACTIVE] " + e.message); }
});'
```
- **Meaning**: Confirms that an OkHttpClient is constructed at runtime and prints the calling stack trace.
- **Misleading failure mode**: If the app uses a pre-configured singleton initialized before the script attaches, `$init` will not fire unless spawned with `--no-pause`.

---

## 5. Encryption & Asset Obfuscation Detection

### Probe: Inspect Magic Header Bytes of Target Asset
```bash
unzip -p <apk> 'assets/startup.js.mp3' 2>/dev/null | head -c 16 | xxd -p
```
- **Meaning**: Reading the first 16 bytes identifies plain text, standard zip/archive headers (`504b0304`), or proprietary binary ciphertext.
- **Misleading failure mode**: Custom header wrappers may prefix encrypted payloads with pseudo-plaintext headers or ID3 tags.

### Probe: Calculate Shannon Entropy on Extracted Asset
```bash
python3 -c '
import sys, math
data = open(sys.argv[1], "rb").read()
if not data: sys.exit(0)
entropy = -sum((data.count(bytes([b])) / len(data)) * math.log2(data.count(bytes([b])) / len(data)) for b in set(data))
print(f"Entropy: {entropy:.4f} (Max: 8.0000)")
' <extracted_asset_file>
```
- **Meaning**: Entropy > 7.5 indicates compressed or strongly encrypted data; entropy < 6.0 indicates structured plaintext or uncompressed code.
- **Misleading failure mode**: Standard gzip or zlib compression also yields entropy > 7.5 without involving any cryptographic protection.

---

## 6. Configuration File Discovery

### Probe: Discover Configuration and Property Files
```bash
unzip -Z1 <apk> | grep -iE '(application\.properties|buildconfig|assets/config.*|res/raw/.*\.properties|default\.properties)'
```
- **Meaning**: Locates configuration files that define framework runtime flags, environment URLs, and security toggles.
- **Misleading failure mode**: Legacy, unused template configuration files are frequently left behind in the build pipeline and do not reflect runtime settings.

### Probe: Inspect Kony/Temenos Specific Security Toggles
```bash
unzip -p <apk> 'assets/application.properties' 2>/dev/null | grep -E '(UseCryptoLibrary|NetworkTrustConfig|AppID|ServerURL)'
```
- **Meaning**: Extracts Kony platform configuration governing crypto delegation, certificate pinning behavior, and backend gateway endpoints.
- **Misleading failure mode**: If the mobile app initializes through Kony Fabric SDK dynamically, local properties are overridden by remote runtime configuration.

---

## 7. Device Connectivity and Root Verification

### Probe: Verify Device Adb Daemon and Architecture
```bash
adb devices -l && adb shell getprop ro.product.cpu.abi
```
- **Meaning**: Confirms host can talk to the target device and identifies CPU architecture (e.g., `arm64-v8a`) to match Frida server binaries.
- **Misleading failure mode**: Emulators or dual-ABI devices may report `arm64-v8a` while actually running under translation layers (e.g., Houdini on x86_64).

### Probe: Verify Su Privilege and Magisk State
```bash
adb shell "su -c 'id; which su'"
```
- **Meaning**: Verifies that root access is accessible without interactive prompt blocking and confirms `uid=0(root)`.
- **Misleading failure mode**: Su may succeed from interactive shell, while app-spawned Frida agents fail due to Magisk DenyList / Shamiko enforcing namespace isolation.

### Probe: Verify Frida Server Match
```bash
frida --version && adb shell "su -c '/data/local/tmp/frida-server --version'"
```
- **Meaning**: Confirms identical versions between workstation frida CLI and device-side daemon.
- **Misleading failure mode**: Both versions can match while communication fails if SELinux is set to `Enforcing` and blocking socket binding on `127.0.0.1:27042`.
