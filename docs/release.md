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

标准路径为：提交并推送 main，等待该提交的 main push CI 成功，再推送对应版本标签。Release 工作流随后执行发布构建、安装包生命周期和 Caddy 验收，全部成功后才发布。可以先在功能分支验证后合入 main，也可以按仓库权限直接在 main 提交。下面以直接在 main 发布 `v0.4.0` 为例。

`release.yml` 要求标签与根目录 `CMakeLists.txt` 的 `QCWS_VERSION` 一致。`scripts/release_gate.py` 检查标签提交已属于 main，且同一 SHA 最新一次 main push `ci.yml` 运行已成功。缺少记录、仍在运行、失败或取消都会阻止发布。手动 CI 成功不能替代这条 main push 记录，不能把尚未验收的 main 提交和版本标签同时推送。

### 1. 同步 main 并提交版本改动

以下 PowerShell 命令在同一终端中依次执行，需要已安装 Git 和 [GitHub CLI](https://cli.github.com/)，并有仓库写入及 Actions 操作权限。先保留和审查已有本地修改。同步失败时先处理分支或文件冲突，不要强制重置。若发布提交已经完成，跳过暂存和提交命令，继续核对版本及工作区。

```powershell
Set-Location C:\Dev\github-repos\QCopilots-WebSearch
$repo = 'xiaobing318/QCopilots-WebSearch'
$version = '0.4.0'
$tag = "v$version"
gh auth status
if ($LASTEXITCODE -ne 0) { throw '请先运行 gh auth login 完成登录。' }
git status --short
git fetch origin --tags
if ($LASTEXITCODE -ne 0) { throw '获取远端引用失败。' }
git switch main
if ($LASTEXITCODE -ne 0) { throw '切换 main 失败。' }
git merge --ff-only origin/main
if ($LASTEXITCODE -ne 0) { throw 'main 无法快进，请先处理分支差异。' }
```

完成本次功能与版本更新，使 `CMakeLists.txt` 中的 `QCWS_VERSION` 为 `0.4.0`，同步相关版本说明并完成本地验证。确认 `git status` 中的修改都属于本次发布，且没有凭据或临时文件，再暂存和审查提交。

```powershell
git diff --check
if ($LASTEXITCODE -ne 0) { throw '请先修复 diff 检查问题。' }
git add --all
git diff --cached --stat
git diff --cached
```

审查通过后提交。若已完成提交，直接从工作区和版本检查开始。

```powershell
git commit -m "Prepare release $tag"
if ($LASTEXITCODE -ne 0) { throw '提交失败，请核对暂存内容。' }
if (git status --porcelain) { throw '发布前工作区必须干净。' }
$versionMatch = Select-String -LiteralPath CMakeLists.txt -Pattern '^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"'
if (-not $versionMatch -or $versionMatch.Matches[0].Groups[1].Value -ne $version) {
    throw 'QCWS_VERSION 与计划标签不一致。'
}
$releaseSha = (git rev-parse HEAD).Trim()
git push origin main
if ($LASTEXITCODE -ne 0) { throw '推送 main 失败，不能继续打标签。' }
```

### 2. 等待同一 SHA 的 main push CI

推送后等待 Actions 创建运行，再执行下面的查询。如果暂未出现运行记录，稍后重新执行查询，不要改用其他提交的成功结果。查询限定 `ci.yml`、main、push 和发布 SHA，选择最新运行。[运行查询](https://cli.github.com/manual/gh_run_list) · [等待运行结束](https://cli.github.com/manual/gh_run_watch)

```powershell
$pushRuns = @(gh run list --repo $repo --workflow ci.yml --branch main --event push --commit $releaseSha --limit 1 --json databaseId,headSha,status,conclusion | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $pushRuns.Count -ne 1) { throw '尚未找到本提交的 main push CI。' }
$pushRunId = $pushRuns[0].databaseId
gh run watch $pushRunId --repo $repo --exit-status
if ($LASTEXITCODE -ne 0) { throw 'main push CI 未成功，不得发布。' }
$pushResult = gh run view $pushRunId --repo $repo --json headSha,status,conclusion | ConvertFrom-Json
if ($pushResult.headSha -ne $releaseSha -or $pushResult.status -ne 'completed' -or $pushResult.conclusion -ne 'success') {
    throw 'main push CI 的提交或结果不符合发布条件。'
}
```

这次 main push CI 已包含 ASan、UBSan、clang-tidy 和限时模糊测试。通常下一步即可打标签，Release 会自动执行完整的安装和部署验收。

如需在打标签前提前检查安装生命周期、Caddy 部署或下载包进行桌面联调，可在上述 push CI 完成后选择运行一次手动 CI。不要并行触发，同一分支的 push 和手动 CI 共用 `cancel-in-progress` 并发组，新运行会取消旧运行。验收期间不要推送新提交。[手动触发](https://cli.github.com/manual/gh_workflow_run)

```powershell
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw '获取 main 失败。' }
if ((git rev-parse origin/main).Trim() -ne $releaseSha) { throw '远端 main 已变化，请重新选择发布提交并验收。' }
gh workflow run ci.yml --repo $repo --ref main
if ($LASTEXITCODE -ne 0) { throw '手动 CI 触发失败。' }
gh run list --repo $repo --workflow ci.yml --branch main --event workflow_dispatch --commit $releaseSha --limit 5 --json databaseId,headSha,status,conclusion,url
$deepRunId = Read-Host '输入本次手动运行的 databaseId，若列表尚未更新则先重新查询'
$deepRun = gh run view $deepRunId --repo $repo --json workflowName,event,headBranch,headSha | ConvertFrom-Json
if ($deepRun.workflowName -ne 'CI' -or $deepRun.event -ne 'workflow_dispatch' -or $deepRun.headBranch -ne 'main' -or $deepRun.headSha -ne $releaseSha) {
    throw '选择的运行不是本提交的 main 手动 CI。'
}
gh run watch $deepRunId --repo $repo --exit-status
if ($LASTEXITCODE -ne 0) { throw '手动验收未成功，请先处理失败。' }
```

需要人工桌面验收时，下载这次运行的 `packages-windows-x64`。网页下载的该 Artifact 是外层 ZIP，其中包含 MSI 和便携 ZIP。在 Windows 11 上验证便携 ZIP，需要 QGIS 联调时使用实际公网 HTTPS 地址完成工具发现、搜索、网页打开和查找。记录提交 SHA、Actions 运行 ID 和包的 SHA-256，结束后关闭本次启动的测试进程。

### 3. 创建并推送带注释的版本标签

推送标签前再次核对 main push CI，避免采用已经被后续失败或取消运行替代的记录。标签固定在 `$releaseSha`，默认不覆盖或移动已有标签，未发布标签的异常处理见下文。

```powershell
$gateRuns = @(gh run list --repo $repo --workflow ci.yml --branch main --event push --commit $releaseSha --limit 1 --json status,conclusion | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $gateRuns.Count -ne 1 -or $gateRuns[0].status -ne 'completed' -or $gateRuns[0].conclusion -ne 'success') {
    throw '最新 main push CI 尚未成功。'
}
git tag -a $tag $releaseSha -m "QCopilots-WebSearch $version"
if ($LASTEXITCODE -ne 0) { throw '标签创建失败，检查是否已存在，不要强制覆盖。' }
git push origin "refs/tags/$tag"
if ($LASTEXITCODE -ne 0) { throw '标签推送失败，请检查远端状态，不要强制推送。' }
```

标签 push 会自动触发 Release，不需要手动创建 GitHub Release 或上传本地构建包。

### 4. 跟踪 Release 并验证发布资产

等待列表出现本标签的 Release 运行，选择对应 ID 并核对提交。Release 只有在构建和安装部署验收通过后，才会校验并下载锁文件指定的 Qt 源码、生成 `SHA256SUMS`、签发三个安装包的构建来源证明，最后创建正式发布页面。

```powershell
gh run list --repo $repo --workflow release.yml --event push --commit $releaseSha --limit 5 --json databaseId,headBranch,headSha,status,conclusion,url
$releaseRunId = Read-Host '输入本标签 Release 运行的 databaseId，若列表尚未更新则先重新查询'
$releaseRun = gh run view $releaseRunId --repo $repo --json workflowName,headBranch,headSha | ConvertFrom-Json
if ($releaseRun.workflowName -ne 'Release' -or $releaseRun.headBranch -ne $tag -or $releaseRun.headSha -ne $releaseSha) {
    throw '选择的运行与发布标签或提交不一致。'
}
gh run watch $releaseRunId --repo $repo --exit-status
if ($LASTEXITCODE -ne 0) { throw 'Release 未成功，请检查失败日志。' }
gh release view $tag --repo $repo
$releaseDir = Join-Path (Get-Location) "release-evidence\$tag"
gh release download $tag --repo $repo --dir $releaseDir
if ($LASTEXITCODE -ne 0) { throw '下载失败，请检查目录和发布资产。' }
```

正式发布的 **Assets** 应有一个 Windows 便携 ZIP、一个 Windows MSI、一个 Ubuntu amd64 DEB、锁文件对应的 `qtbase-everywhere-src-*.tar.xz` 和 `SHA256SUMS`。GitHub 自动生成的项目源码 ZIP/TAR.GZ 不能代替 Qt 源码归档。来源证明关联三个安装包，不是必须单独出现在 Assets 中的文件。[发布下载](https://cli.github.com/manual/gh_release_download)

```powershell
$assets = @(Get-ChildItem -LiteralPath $releaseDir -File)
foreach ($extension in @('.zip', '.msi', '.deb')) {
    if (@($assets | Where-Object Extension -eq $extension).Count -ne 1) { throw "缺少或重复安装包：$extension" }
}
$lock = git show "${releaseSha}:config/dependencies.lock.json" | ConvertFrom-Json
$qtName = [IO.Path]::GetFileName(([uri]$lock.qt_source.url).AbsolutePath)
$qtPath = Join-Path $releaseDir $qtName
if ((Get-FileHash -LiteralPath $qtPath -Algorithm SHA256).Hash -ne $lock.qt_source.sha256) { throw 'Qt 源码与发布提交的锁文件不一致。' }
$checksums = @(Get-Content -LiteralPath (Join-Path $releaseDir 'SHA256SUMS'))
if ($assets.Count -ne 5 -or $checksums.Count -ne 4) { throw '发布资产或校验清单数量不符合预期。' }
foreach ($line in $checksums) {
    if ($line -notmatch '^([0-9a-f]{64})  ([^/\\]+)$') { throw 'SHA256SUMS 条目格式不正确。' }
    $expectedHash = $Matches[1]
    $assetPath = Join-Path $releaseDir $Matches[2]
    if ((Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash -ne $expectedHash) { throw "文件校验失败：$assetPath" }
}
foreach ($package in $assets | Where-Object Extension -in @('.zip', '.msi', '.deb')) {
    gh attestation verify $package.FullName --repo $repo
    if ($LASTEXITCODE -ne 0) { throw "构建来源证明验证失败：$($package.Name)" }
}
```

保留 `$releaseSha`、`$pushRunId`、`$releaseRunId` 和校验结果，打开 [GitHub Releases](https://github.com/xiaobing318/QCopilots-WebSearch/releases) 确认版本及资产。校验通过表示下载文件与清单和构建证明相符，不替代目标机器上的安装及实际连接测试。

### 失败处理

- main push CI 失败或被取消时，先查看该运行的失败日志。若无需改代码，可在 Actions 重跑同一运行，成功后再打标签。若修改了代码，提交新 SHA 并重新等待它的 main push CI，不沿用旧结果。
- 手动 CI 意外取消 main push CI 时，手动成功仍不能通过发布门禁。等手动运行结束，再重跑对应的 main push CI 并确认成功。
- Release 因主分支 CI 未完成而被门禁拒绝时，先让同 SHA 的 main push CI 成功，再重跑原 Release。基础设施或下载暂时失败且未创建发布时，也可重跑原运行。查看日志可使用 `gh run view <运行ID> --repo $repo --log-failed`。
- 标签已推送但尚未创建 Release，而修复需要新提交时，推荐使用新的版本号。只有仓库维护者明确同意，且已确认没有该版本的 Release 或发布资产，才考虑修正尚未发布的标签。新提交仍须先通过对应的 main push CI，再触发新的标签发布运行。重跑旧运行不会自动使用修复后的提交。
- Release 已创建但后续检查失败时，先核对现有资产和日志，不直接重复创建或覆盖资产。需要代码或版本修复时使用新提交和新版本，例如 `v0.4.1`。不要删除并重建已发布标签，不要移动标签到另一提交。

参阅[许可声明和 Qt 替换说明](licensing.md)。
