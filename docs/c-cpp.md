# C/C++ Integration

Detects C and C++ projects by build system markers and compile database
presence. The language is determined by scanning source file extensions:
C++ extensions (`.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`) → `"cpp"`,
C-only (`.c`, `.h`) → `"c"`. Mixed projects register as `"cpp"`.

## Build System Detection

Marker files checked in priority order (first match wins):

| Priority | Build System | Markers |
|----------|-------------|---------|
| 1 | CMake | `CMakeLists.txt` |
| 2 | Meson | `meson.build` |
| 3 | Autotools | `configure.ac`, `configure.in` |
| 4 | Bazel | `MODULE.bazel`, `WORKSPACE`, `WORKSPACE.bazel` |
| 5 | Make | `Makefile` (only if no `CMakeCache.txt` — avoids misidentifying CMake-generated Makefiles) |

A standalone `compile_commands.json` without any build system marker also
triggers detection with `build_system="unknown"`.

## Compile Database Discovery

The detector searches for `compile_commands.json` in:

1. Project root
2. Common build directories: `build/`, `cmake-build-debug/`,
   `cmake-build-release/`, `cmake-build-relwithdebinfo/`,
   `cmake-build-minsizerel/`, `out/`, `out/build/`, `builddir/`

If found, detection confidence is `"high"`. If not found, confidence
is `"medium"`.

## CMake Build Directory Detection

For CMake projects, the detector also searches for `CMakeCache.txt` to
find configured build directories (both in-tree and out-of-tree). From
`CMakeCache.txt` it extracts:

- `CMAKE_C_COMPILER` / `CMAKE_CXX_COMPILER`
- `CMAKE_BUILD_TYPE`
- `CMAKE_EXPORT_COMPILE_COMMANDS` (whether export was enabled)

## ClangdAdapter — Compile Database Generation

The adapter never writes to the project tree. All generated files go to
a platform-specific data directory (`projects/{hash}/clangd/` under the
user data dir determined by [platformdirs](https://pypi.org/project/platformdirs/)).

### Resolution order

1. **Explicit** `build_info.compile_commands_dir` — check freshness, copy to managed dir
2. **Detected** `details.compile_commands_dir` — check freshness, copy to managed dir
3. **Generate for CMake**: run `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
   in an out-of-tree build under the managed dir.
4. **Generate for Meson**: run `meson setup` with build dir under managed dir

If a candidate `compile_commands.json` is **stale**, it is skipped and generation
is attempted instead (for CMake/Meson projects). If the build system does not
support generation, the stale file is used with a warning.

### Staleness detection

A `compile_commands.json` is considered stale if either condition holds:

- **Build config newer**: any `CMakeLists.txt` or `meson.build` in the project
  tree has a modification time newer than the `compile_commands.json`
- **Dead source references**: any source file referenced in the compilation
  database no longer exists on disk

All referenced files are checked (not sampled), which takes ~10-20ms even for
large projects (10K+ entries) due to OS dentry caching.

### CMake generation details

- If no existing build dir, `cmake -S <project> -B <managed>/cmake-build`
  creates a fresh out-of-tree build under the managed directory.
- The project tree is never modified.
- Timeout: 120 seconds.

## Additional Config Detection

| File | Field | Description |
|------|-------|-------------|
| `compile_flags.txt` | `details.compile_flags_txt` | Simpler alternative to compile_commands.json; clangd reads it automatically |
| `.clangd` | `details.clangd_config` | Per-project clangd configuration (CompileFlags, Diagnostics, etc.) |

## Detection Output

```python
DetectedLanguage(
    language="cpp",              # or "c"
    build_system="cmake",        # or "meson", "autotools", "bazel", "make", "unknown"
    confidence="high",           # "high" if compile_commands.json found, else "medium"
    build_info={
        "compile_commands_dir": "/path/to/dir",  # only if found
    },
    details={
        # Compile database
        "compile_commands_dir": "/path/to/dir",

        # CMake-specific
        "cmake_build_dirs": ["/project", "/project/build"],
        "c_compiler": "/usr/lib64/ccache/gcc",
        "cxx_compiler": "/usr/lib64/ccache/g++",
        "cmake_build_type": "Debug",
        "compile_commands_available": True,  # CMAKE_EXPORT_COMPILE_COMMANDS was ON

        # Additional config
        "compile_flags_txt": True,
        "clangd_config": True,
    }
)
```
