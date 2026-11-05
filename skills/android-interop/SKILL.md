---
name: android-interop
description: Decision patterns, diagnostic probes, and failure modes for reverse engineering and interoperability analysis of Android banking client apps across hybrid frameworks (Kony/Temenos, Flutter, React Native) and JVM.
---

# Android Application Interoperability Skill

This skill provides the operational playbook, diagnostic probes, and pitfall analyses for reverse engineering and reproducing network contracts of Android applications on rooted devices.

## Core Documents

1. **[Decision Tree](DECISION_TREE.md)**:
   Step-by-step diagnostic tree from raw APK to standalone external HTTP replay.
   - Split APK triage
   - DEX shelling / packer recovery
   - Framework classification (Kony / Flutter / React Native / Standard JVM)
   - Asset bundle encryption validation
   - Cryptographic delegation and pinning verification
   - Runtime HTTP client identification

2. **[Diagnostic Probes](PROBES.md)**:
   Exact copy-paste runnable macOS / Android CLI commands.
   - APK identity & manifest triage
   - File count vs file size DEX shelling checks
   - Framework native library and asset fingerprinting
   - Runtime class loading verification via Frida
   - Magic bytes and Shannon entropy inspection
   - Property file parsing (`application.properties`)
   - Device connectivity and Magisk root hiding verification

3. **[Pitfalls & Countermeasures](PITFALLS.md)**:
   Key failure modes identified in enterprise banking engagements and their actionable mitigations.
   - Retrofit annotation assumptions on hybrid frameworks
   - Resource string heuristics vs actual runtime class resolution
   - Root privilege vs Magisk DenyList + Shamiko requirements
   - Encrypted JavaScript bundles (`startup.js.mp3`)
   - Hardcoded certificate pinning (`NetworkTrustConfig: Allow Bundled`)
   - High Java class counts in hybrid boilerplate
   - Monolithic Frida scripts vs modular hooks
