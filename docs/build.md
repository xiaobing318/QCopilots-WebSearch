<a name="build-and-package"></a>

# 构建与打包

服务支持 amd64 架构的 Windows 11 和 Ubuntu 26.04。Windows 自动构建在 Windows Server 2025 上运行，便携 ZIP 在本地 Windows 11 上验证。

<a name="prerequisites"></a>

## 前置条件

- Windows 需要 Visual Studio 2022 或 2026，并安装「使用 C++ 的桌面开发」工作负载和 Windows SDK。同时需要 CMake 3.30 或更高版本、Ninja、Git 和 Python 3.12 或更高版本。构建流程会自动查找 x64 工具，也可使用 Visual Studio 自带的 CMake 和 Ninja。构建时会校验并静态链接 Mbed TLS 3.6.8，用于接收业务 HTTPS 请求。libcurl 通过系统 Schannel 访问搜索源和网页。
- Ubuntu 在运行 Bash 构建脚本前需要 Python 3.12 或更高版本，并提供 venv 和 pip。`bootstrap` 阶段通过 apt 安装 GCC、CMake、Ninja、Git、发行版提供的 OpenSSL 开发文件及其他原生依赖。安装系统软件包需要以 root 身份执行，或使用非交互式 sudo。
- `bootstrap` 阶段会准备 Qt 6.12.0、Node 24.22.3 和隔离的 Python 工具环境。Node 用于构建内置设置界面。安装后的原生服务已经内置该界面，并使用 Qt 共享库。

<a name="build-and-configure"></a>

## 构建与配置

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --ConfigPath config/BuildPipeline.json --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

```bash
bash scripts/BuildPipeline.sh --ConfigPath config/BuildPipeline.json --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

两个入口脚本调用同一套 Python 实现，并依次执行指定阶段。`--CheckOnly` 只检查并报告前置条件，`--DryRun` 只打印将要执行的命令。

若需修改本地的构建、安装、缓存或工具路径，请复制配置文件，并将副本命名为以 `.local.json` 结尾的文件。相对路径以仓库根目录为基准，配置方案中的环境变量会展开。Windows 上的默认 Qt 根目录为 `C:/Softwares/Qt`。如果只需要 ZIP，将 Windows 配置方案的 `packageGenerators` 设为 `["ZIP"]`。

配置阶段使用 CMake 预设 `windows-x64`、`ubuntu-x64` 和 `ubuntu-x64-quality`。原生构建的 CI 配置设置 `QCWS_BUILD_WEB=OFF`，并通过 `QCWS_WEB_DIST_DIR` 使用前端作业预先生成的产物。本地默认会构建前端。

下载的工具和源码会与仓库中提交的哈希值核对。在本地配置方案中设置 `proxy` 可为下载指定代理，留空时直连。此代理也用于 apt、pip 和 npm。服务运行时的出站请求独立使用直连。离线构建时请设置 `offline: true`，并提前安装系统工具、准备已校验的缓存。离线构建前端还需要已包含锁定版本依赖的 npm 缓存，或可直接使用的预构建产物。

<a name="windows-msi-tooling"></a>

## Windows MSI 打包工具

准备好 .NET 10 SDK 后，安装官方 WiX 7.0.1 和同版本的 UI 扩展。

```text
dotnet tool install --tool-path <tool-directory> wix --version 7.0.1
wix extension add -g WixToolset.UI.wixext/7.0.1 -acceptEula wix7
```

将工具目录加入 PATH，或在本地配置方案中设置 `tools.wix`。CPack 通过 `CPACK_WIX_VERSION=4` 使用 WiX .NET 驱动。WiX 条款见[许可证与可替换库](licensing.md)。

`publish` 阶段将软件包输出到所选配置方案的 `buildDir/packages/<timestamp>`。安装 MSI 需要提升权限才能注册系统服务。推送版本标签后，GitHub 会构建和测试该版本，通过后再发布软件包。详见 [GitHub 发布流程](release.md)。

<a name="test"></a>

## 测试

CTest 使用本地可复现的测试数据和环境，覆盖 TLS、HTTP、设置、搜索后端和 MCP。单独检查前端时，请使用 `.nvmrc` 锁定的 Node 版本。

```bash
cd web
npm ci --ignore-scripts
npm run lint
npm run format:check
npm run typecheck
npm run test:ci
npm run build
```

验证已安装的可执行文件时，先在隔离的 Python 环境中使用 `pip --require-hashes` 安装 `tests/requirements.lock` 锁定的依赖，再运行 SDK 检查。

```text
python scripts/test_mcp_sdk.py --Executable <installed-binary> --Output <report.json>
```

使用 `--PageFixture <build>/tests/qcws_sdk_page_fixture`（Windows 添加 `.exe`）可运行双传输离线网页成功链。CTest 可通过 `QCWS_SDK_TEST_PYTHON` 指定装有锁定 SDK 的解释器。指定 `--PublicUrl <公开测试页>` 后，读取、查找或续读失败会令验收退出非零。

添加 `--LiveProvider auto` 可访问真实的公共搜索源。此项检查的结果应独立于本地可复现测试结果记录。

CI 使用 Windows ZIP 解压后的程序和 Ubuntu DEB 安装后的程序执行 SDK 与冒烟测试。Ubuntu 使用本次构建的 DEB 检查重装、故障恢复及卸载，分别验证 `remove` 和 `purge` 对运行配置与私有数据的处理。手动 CI 和 Release 还会在 Windows 上使用同一份 MSI，依次检查默认及自定义安装目录、配置和凭据保留，以及初始化后和服务启动后两种安装失败的回滚。各平台在安装验收前核对包的 SHA-256。Windows 本地 MSI 验收需在可丢弃的测试机上以管理员身份运行 `scripts/AcceptWindowsMsi.ps1`。[发布验收](release.md)
