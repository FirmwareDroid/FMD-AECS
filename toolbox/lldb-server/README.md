# Installing lldb-server for Android
- Select lldb-server binary for current Android version

| Clang revision | LLVM version | Android version |
| -------------- | ------------ | --------------- |
| r450784e       | LLVM 14      | Android 12      |
| r475365b       | LLVM 15      | Android 12L     |
| r487747c       | LLVM 16      | Android 13      |
| r498229b       | LLVM 17      | Android 13      |
| r510928        | LLVM 17.x    | Android 14      |

- Find lldb-server binaries in AOSP source tree
```
~/aosp/aosp14$ find ./ -name "lldb-server"
./prebuilts/clang/host/linux-x86/clang-r487747c/runtimes_ndk_cxx/aarch64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r487747c/runtimes_ndk_cxx/x86_64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r487747c/runtimes_ndk_cxx/arm/lldb-server
./prebuilts/clang/host/linux-x86/clang-r487747c/runtimes_ndk_cxx/i386/lldb-server
./prebuilts/clang/host/linux-x86/clang-r498229b/runtimes_ndk_cxx/riscv64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r498229b/runtimes_ndk_cxx/aarch64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r498229b/runtimes_ndk_cxx/x86_64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r498229b/runtimes_ndk_cxx/arm/lldb-server
./prebuilts/clang/host/linux-x86/clang-r498229b/runtimes_ndk_cxx/i386/lldb-server
./prebuilts/clang/host/linux-x86/clang-r450784e/runtimes_ndk_cxx/aarch64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r450784e/runtimes_ndk_cxx/x86_64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r450784e/runtimes_ndk_cxx/arm/lldb-server
./prebuilts/clang/host/linux-x86/clang-r450784e/runtimes_ndk_cxx/i386/lldb-server
./prebuilts/clang/host/linux-x86/clang-r475365b/runtimes_ndk_cxx/aarch64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r475365b/runtimes_ndk_cxx/x86_64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r475365b/runtimes_ndk_cxx/arm/lldb-server
./prebuilts/clang/host/linux-x86/clang-r475365b/runtimes_ndk_cxx/i386/lldb-server
./prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/riscv64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/aarch64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/x86_64/lldb-server
./prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/arm/lldb-server
./prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/i386/lldb-server
```

- Copy lldb-server binary to this folder and rename it to lldb-server
```bash
# Android 14
cp ~/aosp/aosp14/prebuilts/clang/host/linux-x86/clang-r510928/runtimes_ndk_cxx/aarch64/lldb-server ./
chmod +x lldb-server
```

## Connect to lldb-server on Android device
- Forward port from host to device
```bash
adb forward tcp:1338 tcp:1338
```
- Starting lldb client
```
lldb
(lldb) platform select remote-android
(lldb) platform connect connect://localhost:1338
# Attach an app
attach <PID>
```