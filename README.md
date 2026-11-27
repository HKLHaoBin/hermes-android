# Hermes Android

独立社区 Android 移植版：在手机本地运行 Hermes Agent 与 Hermes WebUI，不依赖电脑、Termux App 或远程 Hermes。使用云模型仍需联网并配置自己的服务商凭据。

这不是 Nous Research 官方 Android 发行版。Agent 与 WebUI 及其依赖遵循各自的许可证。

- 下载签名 APK：[GitHub Releases](https://github.com/HKLHaoBin/hermes-android/releases/latest)
- 项目网站：[Hermes Android](https://hklhaobin.github.io/hermes-android/)

## 安装与使用

1. 支持 **arm64-v8a，Android 7.0 及以上**。下载 APK 后按系统提示侧载安装。
2. 首次启动等待运行时解包；在 WebUI 设置里配置模型服务商、模型及自己的凭据。
3. 聊天输入框旁可打开内置终端；它复用 WebUI 的认证与命令通道。
4. 正式版固定包名 `io.github.hkhaobin.hermeswebui` 和长期签名，可覆盖升级同签名版本。不要卸载或清除应用数据来解决升级问题。

### 导入手机上的文件（v0.1.2）

1. 新建或打开会话，点击输入框旁的 **回形针 📎（导入文件）**。只有这一个入口，不再另设文字按钮。
2. 在系统文件选择器里选中 ZIP、文档或其他文件。无需开放整个手机存储。
3. 文件立即上传到当前会话的工作目录。界面只有收到读回校验回执后才显示成功，并展示实际路径、大小、SHA-256。
4. 路径会加入当前会话的输入框；补充处理要求后发送。此时只完成文件导入，**不代表压缩包中的会话已经解析或导入**。

原始 ZIP 会保留，不自动解压；同名文件另存，不覆盖旧文件。取消、0 字节、网络失败及未通过验证都会明确显示。ZIP 结构检查不等于会话格式或所有文件 CRC 检查。

回形针选文件、粘贴和拖入文件统一立即导入，不再新增需要等发送消息才上传的附件。发送消息只是把处理要求和已验证的文件路径交给 Agent。旧版本已经保存的未发送附件草稿不会被自动删除。

图片会保留已验证的消息元数据，在发送包含该图片路径的问题时一起传给原生多模态流程，不重复上传；具体是否原生看图仍遵循所选模型与 `image_input_mode` 设置。图片信息按原会话及配置隔离，发送失败时保留待确认信息。

## 文件与工具边界

- 应用没有申请「管理所有文件」权限。终端能列出目录或取得 `stat` 信息，不等于能读取其他应用写入的内容；遇到拒绝访问时，使用系统文件选择器导入，不要反复全盘搜索或要求用户猜权限开关。
- 不承诺访问其他应用的 `Android/data` 或内部私有目录。系统选择器的 URI 授权不等于原始文件路径获得全局访问权。
- 终端使用 Android `/system/bin/sh` 与 Toybox，不是完整 Linux 发行版。命令参数和日期语法要实际验证；保留错误输出，便携的按时间筛选可用 Python `os.scandir` / `stat`。
- 不内置 Chromium、Electron、Docker、完整 Bash/Git。浏览器工具是否能运行，要看实际后端和依赖；联网检索与本地浏览器自动化是不同能力。
- 云模型连接中断与存储权限是独立问题。配置了某个提供商不意味着所有可选工具的凭据均已配置。
- `targetSdkVersion=28` 为运行 App 私有目录中的 Android 运行时而保留，适合自行侧载；部分系统会提示应用面向旧版 Android。
- 长任务仍受 Android 后台与电池策略约束。保留运行中的任务，不随意清空配置或重启服务作为通用修复。

## 构建和验证

只通过 GitHub Actions 生成 APK：推送 `main` 或手动运行 **Build APK**。

- 官方 Android arm64 运行时由 `runtime.lock` 固定；下载时校验 APT 签名与包 SHA-256。当前索引不再列出旧版时，只允许按 `runtime-package.lock.json` 获取同一官方包池文件，并核对预先审查的 SHA-256、大小与内嵌源码提交；不改用最新版本。
- WebUI 从工作流固定的公开提交取允许列表；不上传本机 Windows WebUI、用户配置、密钥或会话。
- 构建阶段打入 Android 兼容补丁、本地终端资源和文件导入界面。
- 缺少长期签名材料时失败，不退回临时调试签名。构建后验证 v1/v2/v3 和固定证书 SHA-256。
- 运行导入前端回归；在 ARM Android 容器里测试认证、CSRF 拒绝、真实 multipart 文件写入与读回、同名保护、ZIP 保留和失败路径，并保留终端、RPC socket、会话数据库回归。

Android 容器不等于 WebView 或红米 K70 / HyperOS 真机验收。Release 说明会区分已执行验证与需要原设备复测的内容。

### 真机验收与发布门禁（Issue #14）

- 真机矩阵、测试 ID、receipt schema 和发布 gate 见 [`docs/android-device-acceptance.md`](docs/android-device-acceptance.md)。矩阵槽位只描述 API/OEM/ABI，不伪造设备或结果。
- `scripts/android_device_matrix.py` 只从实际授权 ADB 设备读取身份；没有在线设备时 capture 明确返回 `BLOCKED`，不写入 receipt。
- receipt 必须绑定精确设备/API/OEM/WebView、APK/runtime/WebUI/Actions identity、证据 SHA-256 和脱敏状态。`PASS`、`FAIL`、`BLOCKED`、`NOT RUN` 的语义不可互换。
- Build APK 的 `apk` Actions artifact 额外包含 `artifact-manifest.json`，绑定 APK、Actions run/head、runtime/payload/WebUI 和签名身份；Publish verified APK 会重新核对并保留该 manifest。
- Build APK CI 只做 matrix/schema/static validation、既有签名/payload/container smoke；不连接手机、不修改 package verifier、不把 container 结果当作真机结果。

构建成功后，手动运行 **Publish verified APK**，提供成功的 `main` 构建编号与新版本标签。发布工作流重新验证来源、APK SHA-256 与证书，只发布验证过的工件。

## 源码结构

- `android/`：原生 WebView、运行服务与运行时安装器。
- `scripts/relocate_payload.py`：校验并重定位官方运行时。
- `scripts/android_runtime_fixes.py`：构建期 Android 运行时兼容补丁。
- `scripts/stage_android_terminal.py`：固定终端资源与 Android WebUI staging 入口。
- `scripts/stage_android_import.py`、`android_import.js`、`android_import_api.py`：立即导入与校验回执。
- `scripts/smoke_webui.py`、`smoke_runtime_fixes.py`：打包运行时的实际回归。
- `docs/`：项目网站。

## 许可

本仓库代码采用 MIT 许可。APK 中包含的上游运行时、WebUI、终端库和图标素材保留原有许可与署名。
