<a name="licensing-and-replaceable-libraries"></a>

# 许可证与可替换库

本项目自有的原生代码采用 Apache-2.0 许可证。独立的 QGIS 插件在 QGIS 仓库中维护，采用 GPL-2.0-or-later 许可证。两个项目的源代码和各自使用的第三方库均适用各自的原有许可证。

## Qt

本项目动态链接 Qt 6.12.0 的 Core、Network 和 SQL 库。SQLite、网络信息和 TLS 插件沿用上游许可证。本项目未使用 Qt HTTP Server。项目按 LGPL-3.0 选项使用 Qt，并遵守该许可证纳入的适用 GPL-3.0 条款。

每个发布版本均附带未经修改的对应 `qtbase-everywhere-src-6.12.0` 源码归档及其 SHA-256 校验值，同时提供上游许可证原文、Qt 构建选项和原始 Qt 软件物料清单（SBOM）。上游 SBOM 的范围为完整的 qtbase 发行版。

安装后的程序允许用户自行重建 Qt，并替换相应的共享库和插件。程序无需校验 Qt DLL 或厂商签名。为了调试对 LGPL 组件所做的修改，允许进行逆向工程。替换时应确保 ABI、目标架构、编译器 ABI 和构建配置兼容。在 Windows 上，先停止服务，再替换 `bin` 和 `plugins` 下的 Qt 文件。在 Ubuntu 上，先停止对应版本的 systemd 单元，再替换 `/opt/qcopilots-websearch-<version>/lib` 和同级 `plugins` 目录下该版本专用的 Qt 文件。请保留备份，替换后运行 `--Version`、`--CheckConfig` 和可复现测试，再重启服务。

替换安装目录中的文件需要具备写入权限的管理员。使用便携 ZIP 时，可在解压目录中直接替换，无需管理员权限。重新安装软件包可能会恢复随包分发的库版本，请另行保存自行修改的构建产物。

源码归档和声明与二进制文件一同发布，可以从同一个发布版本直接下载。另请查阅软件包中的许可证原文和 [Qt 分发指南](https://www.qt.io/development/open-source-lgpl-obligations)。

<a name="other-components"></a>

## 其他组件

原生构建静态链接 cpp-httplib（MIT）、libcurl（curl license）、c-ares（MIT）、Lexbor（Apache-2.0）、nlohmann/json（MIT）、JSON Schema Validator（MIT）和 zlib（Zlib）。这些组件的原始声明安装在 `licenses` 目录中。Linux 版 Qt 还包含 ICU 73.2 库及其 Unicode/ICU 声明。

Windows 版通过静态链接的 Mbed TLS 3.6.8 接收业务 HTTPS 请求。上游提供 Apache-2.0 或 GPL-2.0-or-later 双许可，本项目选择 Apache-2.0。软件包在 `licenses/mbedtls` 中附有上游许可原文。Windows 的出站 HTTPS 使用操作系统提供的 Schannel。[Mbed TLS 上游许可证](https://github.com/Mbed-TLS/mbedtls/blob/mbedtls-3.6.8/LICENSE)

Linux 的入站和出站 HTTPS 使用由发行版管理的 OpenSSL 共享库。DEB 软件包会记录所需的系统依赖。安装 Ubuntu 安全更新后，请重启服务以加载更新后的库。

前端产物包含 `THIRD_PARTY_NOTICES.txt`，用于列明前端依赖的声明。自动搜索后端及搜索结果页（SERP）的处理参考了 Strata。其 MIT 许可证和著作权声明安装在 `licenses/strata` 下。

项目修改了 cpp-httplib 0.66.0 的头文件，以约束请求输入的生命周期，并在服务关闭时中断服务持有的套接字。修改后的文件仍适用 MIT 许可证。软件包的 `share` 目录包含修改后的头文件、补丁脚本和修改说明。

Windows 软件包包含 Visual Studio 授权再分发目录中允许分发的 Microsoft Visual C++ 运行库 DLL。这些 DLL 属于 Microsoft 的专有组件，适用 Microsoft 的再分发条款。软件包的 `NOTICE` 对其单独列明。Windows 系统 DLL 由本机操作系统提供。

在团队部署中，SearXNG（AGPL-3.0-or-later）、Caddy（Apache-2.0）和 Valkey（BSD-3-Clause）作为独立服务运行。部署清单列出这些服务所用的容器镜像及许可证。部署经修改的 SearXNG 时，须满足其许可证规定的源码共享要求。

官方 WiX 7 软件包对符合条件的用途设有维护费条款。所需许可证应按适用条件另行取得。WiX 仅用于构建，已安装的服务运行时无需 WiX。使用此发行版构建前，请查阅[官方软件包条款](https://www.nuget.org/packages/wix/7.0.1)。
