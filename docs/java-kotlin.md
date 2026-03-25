# Java/Kotlin Integration

Detects Java and Kotlin projects by build system markers, IDE metadata,
and filesystem conventions. Java and Kotlin are treated as a single `"java"`
language registration since jdtls handles both natively.

## Build System Detection

Marker files checked in priority order (first match wins):

| Priority | Build System | Markers |
|----------|-------------|---------|
| 1 | Gradle | `settings.gradle`, `settings.gradle.kts`, `build.gradle`, `build.gradle.kts` |
| 2 | Maven | `pom.xml` |
| 3 | Ant | `build.xml` |
| 4 | Eclipse | `.classpath` (only if no other build system found) |

If no build markers are found but IDE metadata indicates a Java project
(e.g., `.idea/misc.xml` with a JavaSDK), detection still succeeds with
`build_system="unknown"`.

## Module Discovery

### Gradle

Modules are parsed from `settings.gradle` or `settings.gradle.kts` by
matching `include("module-name")` declarations (both Kotlin DSL and Groovy
syntax). Colon-separated names like `:sub:module` are converted to
directory paths (`sub/module`). Only directories that exist on disk are
included.

Falls back to `.idea/gradle.xml` `<GradleProjectSettings>` module list
if no settings file is found.

### Maven

Modules are parsed from `pom.xml` `<modules><module>` elements. Handles
both namespaced (`xmlns="http://maven.apache.org/POM/4.0.0"`) and
non-namespaced POM files. Only directories that exist on disk are included.

### Gradle Multi-Module Root

For submodule directories, the detector walks up to 5 levels looking for
a `settings.gradle` or `settings.gradle.kts` to find the multi-module
root. If found above the given project path, `build_info.project_root`
is set to the root directory.

## Source Root Detection

For each discovered module (and the project root), the detector checks
for conventional source directories on disk:

| Category | Directories |
|----------|------------|
| Main sources | `src/main/java`, `src/main/kotlin` |
| Test sources | `src/test/java`, `src/test/kotlin` |
| Resources | `src/main/resources`, `src/test/resources` |

Only directories that actually exist are reported.

### Generated Source Roots

If JetBrains `.idea/` metadata is present, `<sourceFolder>` entries from
`.iml` files are extracted separately as `generated_source_roots`. These
are typically annotation processor output directories
(e.g., `build/generated/sources/annotationProcessor/java/main`).

## Kotlin Detection

Kotlin presence is detected from two sources:

1. **IDE metadata** (tier 3): `.idea/kotlinc.xml` exists with a `version`
   or `languageVersion` option
2. **Filesystem** (tier 5): `.kt` or `.kts` files found under `src/` with
   a bounded depth scan (max 3 levels)

If Kotlin is detected, `details.kotlin_detected = true`.

## IDE Metadata

### JetBrains (.idea/)

| File | Tier | Fields Extracted |
|------|------|-----------------|
| `compiler.xml` | 2 (build-sync) | `bytecode_target` from `<bytecodeTargetLevel target="...">` |
| `gradle.xml` | 2 (build-sync) | `gradle_jvm`, `gradle_home`, module list |
| `modules.xml` + `*.iml` | 2 (build-sync) | `source_roots` (non-test `<sourceFolder>` entries) |
| `misc.xml` | 3 (project) | `java_sdk` (`project-jdk-name`), `java_language_level` (`languageLevel`) |
| `kotlinc.xml` | 3 (project) | `kotlin_version`, `kotlin_api_version` |

### Eclipse

| File | Tier | Fields Extracted |
|------|------|-----------------|
| `.classpath` | 4 (workspace) | `source_roots` (kind=src), `java_sdk` (from JRE_CONTAINER path) |
| `.settings/org.eclipse.jdt.core.prefs` | 4 (workspace) | `java_language_level` (compiler.compliance) |

### VS Code

| File | Tier | Fields Extracted |
|------|------|-----------------|
| `.vscode/settings.json` | 4 (workspace) | `java_sdk` (from `java.jdt.ls.java.home` or `java.configuration.runtimes`) |

## Credibility Hierarchy

When multiple sources provide the same fact, the highest-tier value wins:

| Tier | Source | Example |
|------|--------|---------|
| 1 | Build config | Module list from `settings.gradle` |
| 2 | IDE build-sync | `bytecode_target` from `compiler.xml` |
| 3 | IDE project | `java_sdk` from `misc.xml` |
| 4 | IDE workspace | `java_language_level` from Eclipse `.settings/` |
| 5 | Filesystem | Source roots from `src/main/java` directory existence |

Each field in `details` includes `_source` and `_tier` suffixed keys
for provenance tracking (e.g., `bytecode_target_source: "compiler.xml"`,
`bytecode_target_tier: 2`).

## jdtls Indexing and Readiness

jdtls has a multi-phase startup: import (Gradle/Maven sync), build, search
(indexing), then ongoing validation and diagnostics. The `JdtlsNormalizer`
tracks this lifecycle and marks the server ready only when all three
conditions are met:

1. **ServiceReady** — `language/status` notification with `"ServiceReady"` received
2. **Searching seen** — a `"Searching"` progress task has begun (indicates indexing started)
3. **No active progress tokens** — all progress tasks have completed

This prevents premature query dispatch. On a cold start (no cached index),
the full cycle typically takes 3–6 minutes for large projects. On warm start
(cached index), readiness is reached in seconds.

Single-file queries (definition, hover, document symbols) wait only for LSP
initialization, not full indexing. Cross-file queries (references, call/type
hierarchy, diagnostics) wait for the normalizer to report readiness, with
the timeout extending automatically as long as progress is being made.

The default warmup timeout for jdtls is 300 seconds (5 minutes). For very
large monorepos, increase `LSP_MCP_READY_TIMEOUT` in the MCP server
environment.

## Detection Output

```python
DetectedLanguage(
    language="java",
    build_system="gradle",       # or "maven", "ant", "eclipse", "unknown"
    confidence="high",
    build_info={
        "project_root": "/path/to/root"  # only if different from project_path
    },
    details={
        # Modules
        "gradle_modules": ["/path/to/root", "/path/to/root/module-a", ...],
        "gradle_modules_source": "settings.gradle",
        # or for Maven:
        # "maven_modules": [...],
        # "maven_modules_source": "pom.xml",

        # Source roots (from filesystem scan)
        "source_roots": ["/path/to/root/module-a/src/main/java", ...],
        "test_source_roots": ["/path/to/root/module-a/src/test/java", ...],
        "resource_roots": ["/path/to/root/module-a/src/main/resources", ...],
        "generated_source_roots": [".../build/generated/sources/...", ...],

        # Java config (from IDE metadata)
        "java_sdk": "azul-17",
        "java_language_level": "JDK_17",
        "bytecode_target": "17",

        # Kotlin
        "kotlin_detected": True,
        "kotlin_version": "1.9.23",

        # Gradle-specific
        "gradle_jvm": "azul-17",
    }
)
```
