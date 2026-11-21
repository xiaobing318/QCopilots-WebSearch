<a name="build-and-publish-a-release"></a>

# 构建与发布

GitHub Actions 在 Windows Server 2025 上构建 Windows x64 便携 ZIP 和 MSI 安装包，在 Ubuntu 26.04 上构建并验证 Ubuntu 26.04 amd64 DEB 安装包。Windows 桌面验收使用 Windows 11 上的便携 ZIP。

<a name="continuous-integration"></a>

## 持续集成

- `ci.yml` 在推送、拉取请求和手动触发时运行，并检查文档标点。
- 可复用工作流 `build.yml` 使用 `.nvmrc` 指定的 Node 版本构建一次 TypeScript 前端。各平台的原生构建任务共用该 `web-dist` 和仓库中的 CI 构建配置，并使用缓存加速构建。
- 每项原生构建任务都配置、编译、测试、安装并打包服务。SDK 测试及 ZIP 或 DEB 的冒烟测试使用已安装的程序。任务失败时会上传可用的 CTest 结果和诊断信息。
- 推送主分支或手动运行时，还会在 Ubuntu 26.04 上执行 ASan、UBSan、clang-tidy 和限时模糊测试。
- 手动运行和发布运行还会验收安装包生命周期及 Caddy 部署。验收使用两个基线。基于当前源码构建的 0.0.1 用于验证多版本并装时的配置、凭据和监听端口隔离。基于固定历史提交构建的 0.2.0 使用旧版系统目录布局，用于验证新安装包与旧版部署并存。

功能分支可直接运行常规工作流。CI 和本地构建流程的 `publish` 阶段只生成构建产物。MSI 验收需要具备管理员权限且可丢弃的 Windows 运行环境。

<a name="publish-from-a-version-tag"></a>

## 通过版本标签发布

先在独立功能分支完成本地验证，再推送分支。等待普通 push CI 成功后，对同一最终提交手动运行 CI，使完整的质量检查和安装生命周期验收都执行。两次运行共用该分支的并发组，同时运行会取消较早的一次。验收期间不再推送新提交，并核对手动运行的提交 SHA。下载该次运行的 `packages-windows-x64`，取其中的便携 ZIP 在 Windows 11 上验证。记录提交 SHA、Actions 运行 ID 和包的 SHA-256。需要 QGIS 联调时，通过实际的公网 HTTPS 地址完成工具发现、搜索、网页打开和查找。

确认功能分支验收通过后再合入 main，并等待合入提交的 main push CI 成功。将仓库根目录 CMake 文件中的版本号作为 `vMAJOR.MINOR.PATCH` 标签推送。`release.yml` 先校验标签与版本一致，再通过 GitHub API 检查标签提交已属于 main，且同一 SHA 最近一次 main push `ci.yml` 运行已成功。缺少记录、仍在运行、失败或取消都会阻止发布。之后调用 `build.yml` 完成发布构建和验收。

发布任务使用通过验收的 ZIP、MSI 和 DEB 安装包，依据 `config/dependencies.lock.json` 校验匹配的 Qt 源码归档后将其纳入发布，生成 `SHA256SUMS`，并为安装包生成 GitHub 构建来源证明。最后，通过 `gh release create --verify-tag --generate-notes` 发布这些文件。

打开正式发布页面，在对应版本的 **Assets** 下单独下载 Windows MSI、Windows 便携 ZIP 或 Ubuntu DEB。 [GitHub Releases](https://github.com/xiaobing318/QCopilots-WebSearch/releases)

常规 Actions 运行的 **Artifacts** 中，`packages-windows-x64` 是一个外层 ZIP，其中包含 MSI 和便携 ZIP。

下载后可用 `SHA256SUMS` 校验文件。也可通过 GitHub CLI 验证安装包的构建来源。

```bash
version=0.3.1
gh attestation verify "./qcopilots-websearch-${version}_${version}_amd64.deb" --repo xiaobing318/QCopilots-WebSearch
```

参阅[许可声明和 Qt 替换说明](licensing.md)。
